"""Scores arbitrary policies over the gittins log, which `gittins eval` doesn't support yet.

The log is checked by `gittins verify`, and the pairing of decisions with resolutions is the one
gittins recorded. Every number a report shows is computed here.
"""

import ast, json, math, operator, random, runpy, shutil, subprocess, tempfile
from collections import Counter
from types import SimpleNamespace

ESS_MIN = 30  # below this a confidence interval means little
OPEN_WARN = 0.10
Z = 1.96

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def parse_objective(expr):
    """Compile an arithmetic expression over reward names. Returns (fn(reward dict), names)."""
    names = set()

    def build(node):
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            op, left, right = _OPS[type(node.op)], build(node.left), build(node.right)
            return lambda r: op(left(r), right(r))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            sign, inner = (-1 if isinstance(node.op, ast.USub) else 1), build(node.operand)
            return lambda r: sign * inner(r)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return lambda r: node.value
        if isinstance(node, ast.Name):
            names.add(node.id)
            return lambda r: r.get(node.id, 0.0)
        raise ValueError(f"objective may use only reward names, numbers and + - * /: {expr!r}")

    return build(ast.parse(expr, mode="eval").body), names


def load(data):
    """Decisions from raw log bytes, each with the resolution gittins recorded for it."""
    events = [json.loads(line) for line in data.splitlines()]
    resolutions = {e["decision_id"]: e for e in events if e["event"] == "resolution"}
    decisions = []
    for e in events:
        if e["event"] == "decision":
            res = resolutions.get(e["record"]["decision_id"])
            w = e["whittle"]
            decisions.append(SimpleNamespace(event=e, id=e["record"]["decision_id"], t=e["record"]["t"],
                                             run_id=w["run_id"], point=w["point"],
                                             state=res["kind"] if res else "open",
                                             reward=res.get("rewards", {}) if res else {}))
    return decisions


def verify(data):
    """None if `gittins verify` accepts the log, else why it can't be trusted."""
    cli = shutil.which("gittins")
    if cli is None:
        return "the log is unverified: install the gittins CLI (https://docs.getgittins.dev)"
    with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
        f.write(data)
        f.flush()
        res = subprocess.run([cli, "verify", "--log", f.name], capture_output=True, text=True)
    if res.returncode == 0:
        return None
    return "gittins verify rejected the log: " + (res.stdout + res.stderr).strip().splitlines()[-1]


def _view(d):
    e = d.event
    w = e["whittle"]
    return SimpleNamespace(point=w["point"], context=e["context"], actions=[c[0] for c in e["candidates"]],
                           default=w["default"], run_id=w["run_id"])


def _target(result, view):
    """A policy's return value (action, or {action: probability}) as a full distribution."""
    probs = {str(result): 1.0} if not isinstance(result, dict) else {str(k): float(v) for k, v in result.items()}
    if any(p < 0 for p in probs.values()) or abs(sum(probs.values()) - 1) > 1e-6:
        raise ValueError(f"policy must return non-negative probabilities summing to 1, got {result}")
    unknown = [a for a, p in probs.items() if p > 0 and a not in view.actions]
    if unknown:
        raise ValueError(f"policy chose {unknown}, which was never offered at {view.point!r} (offered: {view.actions}). "
                         "Logs can only evaluate logged actions: add it to decide() and collect data first.")
    return [probs.get(a, 0.0) for a in view.actions]


def _rows(decisions, target, objective, default_reward, q=None):
    rows, uncovered, repeats = [], 0, Counter()
    for d in decisions:
        view, rec, w = _view(d), d.event["record"], d.event["whittle"]
        probs = _target(target(view), view)
        k, eps = len(view.actions), w["epsilon"]
        logged = [(1 - eps if a == view.default else 0.0) + eps / k for a in view.actions]
        uncovered += any(p > 0 and l <= 0 for p, l in zip(probs, logged))
        chosen, p = rec["chosen"], rec["propensity"]
        base = [float(a == view.default) for a in view.actions]
        r = objective(d.reward) if d.reward else default_reward
        row = SimpleNamespace(wt=probs[chosen] / p, wb=base[chosen] / p, r=r)
        if q:
            qs = q(view)
            q_list = [float(qs[a]) for a in view.actions]
            row.dmt = sum(x * y for x, y in zip(probs, q_list))
            row.dmb = sum(x * y for x, y in zip(base, q_list))
            row.qc = q_list[chosen]
        rows.append(row)
        if d.run_id is not None:
            repeats[d.run_id] += 1
    return rows, uncovered, sum(v > 1 for v in repeats.values())


def _values(rows):
    n, sw_t, sw_b = len(rows), sum(x.wt for x in rows), sum(x.wb for x in rows)
    out = {"ips": sum(x.wt * x.r for x in rows) / n,
           "snips": sum(x.wt * x.r for x in rows) / sw_t if sw_t else math.nan,
           "base": sum(x.wb * x.r for x in rows) / sw_b if sw_b else math.nan}
    head = "snips"
    if hasattr(rows[0], "qc"):
        out["dr"] = sum(x.dmt + x.wt * (x.r - x.qc) for x in rows) / n
        out["base"] = sum(x.dmb + x.wb * (x.r - x.qc) for x in rows) / n
        head = "dr"
    out["lift"] = out[head] - out["base"]
    return out


