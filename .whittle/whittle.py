# /// script
# requires-python = ">=3.10"
# dependencies = ["gittins>=2"]
# ///
"""whittle v0: gittins picks an arm at session start. You rate with + or -; sessions you
don't rate are scored from how much of Claude's code survived and how often you pushed back."""

import fcntl, json, re, sys, time
from collections import Counter
from pathlib import Path

import gittins

HOME = Path(__file__).parent
HORIZON = 86400.0
SETTLE = 8 * 3600  # score a session this long after it starts; must stay below HORIZON
FRICTION_WEIGHT, FRICTION_CAP = 0.15, 3  # guesses until signals.jsonl has ratings to fit them
CORRECTION = re.compile(r"(no|nope|wrong|revert|undo|stop|don'?t|that'?s not|that is not|not what)\b", re.I)


def with_state(step):
    """Run `step(state) -> (result, log_items)` under the lock; log, then save."""
    with open(HOME / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = HOME / "state"
        state = (gittins.deserialize(path.read_text()) if path.exists()
                 else gittins.create(bits=18, horizon=HORIZON, epsilon=0.2, default_reward=0.5))
        result, items = step(state)
        with open(HOME / "log.jsonl", "a") as f:
            for item in items:
                f.write(gittins.log_line(item) + "\n")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(gittins.serialize(state))
        tmp.replace(path)
        return result


def load_session(sid):
    text = (HOME / "sessions" / sid).read_text()
    if not text.startswith("{"):  # pre-JSON format: "decision_id arm", never settled
        decision, arm = text.split()
        return {"decision": decision, "arm": arm}
    return json.loads(text)


def save_session(sid, session):
    path = HOME / "sessions" / sid
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(sid + ".tmp")
    tmp.write_text(json.dumps(session))
    tmp.replace(path)


def significant(text):
    """Lines worth tracking: braces, blanks and other boilerplate survive any edit."""
    lines = (line.strip() for line in text.splitlines())
    return Counter(line for line in lines if len(line) >= 4 and any(c.isalnum() for c in line))


def retention(session):
    """Share of the lines Claude wrote that are still in their files; None if it wrote none."""
    kept = total = 0
    for name, lines in session.get("lines", {}).items():
        path = Path(name)
        now = significant(path.read_text(errors="replace")) if path.is_file() else Counter()
        kept += sum((Counter(lines) & now).values())
        total += sum(lines.values())
    return (kept / total if total else None), total


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or () if isinstance(b, dict))


def friction(transcript):
    """Interrupts, rejected tool calls and correcting prompts in a transcript; None if unreadable."""
    if not transcript or not Path(transcript).is_file():
        return None
    counts, prompts = {"interrupts": 0, "denials": 0, "corrections": 0}, 0
    for line in open(transcript, errors="replace"):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        results = [b for b in content if b.get("type") == "tool_result"] if isinstance(content, list) else []
        if results:
            counts["denials"] += sum(bool(b.get("is_error")) and text_of(b.get("content")).startswith(
                "The user doesn't want to proceed") for b in results)
            continue
        text = text_of(content).strip()
        if text == "[Request interrupted by user]":
            counts["interrupts"] += 1
        elif text and not text.startswith(("<", "[Request interrupted")):
            counts["corrections"] += prompts > 0 and bool(CORRECTION.match(text))
            prompts += 1
    return counts


def score(session):
    """The session's signals, and the reward they imply (None when there is no signal)."""
    kept, lines = retention(session)
    pushback = friction(session.get("transcript"))
    signals = {"retention": kept, "lines": lines, **(pushback or dict.fromkeys(("interrupts", "denials", "corrections")))}
    total = min(sum(pushback.values()), FRICTION_CAP) if pushback else 0
    if kept is None and not total:
        return signals, None
    return signals, max(0.0, (0.5 if kept is None else kept) - FRICTION_WEIGHT * total)


def settle(state, now):
    """Score sessions SETTLE old, once; learn the score unless the user rated the session."""
    items = []
    for path in sorted((HOME / "sessions").glob("*")):
        if path.suffix == ".tmp":
            continue
        session = load_session(path.name)
        if "t" not in session or session.get("settled") or now - session["t"] < SETTLE:
            continue
        signals, reward = score(session)
        used = "rating" if "rating" in session else "none"
        if used == "none" and reward is not None and now - session["t"] < HORIZON:
            res = gittins.learn(state, session["decision"], reward=reward, t=now)
            if res:
                items.append(res)
                used = "proxy"
        save_session(path.name, {**session, "settled": True})
        with open(HOME / "signals.jsonl", "a") as f:
            f.write(json.dumps({"session": path.name, "decision": session["decision"], "arm": session["arm"],
                                "rating": session.get("rating"), **signals, "reward": reward, "used": used}) + "\n")
    return items


def session_start(event):
    sid = event["session_id"]
    if (HOME / "sessions" / sid).exists():  # resume / clear / compact: re-inject the same arm
        arm = load_session(sid)["arm"]
    else:
        arms = sorted(p.stem for p in (HOME / "arms").glob("*.md"))
        context = {"model": event.get("model"), "hour": str(time.localtime().tm_hour)}

        def step(state):
            now = time.time()
            resolved = [*settle(state, now), *gittins.expire(state, t=now)]
            record = gittins.decide(state, context, [(a, {}) for a in arms], t=now, salt="whittle")
            save_session(sid, {"decision": record.decision_id, "arm": arms[record.chosen], "t": now,
                               "transcript": event.get("transcript_path"), "lines": {}})
            return arms[record.chosen], (*resolved, record)

        arm = with_state(step)
    print((HOME / "arms" / f"{arm}.md").read_text())


def post_tool_use(event):
    """Track the lines Claude adds; lines it later replaces itself don't count as lost."""
    sid, edit = event["session_id"], event.get("tool_input") or {}
    if not (HOME / "sessions" / sid).exists() or not edit.get("file_path"):
        return
    session = load_session(sid)
    if "t" not in session:
        return
    name = edit["file_path"]
    if event.get("tool_name") == "Write":
        lines = significant(edit.get("content", ""))
    else:
        lines = Counter(session["lines"].get(name, {}))
        for e in edit.get("edits") or [edit]:
            old, new = significant(e.get("old_string", "")), significant(e.get("new_string", ""))
            lines = lines - (old - new) + (new - old)
    session["lines"][name] = dict(lines)
    save_session(sid, session)


def prompt(event):
    rating = event.get("prompt", "").strip()
    sid = event["session_id"]
    if rating not in ("+", "-") or not (HOME / "sessions" / sid).exists():
        return
    session = load_session(sid)
    reward = 1.0 if rating == "+" else 0.0

    def step(state):
        res = gittins.learn(state, session["decision"], reward=reward, t=time.time())
        return res, (res,) if res else ()

    res = with_state(step)
    if res and "t" in session:
        save_session(sid, {**load_session(sid), "rating": reward})
    note = (f"whittle: noted {rating}" if res else
            "whittle: this session was already scored automatically" if session.get("settled") else
            "whittle: this session is already rated")
    print(json.dumps({"decision": "block", "reason": note}))


HOOKS = {"SessionStart": session_start, "UserPromptSubmit": prompt, "PostToolUse": post_tool_use}

if __name__ == "__main__":
    try:
        event = json.load(sys.stdin)
        HOOKS.get(event["hook_event_name"], lambda e: None)(event)
    except Exception:
        pass  # fail open: no injection, nothing logged
