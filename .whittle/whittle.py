# /// script
# requires-python = ">=3.10"
# dependencies = ["gittins>=2"]
# ///
"""whittle v0: gittins picks an arm at session start, you rate with + or -."""

import fcntl, json, sys, time
from pathlib import Path

import gittins

HOME = Path(__file__).parent


def with_state(step):
    """Run `step(state) -> (result, log_items)` under the lock; log, then save."""
    with open(HOME / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = HOME / "state"
        state = (gittins.deserialize(path.read_text()) if path.exists()
                 else gittins.create(bits=18, horizon=86400.0, epsilon=0.2, default_reward=0.5))
        result, items = step(state)
        with open(HOME / "log.jsonl", "a") as f:
            for item in items:
                f.write(gittins.log_line(item) + "\n")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(gittins.serialize(state))
        tmp.replace(path)
        return result


def session_start(event):
    session = HOME / "sessions" / event["session_id"]
    if session.exists():  # resume / clear / compact: re-inject the same arm
        arm = session.read_text().split()[1]
    else:
        arms = sorted(p.stem for p in (HOME / "arms").glob("*.md"))
        context = {"model": event.get("model"), "hour": str(time.localtime().tm_hour)}

        def step(state):
            now = time.time()
            expired = gittins.expire(state, t=now)
            record = gittins.decide(state, context, [(a, {}) for a in arms], t=now, salt="whittle")
            session.parent.mkdir(exist_ok=True)
            session.write_text(f"{record.decision_id} {arms[record.chosen]}")
            return arms[record.chosen], (*expired, record)

        arm = with_state(step)
    print((HOME / "arms" / f"{arm}.md").read_text())


def prompt(event):
    rating = event.get("prompt", "").strip()
    session = HOME / "sessions" / event["session_id"]
    if rating not in ("+", "-") or not session.exists():
        return
    decision_id = session.read_text().split()[0]
    reward = 1.0 if rating == "+" else 0.0

    def step(state):
        res = gittins.learn(state, decision_id, reward=reward, t=time.time())
        return res, (res,) if res else ()

    res = with_state(step)
    note = f"whittle: noted {rating}" if res else "whittle: this session is already rated"
    print(json.dumps({"decision": "block", "reason": note}))


if __name__ == "__main__":
    try:
        event = json.load(sys.stdin)
        {"SessionStart": session_start, "UserPromptSubmit": prompt}.get(
            event["hook_event_name"], lambda e: None)(event)
    except Exception:
        pass  # fail open: no injection, nothing logged
