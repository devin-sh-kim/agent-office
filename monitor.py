#!/usr/bin/env python3
"""agent-office: 현재 사용자의 claude / codex / grok 프로세스 상태를 웹으로 보여준다.

표준 라이브러리만 사용한다. 실행: python3 monitor.py [--host 100.x.y.z] [--port 8787]
자동 실행 등록: python3 monitor.py --install-service [--port 8787]  (어느 위치에 두든 현재 경로로 등록된다)
"""
import argparse
import glob
import json
import os
import re
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CODEX_DIR = HOME / ".codex"
GROK_DIR = HOME / ".grok"
UID = os.getuid()
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
TAIL_BYTES = 4 * 1024 * 1024

# 트랜스크립트에 윈도우 크기가 기록되지 않는 경우의 기본값
CLAUDE_CONTEXT_WINDOW = 1_000_000
SUB_KEEP = 600        # 끝난 서브에이전트를 이 시간(초) 동안 더 보여 준다
SUB_STALE = 1800      # 끝 표시 없이 이만큼 기록이 멈춘 서브에이전트는 끝난 것으로 본다


def boot_time():
    for line in open("/proc/stat"):
        if line.startswith("btime"):
            return int(line.split()[1])
    return 0


BTIME = boot_time()


# ---------------------------------------------------------------- 파일 캐시

_cache = {}


