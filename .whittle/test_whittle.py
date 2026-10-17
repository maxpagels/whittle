"""Tests for whittle.py's automatic rewards: retention of Claude's edits and friction.

    uv run --with pytest --with 'gittins>=2' pytest -q .whittle/test_whittle.py
"""

import contextlib, importlib.util, io, json, shutil, time
from pathlib import Path

import pytest

SRC = Path(__file__).parent
HOUR = 3600


class Clock:
    def __init__(self, t): self.t = t
    def time(self): return self.t
    def localtime(self): return time.localtime(self.t)


@pytest.fixture
def w(tmp_path):
    home = tmp_path / "whittle"
    home.mkdir()
    shutil.copy(SRC / "whittle.py", home / "whittle.py")
    shutil.copytree(SRC / "arms", home / "arms")
    spec = importlib.util.spec_from_file_location("whittle_test", home / "whittle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time = Clock(1_800_000_000.0)
    return mod


def run(w, event, **fields):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        w.HOOKS[event]({"hook_event_name": event, **fields})
    return out.getvalue()


def start(w, sid, transcript=None):
    return run(w, "SessionStart", session_id=sid, model="m",
               transcript_path=str(transcript) if transcript else None)


def write(w, sid, path, content):
    path.write_text(content)
    run(w, "PostToolUse", session_id=sid, tool_name="Write",
        tool_input={"file_path": str(path), "content": content})


def edit(w, sid, path, old, new):
    path.write_text(path.read_text().replace(old, new))
    run(w, "PostToolUse", session_id=sid, tool_name="Edit",
        tool_input={"file_path": str(path), "old_string": old, "new_string": new})


def settle_later(w, hours=9):
    w.time.t += hours * HOUR
    start(w, f"later-{w.time.t}")


def decision(w, sid):
    return json.loads((w.HOME / "sessions" / sid).read_text())["decision"]


def resolutions(w, sid):
    events = [json.loads(line) for line in (w.HOME / "log.jsonl").read_text().splitlines()]
    return [e for e in events if e["event"] == "resolution" and e["decision_id"] == decision(w, sid)]


def signals(w, sid):
    rows = [json.loads(line) for line in (w.HOME / "signals.jsonl").read_text().splitlines()]
    return [r for r in rows if r["session"] == sid]


def transcript(tmp_path, *entries):
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def prompt(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def interrupt(text="[Request interrupted by user]"):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def denial():
    return {"type": "user", "toolUseResult": "User rejected tool use", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "is_error": True,
         "content": "The user doesn't want to proceed with this tool use. The tool use was rejected"}]}}


def test_kept_share_of_claudes_lines_becomes_the_reward(w, tmp_path):
    src = tmp_path / "app.py"
    start(w, "s1")
    write(w, "s1", src, "def main():\n    items = load()\n    total = sum(items)\n    return total\n")
    src.write_text("def main():\n    items = load()\n    return 0\n")  # the user keeps half of it

    settle_later(w)

    [res] = resolutions(w, "s1")
    assert res["kind"] == "rewarded" and res["reward"] == 0.5
    [row] = signals(w, "s1")
    assert row["retention"] == 0.5 and row["lines"] == 4 and row["used"] == "proxy"


def test_claude_reworking_its_own_lines_is_not_lost(w, tmp_path):
    src = tmp_path / "app.py"
    start(w, "s1")
    write(w, "s1", src, "def main():\n    return compute()\n")
    edit(w, "s1", src, "def main():\n    return compute()", "def main():\n    return compute(items)")

    settle_later(w)

    [row] = signals(w, "s1")
    assert row["retention"] == 1.0 and row["lines"] == 2


def test_sessions_store_hashes_not_code(w, tmp_path):
    src = tmp_path / "app.py"
    start(w, "s1")
    write(w, "s1", src, "def main():\n    return compute()\n")

    stored = (w.HOME / "sessions" / "s1").read_text()
    assert "compute" not in stored
    [lines] = json.loads(stored)["lines"].values()
    assert len(lines) == 2 and all(len(k) == 16 for k in lines)


def test_sessions_recorded_as_raw_text_still_score(w, tmp_path):
    src = tmp_path / "app.py"
    src.write_text("def main():\n    return compute()\n")
    start(w, "s1")
    path = w.HOME / "sessions" / "s1"
    session = json.loads(path.read_text())
    session["lines"] = {str(src): {"def main():": 1, "return compute()": 1}}
    path.write_text(json.dumps(session))
    edit(w, "s1", src, "return compute()", "return compute(items)")  # replaces a line recorded raw

    settle_later(w)

    [row] = signals(w, "s1")
    assert row["retention"] == 1.0 and row["lines"] == 2


def test_trivial_lines_are_ignored(w, tmp_path):
    src = tmp_path / "app.js"
    start(w, "s1")
    write(w, "s1", src, "if (ready) {\n  launch();\n}\n\n")

    settle_later(w)

    assert signals(w, "s1")[0]["lines"] == 2


