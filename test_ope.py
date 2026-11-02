"""Run: uv run pytest  (or uv run python test_ope.py)"""

import random

import gittins

import ope

TRUE = [0.2, 0.5, 0.8]  # Bernoulli reward per arm


def make_log(path, n=3000):
    rng = random.Random(1)
    state = gittins.create(bits=18, horizon=1e9, epsilon=0.2, default_reward=0.5)
    with open(path, "w") as f:
        for i in range(n):
            rec = gittins.decide(state, {"u": "x"}, [(a, {}) for a in "abc"], float(i), "s")
            f.write(gittins.log_line(rec) + "\n")
            res = gittins.learn(state, rec.decision_id, float(rng.random() < TRUE[rec.chosen]), float(i))
            f.write(gittins.log_line(res) + "\n")
        f.write(gittins.log_line(gittins.decide(state, {"u": "x"}, [(a, {}) for a in "abc"], float(n), "s")) + "\n")


def test_estimates_match_truth(tmp_path):
    log = tmp_path / "log.jsonl"
    make_log(log)
    rep = ope.evaluate(log, target=lambda rec, ctx: [1.0, 0.0, 0.0], bootstrap=200,
                       reward_model=lambda rec, ctx: [0.3, 0.3, 0.3])  # deliberately biased q
    assert rep["n"] == 3000  # the open decision is skipped
    for name, v in rep["values"].items():
        lo, hi = rep["ci95"][name]
        assert lo <= TRUE[0] <= hi, (name, v, lo, hi)
    assert rep["ess"] < rep["n"] and "support unknown" in rep["warnings"][-1]


def test_rejects_mass_outside_candidates(tmp_path):
    log = tmp_path / "log.jsonl"
    make_log(log, n=5)
    for bad in ([0.5, 0.5], [0.5, 0.5, 0.5], [-0.1, 0.6, 0.5]):
        try:
            ope.evaluate(log, target=lambda rec, ctx: bad, bootstrap=0)
        except ValueError:
            continue
        raise AssertionError(bad)


if __name__ == "__main__":
    import pathlib, tempfile
    for test in (test_estimates_match_truth, test_rejects_mass_outside_candidates):
        test(pathlib.Path(tempfile.mkdtemp()))
    print("ok")