def _ci(rows, bootstrap, seed):
    # shortcut: pure-Python bootstrap is O(B·n), move to numpy once logs reach ~1e5 decisions
    rng, n, samples = random.Random(seed), len(rows), {}
    for _ in range(bootstrap):
        for name, v in _values([rows[rng.randrange(n)] for _ in range(n)]).items():
            if not math.isnan(v):
                samples.setdefault(name, []).append(v)
    return {name: (sorted(s)[int(0.025 * len(s))], sorted(s)[int(0.975 * len(s)) - 1]) for name, s in samples.items()}


def load_policy(path):
    """A policy file defines policy(d), and optionally q(d) -> {action: predicted objective} to enable DR."""
    module = runpy.run_path(str(path))
    if "policy" not in module:
        raise ValueError(f"{path} must define policy(d)")
    return module["policy"], module.get("q")


def _select(decisions, point):
    points = Counter(d.point for d in decisions)
    if point is None:
        if len(points) > 1:
            raise ValueError(f"several choice points are logged, pick one with --point: {sorted(points)}")
        return decisions
    if point not in points:
        raise ValueError(f"no decisions at {point!r}; logged points: {sorted(points)}")
    return [d for d in decisions if d.point == point]


def _resolve_objective(expr, decisions):
    keys = sorted({k for d in decisions for k in d.reward})
    if expr is None:
        if len(keys) != 1:
            raise ValueError(f"choose an --objective over the logged rewards: {keys}")
        expr = keys[0]
    fn, names = parse_objective(expr)
    if not names <= set(keys):
        raise ValueError(f"objective uses {sorted(names - set(keys))}, not in the logged rewards {keys}")
    return fn, expr


def evaluate(data, policy, q=None, objective=None, point=None, default_reward=0.0, bootstrap=1000, seed=0):
    """The candidate policy's value against the current default, with a ship/wait/block verdict."""
    selected = _select(load(data), point)
    usable = [d for d in selected if d.state != "open"]
    if not usable:
        raise ValueError("no decisions with outcomes yet")
    fn, expr = _resolve_objective(objective, usable)
    rows, uncovered, multi = _rows(usable, policy, fn, default_reward, q)
    n = len(rows)
    sw, sw2 = sum(x.wt for x in rows), sum(x.wt ** 2 for x in rows)
    ess = sw * sw / sw2 if sw2 else 0.0
    values, ci = _values(rows), _ci(rows, bootstrap, seed)
    expired = sum(d.state == "expired" for d in usable)
    open_share = 1 - n / len(selected)

    hold = []  # each reason keeps the verdict at wait, however good the estimate looks
    warnings = []
    if ess < ESS_MIN:
        hold.append(f"effective sample size {ess:.1f} is below {ESS_MIN}")
    if uncovered:
        hold.append(f"{uncovered} of {n} decisions gave the policy's action no chance of being explored")
    problem = verify(data)
    if problem:
        hold.append(problem)
    if open_share > OPEN_WARN:
        warnings.append(f"{open_share:.0%} of decisions are still awaiting outcomes and were left out")
    if expired:
        warnings.append(f"{expired} of {n} decisions got no outcome in time and scored {default_reward}")
    if multi:
        warnings.append(f"{multi} runs made this choice more than once; they are treated as independent, so the "
                        "interval may be too narrow")

    lo, hi = ci.get("lift", (math.nan, math.nan))
    verdict = "wait" if hold or math.isnan(lo) else "ship" if lo > 0 else "block" if hi < 0 else "wait"
    return {"n": n, "objective": expr, "values": values, "ci95": ci, "ess": ess, "max_weight": max(x.wt for x in rows),
            "verdict": verdict, "hold": hold, "warnings": warnings}


def plan(target_ci, data=None, policy=None, objective=None, point=None, epsilon=None, actions=None):
    """Decisions needed to estimate a policy's value to ±target_ci."""
    if policy is None:
        if not epsilon or not actions:
            raise ValueError("pass a policy to plan from logs, or --epsilon and --actions to plan without them")
        # shortcut: assumes a 0-1 reward at worst-case variance, a policy that always leaves the default
        return {"need": math.ceil((Z / target_ci) ** 2 * 0.25 * actions / epsilon), "basis": "worst case for a 0-1 reward"}
    usable = [d for d in _select(load(data), point) if d.state != "open"]
    if not usable:
        raise ValueError("no decisions with outcomes yet")
    fn, _ = _resolve_objective(objective, usable)
    rows, _, _ = _rows(usable, policy, fn, 0.0)
    psi = [x.wt * x.r for x in rows]
    mean = sum(psi) / len(psi)
    variance = sum((p - mean) ** 2 for p in psi) / max(1, len(psi) - 1)
    span = max(d.t for d in usable) - min(d.t for d in usable)
    per_day = len(usable) / span * 86400 if span > 0 else None
    return {"need": math.ceil(Z ** 2 * variance / target_ci ** 2), "have": len(usable), "per_day": per_day,
            "basis": "the variance in your logs"}


def status(data):
    decisions, points = load(data), {}
    for d in decisions:
        s = points.setdefault(d.point, {"decisions": 0, "rewarded": 0, "open": 0, "expired": 0, "actions": Counter(),
                                        "reward_names": set()})
        s["decisions"] += 1
        s[d.state] += 1
        s["actions"][d.event["candidates"][d.event["record"]["chosen"]][0]] += 1
        s["reward_names"] |= set(d.reward)
    return {"points": points, "last": max((d.t for d in decisions), default=None)}
