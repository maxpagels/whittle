import json, random, subprocess

import gittins

import agent, ope


def softmax_ish(scores, epsilon):
    total = sum(scores)
    return [s / total for s in scores]


def test_trace_roundtrip(tmp_path):
    log = tmp_path / "log.jsonl"
    state = gittins.create(bits=18, horizon=1e9, epsilon=0.2, default_reward=0.5)
    rng = random.Random(0)
    for i in range(400):
        trace = f"tr{i}"
        route = agent.choose(state, log, {"q": "x"}, [(m, {}) for m in ("small", "large")], float(i), "s",
                             "router.v1", epsilon=0.2, trace_id=trace, step=0)
        agent.choose(state, log, {"q": "x"}, [(tool, {}) for tool in ("search", "code", "none")], float(i), "s",
                     "tool.v1", trace_id=trace, step=1, score=lambda ctx, cands: [0.1, 0.7, 0.2],  # e.g. an LLM rater
                     explore=softmax_ish)
        reward = float(rng.random() < (0.8 if route.chosen == 1 else 0.4))
        assert agent.resolve_trace(state, log, trace, reward, float(i), {"success": reward}) == 2
        assert agent.resolve_trace(state, log, trace, 1.0, float(i)) == 0  # exactly once

    events = [json.loads(line) for line in open(log)]
    first = {}
    for e in events:
        if e["event"] == "decision":
            first.setdefault(e["record"]["choice_point"], e["record"])
    assert first["router.v1"]["epsilon"] == 0.2 and "probs" not in first["router.v1"]
    assert first["tool.v1"]["scores"] == [0.1, 0.7, 0.2] and first["tool.v1"]["step"] == 1
    assert all(abs(a - b) < 1e-9 for a, b in zip(first["tool.v1"]["probs"], [0.1, 0.7, 0.2]))
    assert all(e["reward_scope"] == "trace" for e in events if e["event"] == "resolution")
    assert subprocess.run(["gittins", "verify", "--log", str(log)], capture_output=True).returncode == 0

    rep = ope.evaluate(log, target=lambda rec, ctx: [0.0, 1.0], choice_point="router.v1", bootstrap=200)
    assert rep["n"] == 400 and rep["coverage"] == 1.0 and not any("support" in w for w in rep["warnings"])
    lo, hi = rep["ci95"]["snips"]
    assert lo <= 0.8 <= hi, rep
    rep = ope.evaluate(log, target=lambda rec, ctx: [0.0, 0.0, 1.0], choice_point="tool.v1", bootstrap=0)
    assert rep["coverage"] == 1.0


def test_wrong_epsilon_is_caught(tmp_path):
    # uniform (all-tied) decisions fit any epsilon, so the check only bites once scores separate
    state, log = gittins.create(bits=18, horizon=1e9, epsilon=0.2, default_reward=0.5), tmp_path / "log.jsonl"
    for i in range(20):
        rec = agent.choose(state, log, {}, [(c, {}) for c in "abc"], float(i), "s", "cp", epsilon=0.2)
        gittins.learn(state, rec.decision_id, float(rec.chosen == 2), float(i))
    try:
        agent.choose(state, log, {}, [(c, {}) for c in "abc"], 20.0, "s", "cp", epsilon=0.5)
    except ValueError:
        return
    raise AssertionError("epsilon=0.5 accepted for a state created with 0.2")
