"""Agent choice points over gittins, logging the DESIGN §7 fields (milestone M3).

choose() wraps gittins.decide and appends the decision to a JSONL log with choice_point, trace_id,
and what ope.py needs to check support: epsilon for gittins' own explorer, or the full scores and
probs for a custom one. resolve_trace() fans one outcome out to every open decision of a trace.

Callers serialise access to state and log, as with gittins itself.
"""

import json, os

import gittins


def choose(state, log, context, candidates, t, salt, choice_point, *, epsilon=None, trace_id=None, score=None,
           explore=None, **fields):
    """Decide and log; returns the gittins record. Extra fields (step, parent_decision_id,
    policy_ref, context_ref, context_digest) are logged as given.

    With gittins' built-in epsilon-greedy, pass the state's epsilon: its eps/K floor is what proves
    support. With a custom explore, the full scores and probs are logged instead."""
    seen = {}
    if explore is None:
        if epsilon is None:
            raise ValueError("pass the state's epsilon, or a custom explore")
        seen["epsilon"] = epsilon
    else:
        def capture(scores, eps):
            seen["scores"], seen["probs"] = list(scores), list(explore(scores, eps))
            return seen["probs"]

    rec = gittins.decide(state, context, candidates, t, salt, score=score, explore=capture if explore else None)
    if explore is None:  # epsilon isn't readable from the state, so check the caller's against what gittins did
        k = len(candidates)
        if not any(abs(rec.propensity - epsilon / k - (1 - epsilon) * m) < 1e-9 for m in [0] + [1 / j for j in range(1, k + 1)]):
            raise ValueError(f"propensity {rec.propensity} impossible under epsilon={epsilon} with {k} candidates")
    else:  # gittins samples from the distribution we return, so this only fails on a gittins contract change
        assert abs(seen["probs"][rec.chosen] - rec.propensity) < 1e-9, (seen["probs"], rec.propensity)
    line = json.loads(gittins.log_line(rec))
    line["record"].update(fields, choice_point=choice_point, trace_id=trace_id, **seen)
    _append(log, [line])
    return rec


def resolve_trace(state, log, trace_id, reward, t, reward_components=None):
    """Reward every still-open decision of trace_id; returns how many were resolved."""
    lines = []
    for did in _traces(log).get(trace_id, ()):
        res = gittins.learn(state, did, reward, t)
        if res:  # None: already resolved or expired (exactly-once is gittins')
            line = json.loads(gittins.log_line(res))
            line.update(reward_scope="trace", **({"reward_components": reward_components} if reward_components else {}))
            lines.append(line)
    _append(log, lines)
    return len(lines)


_index = {}  # log path -> (bytes read, {trace_id: [decision ids]})


def _traces(log):
    """Decision ids per trace, reading only what was appended since the last call in this process."""
    # shortcut: a fresh process (e.g. a hook) still reads the whole log once, persist the index if that gets slow
    path = os.path.abspath(log)
    offset, traces = _index.get(path, (0, {}))
    if os.path.getsize(path) < offset:  # truncated or replaced
        offset, traces = 0, {}
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):  # partial write in progress; pick it up next call
                break
            offset += len(line)
            e = json.loads(line)
            if e["event"] == "decision" and e["record"].get("trace_id") is not None:
                traces.setdefault(e["record"]["trace_id"], []).append(e["record"]["decision_id"])
    _index[path] = offset, traces
    return traces


def _append(log, lines):
    with open(log, "a") as f:
        f.write("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