def test_friction_counts_interrupts_denials_and_corrections(w, tmp_path):
    path = transcript(
        tmp_path,
        prompt("no idea why the parser breaks, fix it"),  # first prompt is never a correction
        interrupt(),
        denial(), interrupt("[Request interrupted by user for tool use]"),  # one denial, not also an interrupt
        prompt("No, revert that"),
        prompt("looks good, now add tests"),
        prompt("no", isMeta=True),
        prompt("<command-name>/clear</command-name>"),
        {**prompt("no"), "isSidechain": True},
    )
    assert w.friction(path) == {"interrupts": 1, "denials": 1, "corrections": 1}


def test_friction_lowers_the_reward(w, tmp_path):
    path = transcript(tmp_path, prompt("explain this"), interrupt(), interrupt())
    start(w, "s1", path)

    settle_later(w)

    [res] = resolutions(w, "s1")
    assert res["reward"] == pytest.approx(0.5 - 2 * w.FRICTION_WEIGHT)


def test_session_without_signal_is_left_to_expire(w, tmp_path):
    start(w, "s1", transcript(tmp_path, prompt("what does this do?")))

    settle_later(w)
    [row] = signals(w, "s1")
    assert row["used"] == "none" and row["reward"] is None
    assert resolutions(w, "s1") == []

    settle_later(w, hours=16)
    [res] = resolutions(w, "s1")
    assert res["kind"] == "expired" and res["reward"] == 0.5


def test_rating_wins_over_the_proxy(w, tmp_path):
    src = tmp_path / "app.py"
    start(w, "s1")
    write(w, "s1", src, "def main():\n    return compute()\n")
    src.unlink()
    run(w, "UserPromptSubmit", session_id="s1", prompt="+")

    settle_later(w)

    [res] = resolutions(w, "s1")
    assert res["reward"] == 1.0
    [row] = signals(w, "s1")
    assert row["rating"] == 1.0 and row["retention"] == 0.0 and row["used"] == "rating"


def test_rating_after_automatic_score_is_refused(w, tmp_path):
    src = tmp_path / "app.py"
    start(w, "s1")
    write(w, "s1", src, "def main():\n    return compute()\n")
    settle_later(w)

    reply = json.loads(run(w, "UserPromptSubmit", session_id="s1", prompt="+"))
    assert reply["reason"] == "whittle: this session was already scored automatically"


def test_sessions_settle_once(w, tmp_path):
    src = tmp_path / "app.py"
    start(w, "s1")
    write(w, "s1", src, "def main():\n    return compute()\n")
    settle_later(w)
    settle_later(w)

    assert len(signals(w, "s1")) == 1


def snapshot(home):
    return {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}


def test_status_on_a_fresh_install(w):
    before = snapshot(w.HOME)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        w.status()
    assert "no sessions yet" in out.getvalue()
    assert snapshot(w.HOME) == before


def test_status_counts_each_way_a_session_ends(w, tmp_path):
    t0 = w.time.t
    start(w, "rated")
    run(w, "UserPromptSubmit", session_id="rated", prompt="+")
    w.time.t = t0 + 1 * HOUR
    start(w, "auto")
    write(w, "auto", tmp_path / "app.py", "def main():\n    return compute()\n")
    w.time.t = t0 + 2 * HOUR
    start(w, "quiet", transcript(tmp_path, prompt("what does this do?")))
    w.time.t = t0 + 11 * HOUR
    start(w, "pending")  # settles rated, auto, quiet
    w.time.t = t0 + 27 * HOUR
    start(w, "fresh")  # expires quiet; pending settles with no signal and stays open

    before = snapshot(w.HOME)
    report = w.summary()
    assert snapshot(w.HOME) == before

    arms = report["arms"]
    total = {k: sum(a[k] for a in arms.values()) for k in ("rated", "auto", "expired")}
    assert total == {"rated": 1, "auto": 1, "expired": 1}
    assert report["open"] == 2  # open sessions are counted without their arm: one of them is yours
    assert all(set(a) == {"rated", "auto", "expired", "mean"} for a in arms.values())
    arm_of = lambda sid: json.loads((w.HOME / "sessions" / sid).read_text())["arm"]
    assert arms[arm_of("rated")]["rated"] == 1 and arms[arm_of("auto")]["auto"] == 1
    assert arms[arm_of("auto")]["mean"] == 1.0
    assert sum(report["preferred"].values()) == 24
    assert set(report["preferred"]) <= set(arms) | {"no preference"}
    assert report["verdict"].startswith("not enough data")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        w.status()
    text = out.getvalue()
    assert all(arm in text for arm in arms) and "not enough data" in text


def test_old_session_files_still_resume(w):
    (w.HOME / "sessions").mkdir()
    (w.HOME / "sessions" / "old").write_text("whittle:0 tests-first")

    assert start(w, "old") == (w.HOME / "arms" / "tests-first.md").read_text() + "\n"
    settle_later(w)  # and settling skips them rather than failing
    assert (w.HOME / "sessions" / "old").read_text() == "whittle:0 tests-first"
