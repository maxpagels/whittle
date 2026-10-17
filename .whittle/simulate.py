# /// script
# requires-python = ">=3.10"
# dependencies = ["gittins>=2"]
# ///
"""Simulate whittle v0 end to end: a fake user on a fake clock, against a scratch copy.

Runs the real session_start / prompt hooks from whittle.py in a temp dir (the live
.whittle/ state is never touched), then checks the log with the gittins CLI.

    uv run --script .whittle/simulate.py [--sessions 600] [--seed 1] [--keep DIR]
"""

import argparse, contextlib, importlib.util, io, json, random, shutil, subprocess, sys, tempfile, time
from collections import Counter
from pathlib import Path

SRC = Path(__file__).parent
P_PLUS = {"baseline": 0.25, "plan-first": 0.5, "tests-first": 0.75}  # chance the user rates +
P_RATE = 0.7  # chance the user rates at all; the rest expire as 0.5
GAP = 2 * 3600  # seconds between sessions
MODEL = "claude-opus-5-5"
BITS = 18  # must match gittins.create(bits=...) in whittle.py


class Clock:
    def __init__(self, t): self.t = t
    def time(self): return self.t
    def localtime(self): return time.localtime(self.t)


def load(home, clock):
    shutil.copy(SRC / "whittle.py", home / "whittle.py")
    shutil.copytree(SRC / "arms", home / "arms")
    spec = importlib.util.spec_from_file_location("whittle_sim", home / "whittle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time = clock
    return mod


def run(fn, event):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(event)
    return out.getvalue()


def greedy(serialized, hour):
    """The arm a state exploits for this hour: decide until a non-exploration pick."""
    import gittins
    arms = sorted(P_PLUS)
    for k in range(100):
        rec = gittins.decide(gittins.deserialize(serialized), {"model": MODEL, "hour": str(hour)},
                             [(a, {}) for a in arms], t=2e9 + k, salt=f"probe-{k}")
        if rec.propensity > 0.5:
            return arms[rec.chosen]


def check(ok, what, failures):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=600)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--keep", type=Path, help="copy the sim's .whittle dir here afterwards")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    home = Path(tempfile.mkdtemp(prefix="whittle-sim-"))
    clock = Clock(1_800_000_000.0)
    w = load(home, clock)
    arm_text = {p.stem: p.read_text() for p in (home / "arms").glob("*.md")}
    by_text = {v: k for k, v in arm_text.items()}
    failures, chosen, rated, resumes_ok, rerate_ok, ignore_ok = [], [], 0, True, True, True

    for i in range(args.sessions):
        sid = f"sim-{i}"
        out = run(w.session_start, {"hook_event_name": "SessionStart", "session_id": sid, "model": MODEL})
        arm = by_text.get(out[:-1])  # print() adds one newline
        chosen.append(arm)

        if i % 10 == 0:  # resume: same arm, no new decision
            resumes_ok &= run(w.session_start, {"hook_event_name": "SessionStart", "session_id": sid, "model": MODEL}) == out
        ignore_ok &= run(w.prompt, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "fix the bug"}) == ""

        clock.t += rng.uniform(60, 3600)
        if rng.random() < P_RATE:
            rating = "+" if rng.random() < P_PLUS[arm] else "-"
            reply = json.loads(run(w.prompt, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": rating}))
            rated += 1
            if reply["reason"] != f"whittle: noted {rating}":
                rerate_ok = False
            if i % 10 == 0:  # second rating must be refused
                again = json.loads(run(w.prompt, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "+"}))
                rerate_ok &= again["reason"] == "whittle: this session is already rated"
        clock.t += GAP

    log = home / "log.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    decisions = [e for e in events if e["event"] == "decision"]
    resolutions = [e for e in events if e["event"] == "resolution"]
    kinds = Counter(e["kind"] for e in resolutions)
    ids = Counter(e["decision_id"] for e in resolutions)

    n, q = len(chosen), max(len(chosen) // 6, 1)
    best = max(P_PLUS, key=P_PLUS.get)
    early = Counter(chosen[:q])[best] / q
    late = Counter(chosen[-q:])[best] / q
    rewards = {a: [] for a in P_PLUS}
    decided = {e["record"]["decision_id"]: e["candidates"][e["record"]["chosen"]][0] for e in decisions}
    for e in resolutions:
        rewards[decided[e["decision_id"]]].append(e["reward"])

    print(f"\nsimulated {n} sessions in {home}")
    print(f"  user P(+): {P_PLUS}, rates {P_RATE:.0%} of sessions\n")
    print(f"  {'arm':<12} {'chosen':>7} {'first 1/6':>10} {'last 1/6':>9} {'mean reward':>12}")
    for a in sorted(P_PLUS):
        r = rewards[a]
        print(f"  {a:<12} {chosen.count(a):>7} {Counter(chosen[:q])[a] / q:>10.0%} "
              f"{Counter(chosen[-q:])[a] / q:>9.0%} {(sum(r) / len(r) if r else float('nan')):>12.2f}")
    print(f"\n  resolutions: {dict(kinds)}\n")

    print("hooks")
    check(None not in chosen, "every session start injected a known arm", failures)
    check(resumes_ok, "resumed sessions re-inject the same arm without a new decision", failures)
    check(ignore_ok, "ordinary prompts pass through untouched", failures)
    check(rerate_ok, "+/- is noted once, a second rating is refused", failures)
    print("logging")
    check(len(decisions) == n, f"one decision per session ({len(decisions)} == {n})", failures)
    check(all(e["bits"] == BITS for e in decisions), f"decisions are logged with bits={BITS}", failures)
    check(kinds.get("rewarded", 0) == rated, f"one rewarded resolution per rating ({kinds.get('rewarded', 0)} == {rated})", failures)
    check(all(c == 1 for c in ids.values()), "no decision resolved twice", failures)
    open_ = n - len(ids)
    check(kinds.get("expired", 0) > 0 and open_ <= 24 * 3600 // GAP + 1,
          f"unrated sessions expire ({kinds.get('expired', 0)} expired, {open_} still inside the horizon)", failures)
    check(all(e["reward"] == 0.5 for e in resolutions if e["kind"] == "expired"), "expired sessions train as 0.5", failures)
    size = (home / "state").stat().st_size
    check(size < 64 * 1024, f"state stays small at bits={BITS} ({size / 1024:.0f} KB < 64 KB)", failures)
    print("learning")
    check(late > 0.6, f"best arm ({best}) dominates late traffic: {early:.0%} -> {late:.0%}", failures)

    print("gittins CLI")
    gittins = shutil.which("gittins")
    if not gittins:
        print("  skip gittins not on PATH")
    else:
        v = subprocess.run([gittins, "verify", "--log", str(log)], capture_output=True, text=True)
        check(v.returncode == 0, "gittins verify passes", failures)
        if v.returncode:
            print(v.stdout + v.stderr)
        r = subprocess.run([gittins, "replay", "--log", str(log), "--bits", str(BITS), "--horizon", "86400",
                            "--default-reward", "0.5", "--epsilon", "0.2"], capture_output=True, text=True)
        # replay drops still-open decisions, so compare policies, not bytes
        same = r.returncode == 0 and all(
            greedy(r.stdout.strip(), h) == greedy((home / "state").read_text(), h) for h in range(24))
        check(same, "gittins replay of the log rebuilds a state with the same policy", failures)
        e = subprocess.run([gittins, "eval", "--log", str(log), "--bits", str(BITS), "--epsilon", "0.2"],
                           capture_output=True, text=True)
        print("\n" + (e.stdout + e.stderr).rstrip())

    if args.keep:
        shutil.copytree(home, args.keep, dirs_exist_ok=True)
    print(f"\n{'PASS' if not failures else f'FAIL ({len(failures)})'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
