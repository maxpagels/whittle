"""Validation study on a simulated agent (DESIGN §11, milestone M5).

    uv run python validate.py [--runs 5000] [--bootstrap 500] [--seed 0] [--seeds 10]

Each run of the simulated agent has two choice points, router.v1 (small/large model) and tool.v1
(search/code/none), both logged through agent.choose() with a fixed rater as scorer and gittins'
epsilon-greedy on top; the run's outcome is resolved with agent.resolve_trace(). Candidate changes
alter one choice point and hold the other at its logged behaviour (DESIGN §8.2). Each is predicted
from the log with ope.evaluate() and compared with its true value, computed exactly from the
simulator's success rates: what a live run converges to, without its sampling noise.
"""

import argparse, itertools, random, statistics, tempfile
from pathlib import Path

import gittins

import agent, ope

EPSILON = 0.1
LARGE_COST = 0.15  # reward = success - cost of the large model
CONTEXTS = [{"kind": k, "size": s} for k in ("bug", "feature", "refactor", "question") for s in ("small", "big")]
CANDIDATES = {"router.v1": ["small", "large"], "tool.v1": ["search", "code", "none"]}


def key(ctx):
    return ctx["kind"], ctx["size"]


def eps_greedy(scores):
    """gittins' built-in explorer (checked equal on the chosen propensity); rater scores have no ties."""
    best = scores.index(max(scores))
    return [EPSILON / len(scores) + (1 - EPSILON) * (i == best) for i in range(len(scores))]


class World:
    def __init__(self, rng):
        self.success = {}  # (ctx key, route, tool) -> P(success)
        for ctx in CONTEXTS:
            base, large = rng.uniform(0.2, 0.5), rng.uniform(-0.05, 0.3)
            tools = [rng.uniform(-0.15, 0.25) for _ in CANDIDATES["tool.v1"]]
            for (r, route), (t, tool) in itertools.product(enumerate(CANDIDATES["router.v1"]), enumerate(tools)):
                self.success[key(ctx), r, t] = min(1.0, max(0.0, base + large * r + tool))
        # the logging rater: truth plus noise, as an imperfect LLM rater would be
        self.rater = {(key(ctx), cp): [self.q(ctx, cp, a, logging=True) + rng.gauss(0, 0.1)
                                       for a in range(len(cands))]
                      for ctx in CONTEXTS for cp, cands in CANDIDATES.items()}

    def logging(self, ctx, cp):
        return eps_greedy(self.rater[key(ctx), cp])

    def reward(self, ctx, r, t):
        """Expected reward of one (route, tool) pair in ctx."""
        return self.success[key(ctx), r, t] - LARGE_COST * r

    def q(self, ctx, cp, a, logging=False):
        """True expected reward of action a at cp, the other choice point at its logged behaviour."""
        other = "tool.v1" if cp == "router.v1" else "router.v1"
        probs = [1 / len(CANDIDATES[other])] * len(CANDIDATES[other]) if logging else self.logging(ctx, other)
        pairs = [(a, b) if cp == "router.v1" else (b, a) for b in range(len(probs))]
        return sum(p * self.reward(ctx, r, t) for p, (r, t) in zip(probs, pairs))

    def value(self, cp, policy):
        """Exact value of following policy(ctx) -> probs at cp, holding the other at logged behaviour."""
        return statistics.mean(sum(p * self.q(ctx, cp, a) for a, p in enumerate(policy(ctx))) for ctx in CONTEXTS)


def log_runs(world, log, runs, rng):
    # one state for both choice points: decision ids are per state, and the log needs them unique
    state = gittins.create(bits=18, horizon=1e9, epsilon=EPSILON, default_reward=0.0)
    for i in range(runs):
        ctx, trace, chosen = rng.choice(CONTEXTS), f"run{i}", {}
        for step, (cp, cands) in enumerate(CANDIDATES.items()):
            rec = agent.choose(state, log, ctx, [(c, {}) for c in cands], float(i), "val", cp,
                               epsilon=EPSILON, trace_id=trace, step=step,
                               score=lambda c, _, cp=cp: world.rater[key(c), cp])
            chosen[cp] = rec.chosen
        r, t = chosen["router.v1"], chosen["tool.v1"]
        success = rng.random() < world.success[key(ctx), r, t]
        agent.resolve_trace(state, log, trace, float(success) - LARGE_COST * r, float(i),
                            {"success": float(success), "cost": LARGE_COST * r})


def one_hot(a, k):
    return [float(i == a) for i in range(k)]


