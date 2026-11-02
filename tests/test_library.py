import fcntl, json, logging, multiprocessing, os, shutil, subprocess, sys, textwrap, threading
from collections import Counter

import pytest

import whittle
from whittle import evaluate as ev, store

needs_cli = pytest.mark.skipif(not shutil.which("gittins"), reason="needs the gittins CLI")


def events(data):
    return [json.loads(line) for line in data().splitlines()]


def root():
    return store.location()


def test_default_is_kept_except_for_the_exploration_share():
    picks = Counter(whittle.decide("p", ["a", "b", "c"], epsilon=0.3).action for _ in range(3000))
    assert picks["a"] == pytest.approx(3000 * 0.8, rel=0.05)
    assert picks["b"] == pytest.approx(3000 * 0.1, rel=0.2)


def test_nothing_learns_online():
    for _ in range(300):
        whittle.decide("p", ["a", "b"], epsilon=0.5).learn(success=1 if _ % 2 else 0)
    picks = Counter(whittle.decide("p", ["a", "b"], epsilon=0.2).action for _ in range(1000))
    assert picks["a"] == pytest.approx(900, rel=0.05)


def test_decision_is_committed_before_decide_returns(data):
    d = whittle.decide("p", ["a", "b"])
    assert [e["record"]["decision_id"] for e in events(data)] == [d.id]


def test_ledger_lives_on_disk_not_in_the_process(data):
    d = whittle.decide("p", ["a", "b"])
    code = f"import whittle\nfrom whittle import store\nimport gittins\nprint(whittle.Decision({d.id!r}, 'a', 'p', None).learn(success=1))"
    out = subprocess.run([sys.executable, "-c", code], check=True, env=os.environ, capture_output=True, text=True)
    assert out.stdout.strip() == "1" and ev.load(data())[0].reward == {"success": 1.0}


def test_propensity_matches_what_is_logged(data):
    whittle.decide("p", ["a", "b", "c"], default="b", epsilon=0.3)
    e = events(data)[0]
    action = e["candidates"][e["record"]["chosen"]][0]
    assert e["record"]["propensity"] == pytest.approx(0.8 if action == "b" else 0.1)


def test_epsilon_can_change_while_running(data):
    assert {whittle.decide("p", ["a", "b"], epsilon=1.0).action for _ in range(60)} == {"a", "b"}
    assert {whittle.decide("p", ["a", "b"], epsilon=0).action for _ in range(60)} == {"a"}


def test_epsilon_zero_env_switches_randomization_off(monkeypatch):
    monkeypatch.setenv("WHITTLE_EPSILON", "0")
    assert {whittle.decide("p", ["a", "b"]).action for _ in range(100)} == {"a"}


def test_non_string_actions_come_back_unchanged():
    assert whittle.decide("k", [5, 10], epsilon=0).action == 5
    assert isinstance(whittle.decide("k", [5, 10], default=10, epsilon=0).action, int)


def test_context_never_stores_long_text_or_breaks_on_odd_values(data):
    whittle.decide("p", ["a"], context={"prompt": "x" * 1000, "n": float("nan"), "obj": {"a": [1]}, "ok": "code"})
    ctx = events(data)[0]["context"]
    assert ctx["prompt"].startswith("sha256:") and ctx["obj"].startswith("sha256:")
    assert ctx["n"] is None and ctx["ok"] == "code"


@pytest.mark.parametrize("env", ["s3://bucket", "/dev/null/no"])
def test_store_failure_gives_the_default_and_never_raises(monkeypatch, env, caplog):
    monkeypatch.setenv("WHITTLE_STORE", env)
    with caplog.at_level(logging.WARNING, "whittle"):
        for _ in range(30):
            d = whittle.decide("p", ["a", "b"], epsilon=1.0)
            assert d.action == "a" and d.id is None and d.learn(success=1) == 0
        with whittle.run():
            assert whittle.learn(success=1) == 0
    assert "using the default action" in caplog.text


