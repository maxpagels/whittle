"""Off-policy evaluation of arbitrary target policies on gittins decision logs (DESIGN §8, milestone M2).

    python ope.py --log decisions.jsonl[.gz] --target-probs probs.jsonl [--choice-point router.v1]

Each line of the target-probs file is {"decision_id": ..., "probs": [...]} with one probability per
logged candidate, plus optional "q": [...] (reward-model predictions) to enable DR.
"""

import argparse, gzip, json, math, random, sys

ESS_WARN = 0.05  # warn when ESS < 5% of n (DESIGN §8.3)
SUPPORT_MIN = 1e-3  # logged propensity below which target mass counts as unsupported


def read_log(path):
    """Resolved decisions in log order: (decision event, reward). Open decisions are skipped."""
    with open(path, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    with (gzip.open(path, "rt") if gz else open(path)) as f:
        events = [json.loads(line) for line in f if line.strip()]
    rewards = {e["decision_id"]: e["reward"] for e in events if e["event"] == "resolution"}
    return [(e, rewards[e["record"]["decision_id"]]) for e in events
            if e["event"] == "decision" and e["record"]["decision_id"] in rewards]


def check_probs(probs, k, decision_id):
    # target mass outside the logged candidate set can't be evaluated (DESIGN §6.4)
    if len(probs) != k or any(p < 0 for p in probs) or abs(sum(probs) - 1) > 1e-6:
        raise ValueError(f"{decision_id}: target probs must be {k} non-negative values summing to 1, got {probs}")


def estimates(rows):
    """rows: (w, r, dm, q_chosen); dm/q_chosen are None without a reward model."""
    n = len(rows)
    s_w = sum(w for w, *_ in rows)
    out = {"ips": sum(w * r for w, r, *_ in rows) / n,
           "snips": sum(w * r for w, r, *_ in rows) / s_w if s_w else math.nan}
    if rows[0][2] is not None:
        out["dr"] = sum(dm + w * (r - q) for w, r, dm, q in rows) / n
    return out


def evaluate(log, target=None, target_probs=None, choice_point=None, reward_model=None,
             context_store=None, bootstrap=1000, seed=0):
    """Estimate the target policy's value with mandatory diagnostics.

    target(record, context) -> probs, or target_probs: {decision_id: probs}.
    reward_model(record, context) -> q per candidate (enables DR).
    context_store(context_ref) -> context, for records that don't embed it.
    """
    rows, coverage = [], []
    for event, r in read_log(log):
        rec = event["record"]
        if choice_point is not None and rec.get("choice_point") != choice_point:
            continue
        k, did = len(event["candidates"]), rec["decision_id"]
        context = context_store(rec["context_ref"]) if context_store and "context_ref" in rec else event.get("context")
        if target_probs is not None:
            if did not in target_probs:
                continue
            probs = target_probs[did]
        else:
            probs = target(rec, context)
        check_probs(probs, k, did)
        w = probs[rec["chosen"]] / rec["propensity"]
        q = reward_model(rec, context) if reward_model else None
        rows.append((w, r, sum(p * x for p, x in zip(probs, q)) if q else None, q[rec["chosen"]] if q else None))
        if "probs" in rec:  # full logging distribution
            coverage.append(all(p == 0 or lp >= SUPPORT_MIN for p, lp in zip(probs, rec["probs"])))
        elif "epsilon" in rec:  # epsilon-greedy floor; below the threshold, which actions are short is unknown
            coverage.append(rec["epsilon"] / k >= SUPPORT_MIN)
    if not rows:
        raise ValueError("no resolved decisions to evaluate")

    n = len(rows)
    ws = sorted((w for w, *_ in rows), reverse=True)
    s_w, s_w2 = sum(ws), sum(w * w for w in ws)
    s_wr = sum(abs(w * r) for w, r, *_ in rows)
    top = sorted((abs(w * r) for w, r, *_ in rows), reverse=True)[:max(1, n // 100)]
    values = estimates(rows)

    # shortcut: pure-Python bootstrap is O(B·n), move to numpy once logs reach ~1e5 decisions
    rng = random.Random(seed)
    boots = {name: [] for name in values}
    for _ in range(bootstrap):
        for name, v in estimates([rows[rng.randrange(n)] for _ in range(n)]).items():
            boots[name].append(v)
    ci = {name: (sorted(b)[int(0.025 * len(b))], sorted(b)[int(0.975 * len(b)) - 1]) if b else None
          for name, b in boots.items()}

    ess = s_w * s_w / s_w2 if s_w2 else 0.0
    warnings = []
    if ess < ESS_WARN * n:
        warnings.append(f"ESS {ess:.1f} is below {ESS_WARN:.0%} of n={n}")
    if not coverage:
        warnings.append("support unknown: log records lack 'probs' and 'epsilon'")
    elif not all(coverage):
        warnings.append(f"target puts mass on unsupported actions in {coverage.count(False)} of {len(coverage)} decisions")
    if bootstrap and len(values) > 1:
        lo, hi = max(l for l, _ in ci.values()), min(h for _, h in ci.values())
        if lo > hi:
            warnings.append("estimators disagree beyond their 95% intervals")
    return {"n": n, "values": values, "ci95": ci if bootstrap else None, "ess": ess, "max_weight": ws[0],
            "top1pct_share": sum(top) / s_wr if s_wr else 0.0,
            "coverage": sum(coverage) / len(coverage) if coverage else None, "warnings": warnings}


def format_report(rep):
    lines = [f"n={rep['n']}  ESS={rep['ess']:.1f}  max_weight={rep['max_weight']:.3g}  "
             f"top1%_share={rep['top1pct_share']:.1%}  coverage="
             + ("unknown" if rep["coverage"] is None else f"{rep['coverage']:.1%}")]
    for name, v in rep["values"].items():
        ci = rep["ci95"] and rep["ci95"][name]
        lines.append(f"{name:>6}: {v:.4f}" + (f"  [{ci[0]:.4f}, {ci[1]:.4f}]" if ci else ""))
    lines += [f"warning: {w}" for w in rep["warnings"]]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", required=True)
    ap.add_argument("--target-probs", required=True)
    ap.add_argument("--choice-point")
    ap.add_argument("--bootstrap", type=int, default=1000)
    args = ap.parse_args(argv)
    with open(args.target_probs) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    probs = {x["decision_id"]: x["probs"] for x in lines}
    qs = {x["decision_id"]: x["q"] for x in lines if "q" in x}
    if qs and len(qs) != len(lines):
        sys.exit("either every target-probs line has 'q' or none does")
    rep = evaluate(args.log, target_probs=probs, choice_point=args.choice_point, bootstrap=args.bootstrap,
                   reward_model=(lambda rec, _: qs[rec["decision_id"]]) if qs else None)
    print(format_report(rep))


if __name__ == "__main__":
    main()
