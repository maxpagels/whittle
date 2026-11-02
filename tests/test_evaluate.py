import json, random, subprocess, sys

import pytest

import whittle
from whittle import cli, evaluate as ev, store

pytestmark = pytest.mark.usefixtures("unverified")

TRUE = {"a": 0.2, "b": 0.5, "c": 0.8}


def simulate(n=3000, epsilon=0.4, truth=TRUE, default="a", seed=1):
    """Logged traffic with a known reward per action."""
    rng = random.Random(seed)
    for _ in range(n):
        d = whittle.decide("p", list(truth), context={"tier": "pro"}, default=default, epsilon=epsilon)
        d.learn(success=int(rng.random() < truth[d.action]))
    return log()


def log():
    return store.read_committed(store.location())


def always(action):
    return lambda d: action


def report(data, policy, **kw):
    return ev.evaluate(data, policy, bootstrap=200, **kw)


def test_recovers_each_actions_true_value():
    data = simulate()
    for action, truth in TRUE.items():
        assert report(data, always(action))["values"]["snips"] == pytest.approx(truth, abs=0.06)


def test_ship_for_better_block_for_worse_wait_for_same():
    data = simulate(default="b")
    better, worse, same = (report(data, always(a)) for a in "cab")
    assert (better["verdict"], worse["verdict"], same["verdict"]) == ("ship", "block", "wait")
    assert better["values"]["lift"] == pytest.approx(0.3, abs=0.07)


def test_verdict_is_held_on_tiny_samples():
    rep = report(simulate(n=10, epsilon=0.3), always("c"))
    assert rep["verdict"] == "wait" and any("effective sample size" in h for h in rep["hold"])


def test_verdict_is_held_when_policy_action_was_never_explored():
    rep = report(simulate(n=200, epsilon=0.0), always("c"))
    assert rep["verdict"] == "wait" and any("no chance" in h for h in rep["hold"])


def test_verdict_is_held_when_verify_fails(monkeypatch):
    data = simulate(default="b")
    monkeypatch.setattr(ev, "verify", lambda data: "gittins verify rejected the log: bad")
    rep = report(data, always("c"))
    assert rep["verdict"] == "wait" and any("rejected" in h for h in rep["hold"])


def test_dr_corrects_a_biased_reward_model():
    rep = report(simulate(), always("c"), q=lambda d: {a: 0.3 for a in d.actions})
    assert rep["values"]["dr"] == pytest.approx(0.8, abs=0.07)


def test_action_never_offered_cannot_be_evaluated():
    with pytest.raises(ValueError, match="never offered"):
        report(simulate(n=20), always("z"))


def test_policy_distribution_must_be_valid():
    with pytest.raises(ValueError, match="summing to 1"):
        report(simulate(n=20), lambda d: {"a": 0.5})


def test_objective_reweights_without_new_data():
    for _ in range(200):
        whittle.decide("p", ["a", "b"], epsilon=0.5).learn(success=1, cost=2)
    assert report(log(), always("a"), objective="success - 0.25*cost")["values"]["snips"] == pytest.approx(0.5)


@pytest.mark.parametrize("expr", ["__import__('os').system('x')", "a.b", "a ** 9", "[1]"])
def test_objective_is_arithmetic_only(expr):
    with pytest.raises(ValueError):
        ev.parse_objective(expr)


def test_objective_typos_are_caught():
    whittle.decide("p", ["a"]).learn(success=1)
    with pytest.raises(ValueError, match="sucess"):
        report(log(), always("a"), objective="sucess")


def test_expired_decisions_score_the_default_reward(monkeypatch):
    monkeypatch.setenv("WHITTLE_HORIZON_DAYS", "1e-12")  # each decision expires as it commits
    for _ in range(4):
        whittle.decide("p", ["a"])
    monkeypatch.delenv("WHITTLE_HORIZON_DAYS")
    (store.location() / "state.json").unlink()
    whittle.decide("p", ["a"]).learn(s=1)
    rep = report(log(), always("a"))
    assert rep["values"]["snips"] == pytest.approx(0.2)
    assert any("no outcome in time" in w for w in rep["warnings"])


def test_open_decisions_are_left_out_and_flagged():
    whittle.decide("p", ["a"]).learn(s=1)
    whittle.decide("p", ["a"])
    rep = report(log(), always("a"))
    assert rep["n"] == 1 and any("awaiting" in w for w in rep["warnings"])


def test_repeated_choices_in_a_run_warn():
    with whittle.run("r"):
        whittle.decide("p", ["a"])
        whittle.decide("p", ["a"])
        whittle.learn(s=1)
    assert any("more than once" in w for w in report(log(), always("a"))["warnings"])


def test_several_points_need_a_choice():
    whittle.decide("p", ["a"]).learn(s=1)
    whittle.decide("q", ["a"]).learn(s=1)
    with pytest.raises(ValueError, match="--point"):
        report(log(), always("a"))
    assert report(log(), always("a"), point="q")["n"] == 1


def test_plan_shrinks_as_precision_loosens_and_matches_observed_interval():
    data = simulate()
    assert ev.plan(0.01, data, always("c"))["need"] > ev.plan(0.02, data, always("c"))["need"] * 3
    half_width = 0.5 * (lambda c: c[1] - c[0])(ev.evaluate(data, always("c"), bootstrap=200)["ci95"]["ips"])
    assert ev.plan(half_width, data, always("c"))["need"] == pytest.approx(3000, rel=0.4)


def test_plan_without_logs_scales_with_budget():
    assert ev.plan(0.01, epsilon=0.02, actions=3)["need"] == round(1.96 ** 2 / 0.01 ** 2 * 0.25 * 3 / 0.02 + 0.5)


def write_policy(tmp_path, body="def policy(d):\n    return 'c'\n"):
    path = tmp_path / "p.py"
    path.write_text(body)
    return str(path)


def test_cli_end_to_end(tmp_path, capsys):
    simulate(default="b")
    with pytest.raises(SystemExit) as e:
        cli.main(["status"])
    out = capsys.readouterr().out
    assert e.value.code in (0, None) and "3000 decisions" in out and "success" in out
    with pytest.raises(SystemExit) as e:
        cli.main(["eval", "--policy", write_policy(tmp_path), "--bootstrap", "100"])
    assert e.value.code in (0, None) and "verdict: ship" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["eval", "--policy", write_policy(tmp_path, "def policy(d):\n    return 'a'\n"), "--bootstrap", "100"])
    assert e.value.code == 1 and "verdict: block" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["plan", "--policy", write_policy(tmp_path), "--target-ci", "0.01"])
    assert "need" in capsys.readouterr().out


def test_cli_reports_errors_plainly(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["eval", "--policy", write_policy(tmp_path)])
    assert e.value.code == "error: no decisions with outcomes yet"


HOOK = "hooks/on_edit.py"


@pytest.mark.parametrize("body,expected", [('x = whittle.decide("router", [])', "router"), ("x = 1", None)])
def test_hook_flags_only_files_with_choice_points(tmp_path, request, body, expected):
    f = tmp_path / "a.py"
    f.write_text(body)
    hook = request.config.rootpath / HOOK
    res = subprocess.run([sys.executable, hook], input=json.dumps({"tool_input": {"file_path": str(f)}}),
                         capture_output=True, text=True)
    assert res.returncode == 0
    assert (expected in res.stdout) if expected else res.stdout == ""


def test_hook_survives_bad_input(request):
    res = subprocess.run([sys.executable, request.config.rootpath / HOOK], input="nope", capture_output=True, text=True)
    assert res.returncode == 0 and not res.stderr
