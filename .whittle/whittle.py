# /// script
# requires-python = ">=3.10"
# dependencies = ["gittins>=2"]
# ///
"""whittle v0: gittins picks an arm at session start. You rate with + or -; sessions you
don't rate are scored from how much of Claude's code survived and how often you pushed back."""

import fcntl, hashlib, json, re, shutil, subprocess, sys, time
from collections import Counter
from pathlib import Path

import gittins

HOME = Path(__file__).parent
HORIZON = 86400.0
SETTLE = 8 * 3600  # score a session this long after it starts; must stay below HORIZON
FRICTION_WEIGHT, FRICTION_CAP = 0.15, 3  # guesses until signals.jsonl has ratings to fit them
MIN_OBSERVED = 50  # scored sessions per arm before status reports an estimate (DESIGN §9.2)
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
    return json.loads((HOME / "sessions" / sid).read_text())


def save_session(sid, session):
    path = HOME / "sessions" / sid
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(sid + ".tmp")
    tmp.write_text(json.dumps(session))
    tmp.replace(path)


def digest(line):
    return hashlib.blake2b(line.encode(), digest_size=8).hexdigest()


def significant(text):
    """Hashes of the lines worth tracking: braces, blanks and other boilerplate survive any edit."""
    lines = (line.strip() for line in text.splitlines())
    return Counter(digest(line) for line in lines if len(line) >= 4 and any(c.isalnum() for c in line))


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
        if session.get("settled") or now - session["t"] < SETTLE:
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
    if res:
        save_session(sid, {**load_session(sid), "rating": reward})
    note = (f"whittle: noted {rating}" if res else
            "whittle: this session was already scored automatically" if session.get("settled") else
            "whittle: this session is already rated")
    print(json.dumps({"decision": "block", "reason": note}))


def preferred(serialized, arms, model):
    """The arm the state exploits at each hour, or "no preference" while scores tie. Read-only."""
    hours = Counter()
    for hour in range(24):
        best = "no preference"
        for k in range(100):
            rec = gittins.decide(gittins.deserialize(serialized), {"model": model, "hour": str(hour)},
                                 [(a, {}) for a in arms], t=0.0, salt=f"status-{k}")
            if abs(rec.propensity - 1 / len(arms)) < 1e-9:
                break  # uniform: every arm scores the same
            if rec.propensity > 0.5:
                best = arms[rec.chosen]
                break
        hours[best] += 1
    return dict(hours)


def summary():
    """Per-arm outcomes, the state's preferences and a verdict, from the log; touches nothing."""
    log, sig = HOME / "log.jsonl", HOME / "signals.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    auto = {r["decision"] for r in map(json.loads, sig.read_text().splitlines()) if r["used"] == "proxy"} \
        if sig.exists() else set()
    decisions = [e for e in events if e["event"] == "decision"]
    resolved = {e["decision_id"]: e for e in events if e["event"] == "resolution"}
    names = sorted({c[0] for e in decisions for c in e["candidates"]} | {p.stem for p in (HOME / "arms").glob("*.md")})
    arms = {a: {"rated": 0, "auto": 0, "expired": 0, "rewards": []} for a in names}
    open_ = 0
    for e in decisions:
        decision_id = e["record"]["decision_id"]
        arm = arms[e["candidates"][e["record"]["chosen"]][0]]
        res = resolved.get(decision_id)
        if res is None:
            open_ += 1  # no arm: the current session is among these, and its arm stays blind
        elif res["kind"] == "expired":
            arm["expired"] += 1
        else:
            arm["auto" if decision_id in auto else "rated"] += 1
            arm["rewards"].append(res["reward"])
    for arm in arms.values():
        rewards = arm.pop("rewards")
        arm["mean"] = sum(rewards) / len(rewards) if rewards else None
    state = HOME / "state"
    prefs = preferred(state.read_text(), names, decisions[-1]["context"]["model"]) if state.exists() and decisions else {}
    fewest = min(names, key=lambda a: arms[a]["rated"] + arms[a]["auto"]) if names else None
    seen = arms[fewest]["rated"] + arms[fewest]["auto"] if fewest else 0
    verdict = (f"not enough data: {MIN_OBSERVED} scored sessions per arm needed, {fewest} has {seen}"
               if seen < MIN_OBSERVED else "enough scored sessions per arm for an off-policy estimate")
    return {"sessions": len(decisions), "open": open_, "arms": arms, "preferred": prefs, "verdict": verdict}


def status(sid=None):
    if sid:
        path = HOME / "sessions" / sid
        arm = load_session(sid)["arm"] if path.exists() else None
        print(f"this session: {arm} (arms/{arm}.md)\n" if arm else "this session: not tracked by whittle\n")
    report = summary()
    arms = report["arms"]
    if not report["sessions"]:
        print("whittle: no sessions yet")
        return
    print(f"{report['sessions']} sessions, {report['open']} still open (their arms are hidden until scored)\n")
    cols = ("rated", "auto", "expired")
    print(f"{'arm':<14}" + "".join(f"{c:>9}" for c in cols) + f"{'mean':>7}")
    for name, a in arms.items():
        mean = "-" if a["mean"] is None else f"{a['mean']:.2f}"
        print(f"{name:<14}" + "".join(f"{a[c]:>9}" for c in cols) + f"{mean:>7}")
    scored = sum(a["rated"] + a["auto"] for a in arms.values())
    expired = sum(a["expired"] for a in arms.values())
    print("\nmean = average reward over rated and auto-scored sessions; expired ones count as missing")
    if expired > scored:
        print(f"warning: more sessions expired ({expired}) than were scored ({scored})")
    prefs = sorted(report["preferred"].items(), key=lambda kv: -kv[1])
    print("prefers: " + (", ".join(f"{a} ({h}/24 hours)" for a, h in prefs) or "no state yet"))
    print(report["verdict"])
    cli = shutil.which("gittins")
    if not report["verdict"].startswith("not enough") and cli:
        out = subprocess.run([cli, "eval", "--log", str(HOME / "log.jsonl"), "--bits", "18", "--epsilon", "0.2"],
                             capture_output=True, text=True)
        print("\ngittins eval (the learned policy, estimated on logged sessions)\n" + (out.stdout + out.stderr).rstrip())


HOOKS = {"SessionStart": session_start, "UserPromptSubmit": prompt, "PostToolUse": post_tool_use}

if __name__ == "__main__":
    if sys.argv[1:2] == ["status"]:
        status(*sys.argv[2:3])
        sys.exit()
    try:
        event = json.load(sys.stdin)
        HOOKS.get(event["hook_event_name"], lambda e: None)(event)
    except Exception:
        pass  # fail open: no injection, nothing logged