def test_stuck_lock_holder_cannot_hang_the_agent(monkeypatch):
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.05)
    root().mkdir(parents=True)
    with open(root() / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        d = whittle.decide("p", ["a", "b"], epsilon=1.0)
    assert d.id is None and d.action == "a"
    assert whittle.decide("p", ["a", "b"]).id is not None


def test_bad_arguments_are_programmer_errors():
    with pytest.raises(ValueError):
        whittle.decide("p", [])
    with pytest.raises(ValueError):
        whittle.decide("p", ["a"], default="z")


def test_a_decision_resolves_once_and_first_report_wins(data):
    d = whittle.decide("p", ["a", "b"])
    assert d.learn(success=1) == 1 and d.learn(success=0) == 0
    assert ev.load(data())[0].reward == {"success": 1.0}


def test_bad_rewards_are_dropped_with_a_warning(data, caplog):
    d = whittle.decide("p", ["a"])
    with caplog.at_level(logging.WARNING, "whittle"):
        assert d.learn(success=float("nan")) == 0 and d.learn() == 0
    assert ev.load(data())[0].state == "open"


def test_learn_outside_a_run_warns():
    assert whittle.learn(success=1) == 0


def test_outcome_from_an_unrelated_process_resolves_the_whole_run(data):
    with whittle.run("req-1"):
        whittle.decide("p", ["a", "b"])
        whittle.decide("q", ["c", "d"])
    code = textwrap.dedent("""
        import whittle
        print(whittle.learn(run_id="req-1", success=1))
    """)
    res = subprocess.run([sys.executable, "-c", code], check=True, env=os.environ, capture_output=True, text=True)
    assert res.stdout.strip() == "2"
    assert [d.reward for d in ev.load(data())] == [{"success": 1.0}] * 2


def test_reused_run_id_resolves_only_what_is_open(data):
    with whittle.run("r"):
        whittle.decide("p", ["a"])
        assert whittle.learn(success=1) == 1
        whittle.decide("p", ["a"])
        assert whittle.learn(success=0) == 1
    assert [d.reward["success"] for d in ev.load(data())] == [1, 0]


def test_resolved_runs_leave_no_files():
    with whittle.run("r"):
        whittle.decide("p", ["a"])
        whittle.learn(success=1)
    assert not list((root() / "runs").iterdir())


def fake_clock(monkeypatch):
    now = [1000.0]
    real = store.time
    fake = type("T", (), {"time": staticmethod(lambda: now[0]), "monotonic": real.monotonic, "sleep": real.sleep})
    monkeypatch.setattr(store, "time", fake)
    return now


def test_decisions_expire_through_gittins(monkeypatch, data):
    now = fake_clock(monkeypatch)
    d = whittle.decide("p", ["a"])
    now[0] += store.HORIZON_DAYS * 86400 + 1
    whittle.decide("p", ["a"])
    first = ev.load(data())[0]
    assert first.id == d.id and first.state == "expired" and first.reward == {}
    assert d.learn(success=1) == 0  # too late


def test_clock_stepping_back_keeps_the_log_in_order(monkeypatch, data):
    now = fake_clock(monkeypatch)
    whittle.decide("p", ["a"])
    now[0] -= 500
    whittle.decide("p", ["a"])
    a, b = ev.load(data())
    assert b.t >= a.t


def test_old_run_files_are_swept(monkeypatch):
    now = fake_clock(monkeypatch)
    with whittle.run("r"):
        whittle.decide("p", ["a"])
    run_file = next((root() / "runs").iterdir())
    os.utime(run_file, (0, 0))
    os.utime(root() / "swept", (0, 0))
    now[0] = 86400 * 100.0
    whittle.decide("p", ["a"])
    assert not run_file.exists()


def test_uncommitted_tail_from_a_crash_is_rolled_back(data):
    whittle.decide("p", ["a"])
    with open(store.log_path(root()), "a") as f:
        f.write('{"event":"deci')
    assert len(events(data)) == 1  # readers never see it
    whittle.decide("p", ["a"])
    assert store.log_path(root()).read_bytes() == data() and len(events(data)) == 2


def test_lost_state_starts_a_new_bandit_without_reusing_ids(data):
    first = whittle.decide("p", ["a"])
    (root() / "state.json").unlink()
    second = whittle.decide("p", ["a"])
    assert first.id != second.id and len(events(data)) == 2


def test_log_shorter_than_state_recovers(data, caplog):
    whittle.decide("p", ["a"])
    os.truncate(store.log_path(root()), 0)
    with caplog.at_level(logging.WARNING, "whittle"):
        assert whittle.decide("p", ["a"]).id is not None
    assert "shorter" in caplog.text


def _worker(i):
    for j in range(100):
        with whittle.run(f"run-{i}-{j}"):
            whittle.decide("p", ["a", "b"], context={"i": i})
            whittle.learn(success=j % 2)


def _check_log(data, n):
    decisions = ev.load(data())
    assert len(decisions) == n and len({d.id for d in decisions}) == n
    assert all(d.state == "rewarded" for d in decisions)
    assert [d.t for d in decisions] == sorted(d.t for d in decisions)


def test_many_processes_share_one_bandit(data):
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        pool.map(_worker, range(8))
    _check_log(data, 800)


def test_threads_share_one_bandit(data):
    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _check_log(data, 400)


@needs_cli
def test_real_gittins_verify_accepts_concurrent_traffic(data, monkeypatch):
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        pool.map(_worker, range(4))
    with whittle.run("open-forever"):
        whittle.decide("p", ["a", "b"])
    assert ev.verify(data()) is None


@needs_cli
def test_real_gittins_verify_catches_tampering(data):
    whittle.decide("p", ["a", "b"], epsilon=0.5).learn(success=1)
    lines = data().splitlines(keepends=True)
    assert "rejected" in ev.verify(data() + lines[-1])  # the same resolution twice


def cli_version():
    out = subprocess.run(["gittins", "--version"], capture_output=True, text=True).stdout.split()
    return tuple(int(x) for x in out[1].split("."))


@needs_cli
def test_state_replayed_from_the_log_continues_it_cleanly(data):
    if cli_version() < (2, 1, 0):
        pytest.skip("replay resumes the decision counter from gittins 2.1.0")
    for i in range(20):
        whittle.decide("p", ["a", "b"], epsilon=0.3).learn(success=i % 2)
    saved = json.loads((root() / "state.json").read_text())
    saved["state"] = subprocess.run(["gittins", "replay", "--log", str(store.log_path(root())), "--bits", str(store.BITS),
                                     "--horizon", str(store.HORIZON_DAYS * 86400)],
                                    capture_output=True, text=True, check=True).stdout.strip()
    (root() / "state.json").write_text(json.dumps(saved))
    d = whittle.decide("p", ["a", "b"], epsilon=0.3)
    assert d.id.endswith(":20") and d.learn(success=1) == 1
    assert ev.verify(data()) is None


def test_missing_cli_is_reported(monkeypatch):
    monkeypatch.setattr(ev.shutil, "which", lambda _: None)
    assert "unverified" in ev.verify(b"")


def test_default_location_is_per_project(tmp_path, monkeypatch):
    monkeypatch.delenv("WHITTLE_STORE")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    a, b = tmp_path / "a", tmp_path / "b"
    (a / ".git").mkdir(parents=True)
    (b / ".git").mkdir(parents=True)
    (a / "sub").mkdir()
    monkeypatch.chdir(a / "sub")
    in_a = store.default_location()
    monkeypatch.chdir(b)
    assert in_a != store.default_location() and in_a.parent == tmp_path / "data/whittle"


def test_ephemeral_platform_warns(monkeypatch, caplog):
    monkeypatch.delenv("WHITTLE_STORE")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    with caplog.at_level(logging.WARNING, "whittle"):
        store.location()
    assert "will be lost" in caplog.text


def test_store_files_are_private():
    whittle.decide("p", ["a"])
    assert (root() / "log.jsonl").stat().st_mode & 0o077 == 0
    assert (root() / "state.json").stat().st_mode & 0o077 == 0