def cached(path, parser):
    """(path, mtime, size)가 그대로면 이전 파싱 결과를 재사용한다."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (str(path), parser.__name__)
    sig = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        val = parser(path)
    except Exception as e:  # 파싱 실패가 전체 응답을 깨지 않도록
        val = {"error": f"{type(e).__name__}: {e}"}
    _cache[key] = (sig, val)
    return val


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def tail_lines(path, nbytes=TAIL_BYTES):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - nbytes)
        f.seek(start)
        data = f.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # 잘린 첫 줄 버림
    for raw in lines:
        if not raw.strip():
            continue
        try:
            yield json.loads(raw)
        except Exception:
            continue


def iso_to_epoch(s):
    if not s:
        return None
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", s).replace("Z", "+00:00")
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------- 활동 요약

ACTIVITY_MAX = 6


def short(text, n=160):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def tool_summary(name, inp):
    """도구 호출 입력을 한 줄로 요약한다."""
    if not isinstance(inp, dict):
        if isinstance(inp, str):  # codex exec: JS 코드 안의 cmd:"..." 를 꺼낸다
            m = re.search(r'cmd:\s*"((?:[^"\\]|\\.)*)"', inp)
            if m:
                inp = m.group(1).replace('\\"', '"').replace("\\n", " ")
        return f"{name}: {short(inp, 120)}" if inp else name
    for k in ("description", "file_path", "path", "pattern", "command", "cmd", "url", "query", "prompt"):
        if inp.get(k):
            return f"{name}: {short(inp[k], 120)}"
    for v in inp.values():
        if isinstance(v, str) and v:
            return f"{name}: {short(v, 120)}"
    return name


# 명령줄·도구 호출 기록에 섞인 비밀값을 가린다 (API 키, 토큰, 비밀번호 등)
MASK = "***"
SECRET_RES = [
    # --api-key xxx, --token=xxx, -password xxx
    (re.compile(r"(?i)(--?[\w-]*(?:key|token|secret|passw(?:or)?d|auth|credential)[\w-]*)(=|\s+)(?!-)\S+"), r"\1\2" + MASK),
    # OPENAI_API_KEY=xxx 같은 환경 변수 지정
    (re.compile(r"\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)=\S+"), r"\1=" + MASK),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    # 잘 알려진 키 형식
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|xai-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}"
                r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[abprs]-[\w-]{10,}|glpat-[\w-]{20,})"), MASK),
]


def redact(text):
    for rx, rep in SECRET_RES:
        text = rx.sub(rep, text)
    return text


def scrub(o):
    """API 응답 전체의 문자열에서 비밀값을 가린다."""
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, list):
        return [scrub(v) for v in o]
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()}
    return o


def push(acts, ts, kind, text):
    if text:
        acts.append({"t": ts, "kind": kind, "text": text})
        del acts[:-ACTIVITY_MAX]


# ---------------------------------------------------------------- 프로세스

def proc_info(pid):
    try:
        st = os.stat(f"/proc/{pid}")
        if st.st_uid != UID:
            return None
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
        argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        stat = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    if not argv:
        return None
    rest = stat[stat.rindex(")") + 2:].split()
    # rest[0] = state(3번 필드) → n번 필드는 rest[n-3]
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = None
    return {
        "pid": pid,
        "ppid": int(rest[1]),
        "state": rest[0],
        "tty_nr": int(rest[4]),
        "utime": int(rest[11]),
        "stime": int(rest[12]),
        "starttime": int(rest[19]),
        "rss": int(rest[21]) * PAGE_SIZE,
        "argv": argv,
        "cwd": cwd,
    }


CODEX_HELPER_CMDS = {"app-server", "mcp-server", "proto", "responses-api-proxy"}


def classify(argv):
    name = os.path.basename(argv[0])
    if name in ("node", "bun") and len(argv) > 1:
        name = os.path.basename(argv[1])
    if name == "claude":
        return "claude"
    if name == "codex":
        # TUI 가 띄우는 보조 데몬(app-server 등)은 세션이 아니다
        if any(a in CODEX_HELPER_CMDS for a in argv[1:3]):
            return None
        return "codex"
    if name == "grok" or name.startswith("grok-"):
        return "grok"
    return None


def tty_name(pid):
    try:
        t = os.readlink(f"/proc/{pid}/fd/0")
        return t.replace("/dev/", "") if t.startswith("/dev/pts") or t.startswith("/dev/tty") else None
    except OSError:
        return None


_prev_cpu = {}  # pid -> (ticks, wall)


def cpu_percent(p):
    ticks = p["utime"] + p["stime"]
    now = time.time()
    prev = _prev_cpu.get(p["pid"])
    _prev_cpu[p["pid"]] = (ticks, now)
    if not prev or now - prev[1] <= 0:
        # 첫 샘플: 생애 평균
        life = now - (BTIME + p["starttime"] / CLK_TCK)
        return 100.0 * ticks / CLK_TCK / life if life > 0 else 0.0
    return 100.0 * (ticks - prev[0]) / CLK_TCK / (now - prev[1])


def descendants_cpu(pid, children):
    total = 0
    stack = list(children.get(pid, []))
    while stack:
        c = stack.pop()
        total += c["utime"] + c["stime"]
        stack.extend(children.get(c["pid"], []))
    return total


def scan():
    procs = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            p = proc_info(int(d))
            if p:
                procs[p["pid"]] = p
    children = {}
    for p in procs.values():
        children.setdefault(p["ppid"], []).append(p)

    cands = {pid: k for pid, p in procs.items() if (k := classify(p["argv"]))}
    # 같은 종류의 직계 부모(node 런처 → 네이티브 바이너리 등)는 래퍼로 보고 버린다.
    # 셸 등을 거쳐 띄운 하위 에이전트는 부모 세션과 별개로 모두 표시한다.
    drop = {procs[pid]["ppid"] for pid, kind in cands.items()
            if cands.get(procs[pid]["ppid"]) == kind}
    alive = set(procs)
    for pid in list(_prev_cpu):
        if pid not in alive:
            _prev_cpu.pop(pid, None)
    return [(procs[pid], kind) for pid, kind in cands.items() if pid not in drop], procs


def arg_value(argv, *names):
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def codex_config_arg(argv, key):
    for i, a in enumerate(argv):
        if a in ("-c", "--config") and i + 1 < len(argv) and argv[i + 1].startswith(key + "="):
            return argv[i + 1].split("=", 1)[1].strip("\"'")
    return None


# ---------------------------------------------------------------- Claude

def claude_session_index():
    """procStart(=stat starttime) → 세션 메타. 샌드박스 안의 PID 네임스페이스도 커버."""
    idx = {}
    for f in glob.glob(str(CLAUDE_DIR / "sessions" / "*.json")):
        d = cached(f, read_json_parser)
        if isinstance(d, dict) and d.get("procStart"):
            idx[str(d["procStart"])] = d
    return idx


def read_json_parser(path):
    return read_json(path)


def parse_claude_transcript(path):
    last = None
    title = None
    last_prompt = None
    last_ts = None
    acts = []
    for d in tail_lines(path):
        t = d.get("type")
        if t == "assistant" and isinstance(d.get("message"), dict) and d["message"].get("usage"):
            if not d.get("isSidechain"):
                last = d
                ts = iso_to_epoch(d.get("timestamp"))
                for b in d["message"].get("content") or []:
                    if b.get("type") == "tool_use":
                        push(acts, ts, "tool", tool_summary(b.get("name"), b.get("input")))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        push(acts, ts, "say", short(b["text"]))
        elif t == "ai-title":
            title = d.get("aiTitle")
        elif t == "custom-title":
            title = d.get("customTitle") or title
        elif t == "last-prompt":
            last_prompt = d.get("lastPrompt")
        if d.get("timestamp"):
            last_ts = d["timestamp"]
    out = {"title": title, "last_prompt": last_prompt, "last_ts": iso_to_epoch(last_ts), "activity": acts}
    if last:
        m = last["message"]
        u = m["usage"]
        out.update(
            model=m.get("model") or last.get("requestedModel"),
            effort=last.get("perTurnEffort") or last.get("effort"),
            ctx_used=(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                      + u.get("cache_creation_input_tokens", 0)),
            out_tokens=u.get("output_tokens"),
        )
    return out


def claude_project_dir(cwd):
    return CLAUDE_DIR / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def parse_claude_subagent(path):
    """subagents/agent-*.jsonl: 서브에이전트 한 개의 대화 기록."""
    task = started = last_ts = None
    last = None          # 마지막 assistant(usage 포함)
    last_role = None     # 마지막 user/assistant 메시지의 종류
    acts = []
    tools = 0
    with open(path, "rb") as f:   # 첫 지시(맡은 일)는 맨 앞에 있다
        for raw in f:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("type") == "user":
                c = (d.get("message") or {}).get("content")
                text = c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")
                task = short(text, 300)
                started = iso_to_epoch(d.get("timestamp"))
                break
    for d in tail_lines(path):
        t = d.get("type")
        if t in ("user", "assistant"):
            last_role = t
        if t == "assistant" and isinstance(d.get("message"), dict):
            m = d["message"]
            ts = iso_to_epoch(d.get("timestamp"))
            if m.get("usage"):
                last = d
            for b in m.get("content") or []:
                if b.get("type") == "tool_use":
                    tools += 1
                    push(acts, ts, "tool", tool_summary(b.get("name"), b.get("input")))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    push(acts, ts, "say", short(b["text"]))
        if d.get("timestamp"):
            last_ts = d["timestamp"]
    out = {"task": task, "started": started, "last_ts": iso_to_epoch(last_ts), "activity": acts, "tools": tools}
    if last:
        m, u = last["message"], last["message"]["usage"]
        out.update(model=m.get("model"), effort=last.get("perTurnEffort") or last.get("effort"),
                   ctx_used=(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                             + u.get("cache_creation_input_tokens", 0)))
        # 마지막 메시지가 도구 호출 없이 끝난 assistant 응답이면 완료
        out["done"] = last_role == "assistant" and m.get("stop_reason") == "end_turn"
    else:
        out["done"] = False
    return out


def claude_subagents(cwd, session_id, since):
    out = []
    base = claude_project_dir(cwd) / session_id / "subagents"
    for f in base.glob("agent-*.jsonl"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if mt < since - 5:
            continue
        r = cached(f, parse_claude_subagent)
        if not r or "error" in r:
            continue
        meta = cached(f.with_suffix(".meta.json"), read_json_parser) or {}
        out.append({
            "id": f.stem.replace("agent-", ""),
            "name": meta.get("description") or meta.get("agentType"),
            "type": meta.get("agentType"),
            "task": r.get("task"),
            "model": r.get("model"),
            "effort": r.get("effort"),
            "ctx_used": r.get("ctx_used"),
            "ctx_window": CLAUDE_CONTEXT_WINDOW if r.get("ctx_used") is not None else None,
            "started": r.get("started") or mt,
            "updated": r.get("last_ts") or mt,
            "done": r.get("done"),
            "tools": r.get("tools"),
            "activity": r.get("activity"),
        })
    return out


def enrich_claude(p, info, sess_idx):
    argv = p["argv"]
    info["model"] = arg_value(argv, "--model")
    info["effort"] = arg_value(argv, "--effort")
    info["mode"] = "headless" if ("-p" in argv or "--print" in argv) else "interactive"
    s = sess_idx.get(str(p["starttime"]))
    if not s:
        return
    info["session_id"] = s.get("sessionId")
    info["name"] = s.get("name")
    info["cwd"] = s.get("cwd") or info["cwd"]
    info["version"] = s.get("version")
    st = s.get("status")
    if st == "idle":
        info["status"] = "idle"
    elif st:
        info["status"] = "busy"
        if st != "busy":
            info["phase"] = st  # shell, waiting 등 세부 상태
    if s.get("statusUpdatedAt"):
        info["status_since"] = s["statusUpdatedAt"] / 1000
    tpath = claude_project_dir(info["cwd"]) / f"{s.get('sessionId')}.jsonl"
    t = cached(tpath, parse_claude_transcript) if tpath.exists() else None
    if t and "error" not in t:
        info["title"] = t.get("title")
        info["last_prompt"] = t.get("last_prompt")
        info["task"] = short(t.get("last_prompt"), 300)
        info["activity"] = t.get("activity")
        info["last_activity"] = t.get("last_ts")
        if t.get("model"):
            info["model"] = t["model"]
        if t.get("effort"):
            info["effort"] = t["effort"]
        if t.get("ctx_used") is not None:
            info["ctx_used"] = t["ctx_used"]
            info["ctx_window"] = CLAUDE_CONTEXT_WINDOW
    if s.get("sessionId"):
        info["subagents"] = claude_subagents(info["cwd"], s["sessionId"], info["started"])
    if not info.get("model"):
        settings = cached(CLAUDE_DIR / "settings.json", read_json_parser) or {}
        info["model"] = settings.get("model")
        info["effort"] = info.get("effort") or settings.get("effortLevel")


# ---------------------------------------------------------------- Codex

def parse_codex_rollout(path):
    out = {}
    meta = None
    turn_ctx = None
    tok = None
    state = None
    last_ts = None
    acts = []
    task = None
    spawns = {}
    for d in tail_lines(path):
        t = d.get("type")
        pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if t == "response_item":
            ts = iso_to_epoch(d.get("timestamp"))
            pt = pl.get("type")
            if pt == "message":
                text = " ".join(c.get("text", "") for c in pl.get("content") or [] if isinstance(c, dict))
                if pl.get("role") == "assistant":
                    push(acts, ts, "say", short(text))
                elif pl.get("role") == "user" and text.strip() and not text.lstrip().startswith("<"):
                    task = short(text, 300)
            elif pt in ("custom_tool_call", "function_call", "local_shell_call"):
                inp = pl.get("input") or pl.get("arguments") or pl.get("action")
                if isinstance(inp, str):
                    try:
                        inp = json.loads(inp)
                    except Exception:
                        pass
                push(acts, ts, "tool", tool_summary(pl.get("name") or pt, inp))
                if pl.get("name") == "spawn_agent" and isinstance(inp, dict) and inp.get("task_name"):
                    msg = str(inp.get("message") or "")
                    if msg and not msg.startswith("gAAAA"):   # 암호화된 지시는 버린다
                        spawns[inp["task_name"]] = short(msg, 300)
        if t == "session_meta":
            meta = pl
        elif t == "turn_context":
            turn_ctx = pl
        elif t == "event_msg":
            et = pl.get("type")
            if et == "token_count" and pl.get("info"):
                tok = pl["info"]
            elif et in ("task_started", "turn_started"):
                state = "busy"
            elif et in ("task_complete", "turn_complete", "turn_aborted"):
                state = "idle"
        if d.get("timestamp"):
            last_ts = d["timestamp"]
    if meta is None or turn_ctx is None:  # 앞부분에 있을 수 있으므로 전체에서 찾는다
        with open(path, "rb") as f:
            for raw in f:
                if meta is None and b'"session_meta"' in raw[:200]:
                    meta = json.loads(raw).get("payload")
                elif turn_ctx is None and b'"turn_context"' in raw[:200]:
                    turn_ctx = json.loads(raw).get("payload")
                if meta is not None and turn_ctx is not None:
                    break
    if meta:
        src = meta.get("source")
        sub = src.get("subagent") if isinstance(src, dict) else None
        if sub is not None:
            spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
            out["subagent"] = True
            if spawn:   # 사람이 시킨 작업을 나눠 받은 서브에이전트 (guardian 같은 내부 검토용은 제외)
                out["parent_id"] = spawn.get("parent_thread_id")
                out["nickname"] = spawn.get("agent_nickname")
                out["agent_path"] = spawn.get("agent_path")
                out["role"] = spawn.get("agent_role")
        out["session_id"] = meta.get("id")
        out["cwd"] = meta.get("cwd")
        out["started"] = iso_to_epoch(meta.get("timestamp"))
        out["version"] = meta.get("cli_version")
    if turn_ctx:
        out["model"] = turn_ctx.get("model")
        out["effort"] = turn_ctx.get("effort") or (
            (turn_ctx.get("collaboration_mode") or {}).get("settings", {}).get("reasoning_effort"))
    if tok:
        last = tok.get("last_token_usage") or {}
        out["ctx_used"] = last.get("total_tokens") or last.get("input_tokens")
        out["ctx_window"] = tok.get("model_context_window")
    out["state"] = state
    out["last_ts"] = iso_to_epoch(last_ts)
    out["activity"] = acts
    out["task"] = task
    out["spawns"] = spawns
    return out


def codex_recent_rollouts(since):
    """since 이후 수정된 rollout 파일 [(mtime, path, parsed)]."""
    out = []
    for f in glob.glob(str(CODEX_DIR / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")):
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if mt >= since - 5:
            r = cached(f, parse_codex_rollout)
            if r and "error" not in r:
                out.append((mt, f, r))
    return out


def codex_rollout_for(p, start_epoch):
    """부모 세션의 rollout. 서브에이전트·guardian 도 같은 프로세스에서 rollout 을 열어 두므로 그것들은 건너뛴다."""
    # 1) 열려 있는 rollout 파일
    best = None
    try:
        for fd in os.listdir(f"/proc/{p['pid']}/fd"):
            try:
                tgt = os.readlink(f"/proc/{p['pid']}/fd/{fd}")
            except OSError:
                continue
            if "/.codex/sessions/" in tgt and tgt.endswith(".jsonl"):
                r = cached(tgt, parse_codex_rollout) or {}
                if r.get("subagent"):
                    continue
                mt = os.path.getmtime(tgt)
                if best is None or mt > best[0]:
                    best = (mt, tgt)
    except OSError:
        pass
    if best:
        return best[1]
    # 2) 프로세스 시작 이후 수정된 rollout 중 cwd 가 같은 것
    for mt, f, r in codex_recent_rollouts(start_epoch):
        if r.get("subagent") or r.get("cwd") != p["cwd"]:
            continue
        if best is None or mt > best[0]:
            best = (mt, f)
    return best[1] if best else None


def codex_subagents(parent_id, since, spawns=None):
    out = []
    for mt, f, r in codex_recent_rollouts(since):
        if r.get("parent_id") != parent_id:
            continue
        path = (r.get("agent_path") or "").rstrip("/").split("/")[-1]
        if not r.get("task") and spawns:
            r = {**r, "task": spawns.get(path) or spawns.get(r.get("agent_path"))}
        out.append({
            "id": r.get("session_id"),
            "name": " · ".join(x for x in (r.get("nickname"), path) if x) or None,
            "type": r.get("role") or "subagent",
            "task": r.get("task"),
            "model": r.get("model"),
            "effort": r.get("effort"),
            "ctx_used": r.get("ctx_used"),
            "ctx_window": r.get("ctx_window"),
            "started": r.get("started") or mt,
            "updated": r.get("last_ts") or mt,
            "done": r.get("state") == "idle",
            "tools": sum(1 for a in r.get("activity") or [] if a["kind"] == "tool"),
            "activity": r.get("activity"),
        })
    return out


def codex_thread_names():
    path = CODEX_DIR / "session_index.jsonl"
    return cached(path, _parse_codex_index) or {}


def _parse_codex_index(path):
    names = {}
    for d in tail_lines(path, 1024 * 1024):
        if d.get("id"):
            names[d["id"]] = d.get("thread_name")
    return names


def enrich_codex(p, info, start_epoch):
    argv = p["argv"]
    info["model"] = arg_value(argv, "-m", "--model")
    info["effort"] = codex_config_arg(argv, "model_reasoning_effort")
    info["mode"] = "headless" if "exec" in argv else "interactive"
    if "--ephemeral" in argv:
        info["note"] = "ephemeral (세션 기록 없음)"
    if info["mode"] == "headless":
        info["status"] = "busy"
    path = codex_rollout_for(p, start_epoch)
    if not path:
        if not info.get("model"):
            cfg = CODEX_DIR / "config.toml"
            try:
                txt = cfg.read_text()
                m = re.search(r'^model\s*=\s*"([^"]+)"', txt, re.M)
                e = re.search(r'^model_reasoning_effort\s*=\s*"([^"]+)"', txt, re.M)
                info["model"] = m and m.group(1)
                info["effort"] = info["effort"] or (e and e.group(1))
            except OSError:
                pass
        return
    r = cached(path, parse_codex_rollout) or {}
    for k in ("session_id", "version", "model", "effort", "ctx_used", "ctx_window", "activity", "task"):
        if r.get(k):
            info[k] = r[k]
    if r.get("state"):
        info["status"] = r["state"]
    info["last_activity"] = r.get("last_ts")
    info["title"] = codex_thread_names().get(r.get("session_id"))
    if r.get("session_id"):
        info["subagents"] = codex_subagents(r["session_id"], start_epoch, r.get("spawns"))


# ---------------------------------------------------------------- Grok

def parse_grok_events(path):
    state = None
    phase = None
    since = None
    last_ts = None
    for d in tail_lines(path, 1024 * 1024):
        t = d.get("type")
        if t == "turn_started":
            state, since = "busy", d.get("ts")
        elif t == "turn_ended":
            state, since = "idle", d.get("ts")
        elif t == "phase_changed":
            phase = d.get("phase")
        last_ts = d.get("ts") or last_ts
    return {"state": state, "phase": phase, "since": iso_to_epoch(since), "last_ts": iso_to_epoch(last_ts)}


def parse_grok_updates(path):
    acts = []
    for d in tail_lines(path, 512 * 1024):
        u = (d.get("params") or {}).get("update") or {}
        kind = u.get("sessionUpdate")
        ts = d.get("timestamp")
        if kind == "tool_call_update" and u.get("title") and u.get("status") in (None, "in_progress", "pending"):
            if not acts or acts[-1]["text"] != short(u["title"], 140):
                push(acts, ts, "tool", short(u["title"], 140))
        elif kind == "agent_message_chunk":
            text = (u.get("content") or {}).get("text")
            if text and text.strip():
                push(acts, ts, "say", short(text))
    return {"activity": acts}


def parse_grok_task(path):
    task = None
    for d in tail_lines(path, 1024 * 1024):
        if d.get("type") == "user":
            c = d.get("content")
            text = " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(c or "")
            text = re.sub(r"</?user_query>", "", text)
            if text.strip():
                task = short(text, 300)
    return {"task": task}


def grok_subagents(sdir, since):
    out = []
    for f in (sdir / "subagents").glob("*/meta.json"):
        m = cached(f, read_json_parser)
        if not isinstance(m, dict):
            continue
        started = iso_to_epoch(m.get("started_at"))
        if started and started < since - 5:
            continue
        ended = iso_to_epoch(m.get("completed_at"))
        st = (m.get("status") or "").lower()
        # 자식 세션의 기록이 있으면 컨텍스트·최근 활동을 더 읽는다
        child = None
        if m.get("child_cwd") and m.get("child_session_id"):
            child = GROK_DIR / "sessions" / urllib.parse.quote(m["child_cwd"], safe="") / m["child_session_id"]
        sig = (cached(child / "signals.json", read_json_parser) or {}) if child else {}
        acts = ((cached(child / "updates.jsonl", parse_grok_updates) or {}).get("activity") if child else None) or []
        try:
            updated = max(x.stat().st_mtime for x in [f, *(child.glob("*") if child and child.exists() else [])])
        except (OSError, ValueError):
            updated = None
        out.append({
            "id": m.get("subagent_id") or f.parent.name,
            "name": m.get("description") or m.get("subagent_type"),
            "type": m.get("subagent_type"),
            "task": short(m.get("prompt"), 300),
            "model": m.get("effective_model_id"),
            "effort": None,
            "ctx_used": sig.get("contextTokensUsed"),
            "ctx_window": sig.get("contextWindowTokens"),
            "started": started,
            "updated": ended or updated,
            "done": bool(ended) or st in ("completed", "failed", "cancelled", "canceled", "error", "aborted"),
            "tools": m.get("tool_calls"),
            "activity": acts,
        })
    return out


def enrich_grok(p, info):
    argv = p["argv"]
    info["model"] = arg_value(argv, "-m", "--model")
    info["effort"] = arg_value(argv, "--reasoning-effort")
    headless = any(a in argv for a in ("-p", "--prompt", "--prompt-file"))
    info["mode"] = "headless" if headless else "interactive"
    if headless:
        info["status"] = "busy"
    active = cached(GROK_DIR / "active_sessions.json", read_json_parser) or []
    sess = next((s for s in active if s.get("pid") == p["pid"]
                 or s.get("pid_start") == p["starttime"]), None)
    sdir = None
    if sess:
        info["session_id"] = sess.get("session_id")
        info["cwd"] = sess.get("cwd") or info["cwd"]
        sdir = GROK_DIR / "sessions" / urllib.parse.quote(info["cwd"], safe="") / sess["session_id"]
    elif info["cwd"]:
        # active_sessions 에 없으면(헤드리스 등) cwd 의 최신 세션 중 프로세스 시작 이후 것
        base = GROK_DIR / "sessions" / urllib.parse.quote(info["cwd"], safe="")
        start = info["started"]
        best = None
        for d in base.glob("*/summary.json"):
            mt = d.stat().st_mtime
            if mt >= start - 5 and (best is None or mt > best[0]):
                best = (mt, d.parent)
        sdir = best[1] if best else None
        if sdir:
            info["session_id"] = sdir.name
    if not sdir or not sdir.exists():
        return
    summ = cached(sdir / "summary.json", read_json_parser) or {}
    sig = cached(sdir / "signals.json", read_json_parser) or {}
    ev = cached(sdir / "events.jsonl", parse_grok_events) or {}
    info["title"] = summ.get("generated_title") or summ.get("session_summary")
    info["model"] = summ.get("current_model_id") or info["model"]
    info["effort"] = summ.get("reasoning_effort") or info["effort"]
    if sig.get("contextWindowTokens"):
        info["ctx_used"] = sig.get("contextTokensUsed")
        info["ctx_window"] = sig.get("contextWindowTokens")
    if ev.get("state"):
        info["status"] = ev["state"]
        info["status_since"] = ev.get("since")
    if ev.get("state") == "busy" and ev.get("phase"):
        info["phase"] = ev["phase"]
    info["last_activity"] = ev.get("last_ts")
    info["activity"] = (cached(sdir / "updates.jsonl", parse_grok_updates) or {}).get("activity")
    info["task"] = (cached(sdir / "chat_history.jsonl", parse_grok_task) or {}).get("task")
    info["subagents"] = grok_subagents(sdir, info["started"])
    usage = cached(sdir / "usage.json", read_json_parser) or {}
    ticks = (usage.get("session") or {}).get("costUsdTicks")
    if ticks:
        info["cost_usd"] = ticks / 1e10


# ---------------------------------------------------------------- 수집

def tidy_subagents(subs, now):
    out = []
    for a in subs or []:
        upd = a.get("updated") or a.get("started") or 0
        if not a.get("done") and now - upd > SUB_STALE:
            a["done"] = True
        if a["done"] and now - upd > SUB_KEEP:
            continue
        a["status"] = "done" if a["done"] else "busy"
        del a["done"]
        out.append(a)
    out.sort(key=lambda a: (a["status"] != "busy", -(a.get("started") or 0)))
    return out


def collect():
    found, procs = scan()
    children = {}
    for q in procs.values():
        children.setdefault(q["ppid"], []).append(q)
    sess_idx = claude_session_index()
    now = time.time()
    rows = []
    for p, kind in found:
        start = BTIME + p["starttime"] / CLK_TCK
        info = {
            "kind": kind,
            "pid": p["pid"],
            "started": start,
            "elapsed": now - start,
            "cwd": p["cwd"],
            "tty": tty_name(p["pid"]),
            "rss": p["rss"],
            "cpu": round(cpu_percent(p), 1),
            "proc_state": p["state"],
            "status": "unknown",
            "cmd": " ".join(p["argv"])[:400],
            "children": len(children.get(p["pid"], [])),
        }
        try:
            if kind == "claude":
                enrich_claude(p, info, sess_idx)
            elif kind == "codex":
                enrich_codex(p, info, start)
            elif kind == "grok":
                enrich_grok(p, info)
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
        if info["proc_state"] == "T":
            info["status"] = "stopped"
        info["subagents"] = tidy_subagents(info.get("subagents"), now)
        rows.append(info)
    order = {"busy": 0, "unknown": 1, "idle": 2, "stopped": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 1), r["kind"], -r["started"]))
    return {"now": now, "host": os.uname().nodename, "sessions": scrub(rows)}


# ---------------------------------------------------------------- HTTP

HERE = Path(__file__).resolve().parent
PAGES = {"/": "list.html", "/studio": "studio.html", "/hwasan": "hwasan.html"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/sessions":
            body = json.dumps(collect(), ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        elif path in PAGES:
            body = (HERE / PAGES[path]).read_bytes()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def tailscale_ip():
    try:
        return subprocess.check_output(["tailscale", "ip", "-4"], text=True, timeout=5).split()[0]
    except Exception:
        return None


SERVICE = "agent-office"


def install_service(args):
    """현재 스크립트 위치와 인터프리터로 systemd 사용자 유닛을 만들고 켠다."""
    import shlex
    import sys
    cmd = [sys.executable, str(Path(__file__).resolve()), "--port", str(args.port)]
    if args.host:
        cmd += ["--host", args.host]
    unit = HOME / ".config/systemd/user" / f"{SERVICE}.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"""[Unit]
Description=AI session monitor (claude/codex/grok)
After=network-online.target tailscaled.service

[Service]
ExecStart={shlex.join(cmd)}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""")
    print(f"작성: {unit}")
    for c in (["daemon-reload"], ["enable", "--now", SERVICE], ["restart", SERVICE]):
        subprocess.run(["systemctl", "--user", *c], check=True)
    print(f"등록 완료. 상태 확인: systemctl --user status {SERVICE}")
    print("로그아웃 후에도 계속 돌게 하려면: sudo loginctl enable-linger $USER")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", help="바인드 주소 (기본: tailscale IPv4, 실패 시 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--dump", action="store_true", help="JSON 한 번 출력하고 종료")
    ap.add_argument("--install-service", action="store_true", help="systemd 사용자 서비스로 등록하고 시작")
    a = ap.parse_args()
    if a.install_service:
        install_service(a)
        return
    if a.dump:
        print(json.dumps(collect(), ensure_ascii=False, indent=2))
        return
    host = a.host or tailscale_ip() or "127.0.0.1"
    srv = ThreadingHTTPServer((host, a.port), Handler)
    print(f"agent-office: http://{host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