def candidate_changes(world, rng):
    """(name, choice point, policy(ctx) -> probs): the changes a team might propose."""
    changes = []
    for cp, cands in CANDIDATES.items():
        k = len(cands)
        changes += [(f"always {c}", cp, lambda ctx, a=a, k=k: one_hot(a, k)) for a, c in enumerate(cands)]
        changes += [
            ("uniform", cp, lambda ctx, k=k: [1 / k] * k),
            ("rater greedy (eps 0)", cp, lambda ctx, cp=cp, k=k: one_hot(max(range(k), key=world.rater[key(ctx), cp].__getitem__), k)),
            ("oracle greedy", cp, lambda ctx, cp=cp, k=k: one_hot(max(range(k), key=lambda a: world.q(ctx, cp, a)), k)),
        ]
        for j in range(3):  # arbitrary context rules, e.g. a new prompt's routing behaviour
            table = {key(ctx): rng.randrange(k) for ctx in CONTEXTS}
            changes.append((f"rule #{j + 1}", cp, lambda ctx, t=table, k=k: one_hot(t[key(ctx)], k)))
    return changes


def biased_judge(world, cp):
    """An LLM judge that is directionally right but compressed and optimistic; DR must correct it."""
    def q(rec, ctx):
        return [0.5 + 0.5 * (world.q(ctx, cp, a) - 0.5) + 0.1 for a in range(len(CANDIDATES[cp]))]
    return q


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", type=int, default=5000)
    ap.add_argument("--bootstrap", type=int, default=500)
    ap.add_argument("--seed", type=int, default=0, help="first seed; its table is printed")
    ap.add_argument("--seeds", type=int, default=10, help="independent logs to pool coverage over")
    args = ap.parse_args(argv)

    # every change in a study is estimated from the same log, so one unlucky log shifts them all:
    # coverage is only meaningful pooled over independent logs
    rows, rhos = [], {est: [] for est in ("ips", "snips", "dr")}
    for seed in range(args.seed, args.seed + args.seeds):
        study = run_study(seed, args.runs, args.bootstrap, verbose=seed == args.seed)
        rows += study
        for est in rhos:
            rhos[est].append(statistics.correlation([r[0] for r in study], [r[1][est] for r in study], method="ranked"))

    print(f"\nover {args.seeds} independent logs of {args.runs} runs:")
    for est in rhos:
        covered = [r[2][est][0] <= r[0] <= r[2][est][1] for r in rows if r[3] >= 500]
        mae = statistics.mean(abs(r[1][est] - r[0]) for r in rows)
        print(f"{est:>5}: spearman mean {statistics.mean(rhos[est]):.3f} min {min(rhos[est]):.3f}  "
              f"mean abs error {mae:.4f}  ci coverage at ESS>=500 {sum(covered)}/{len(covered)}")

    # DESIGN §11 success criteria
    dr_cov = [r[2]["dr"][0] <= r[0] <= r[2]["dr"][1] for r in rows if r[3] >= 500]
    print(f"\nspearman(dr) >= 0.8 in every log: {'PASS' if min(rhos['dr']) >= 0.8 else 'FAIL'}  "
          f"dr coverage >= 90% at ESS>=500: {'PASS' if dr_cov and sum(dr_cov) / len(dr_cov) >= 0.9 else 'FAIL'}")
    if args.seeds < 10:
        print(f"(only {args.seeds} log(s): coverage is noisy, use --seeds 10 or more before reading it)")


def run_study(seed, runs, bootstrap, verbose):
    """Log one world's runs and estimate every candidate change: [(true, values, ci95, ess)]."""
    rng = random.Random(seed)
    world = World(rng)
    log = Path(tempfile.mkdtemp()) / "decisions.jsonl"
    log_runs(world, log, runs, rng)
    if verbose:
        print(f"seed {seed}: logged {runs} runs to {log}\n")
        print(f"{'choice point':<10} {'change':<21} {'true':>7} {'ips':>7} {'snips':>7} {'dr':>7}  {'dr 95% ci':<17} {'ess':>7}")
    rows = []
    for name, cp, policy in candidate_changes(world, rng):
        rep = ope.evaluate(log, target=lambda rec, ctx, p=policy: p(ctx), choice_point=cp,
                           reward_model=biased_judge(world, cp), bootstrap=bootstrap, seed=seed)
        true, v, ci = world.value(cp, policy), rep["values"], rep["ci95"]
        rows.append((true, v, ci, rep["ess"]))
        if verbose:
            print(f"{cp:<10} {name:<21} {true:7.4f} {v['ips']:7.4f} {v['snips']:7.4f} {v['dr']:7.4f}  "
                  f"[{ci['dr'][0]:.3f}, {ci['dr'][1]:.3f}]{'' if ci['dr'][0] <= true <= ci['dr'][1] else ' ✗':<2} {rep['ess']:7.0f}")
    return rows

if __name__ == "__main__":
    main()
