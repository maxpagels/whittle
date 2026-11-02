"""whittle status | eval | plan"""

import argparse, os, sys, time

from . import evaluate as ev, store


def _status(args, root, data):
    st = ev.status(data)
    print(f"store: {root}\nlog: {store.log_path(root)}")
    if os.environ.get("WHITTLE_STORE") is None and store.ephemeral_platform():
        print(f"warning: running on {store.ephemeral_platform()} with no WHITTLE_STORE; this data will be lost")
    if not st["points"]:
        print("no decisions yet")
        return
    print(f"last decision: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st['last']))}")
    for name, p in sorted(st["points"].items()):
        print(f"\n{name}: {p['decisions']} decisions, {p['rewarded']} rewarded, {p['open']} open, {p['expired']} expired")
        print("  actions: " + ", ".join(f"{a}={c}" for a, c in p["actions"].most_common()))
        print("  rewards: " + (", ".join(sorted(p["reward_names"])) or "none yet"))
    problem = ev.verify(data)
    print(f"\nwarning: {problem}" if problem else "\nlog verified by gittins")


def _eval(args, root, data):
    policy, q = ev.load_policy(args.policy)
    rep = ev.evaluate(data, policy, q, args.objective, args.point, args.default_reward, args.bootstrap)
    ci, v = rep["ci95"], rep["values"]
    print(f"objective: {rep['objective']}\nn={rep['n']}  ESS={rep['ess']:.1f}  max_weight={rep['max_weight']:.3g}")
    for name in ("base", "ips", "snips", "dr", "lift"):
        if name in v:
            bounds = f"  [{ci[name][0]:.4f}, {ci[name][1]:.4f}]" if name in ci else ""
            print(f"{name:>6}: {v[name]:.4f}{bounds}")
    for line in rep["hold"]:
        print(f"holding verdict: {line}")
    for line in rep["warnings"]:
        print(f"warning: {line}")
    print(f"verdict: {rep['verdict']}")
    return 1 if rep["verdict"] == "block" else 0


def _plan(args, root, data):
    if args.policy:
        res = ev.plan(args.target_ci, data, ev.load_policy(args.policy)[0], args.objective, args.point)
    else:
        res = ev.plan(args.target_ci, epsilon=args.epsilon, actions=args.actions)
    print(f"need {res['need']} decisions for ±{args.target_ci} (based on {res['basis']})")
    if "have" in res:
        print(f"have {res['have']}")
        if res["per_day"] and res["need"] > res["have"]:
            print(f"about {(res['need'] - res['have']) / res['per_day']:.1f} more days at {res['per_day']:.0f} a day")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="whittle", description="Evaluate agent changes from logged traffic.")
    sub = ap.add_subparsers(dest="command", required=True)
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--objective", help="expression over reward names, e.g. 'success - 50*cost_eur'")
    target.add_argument("--point", help="choice point, when several are logged")

    sub.add_parser("status", help="where the data is and how much there is").set_defaults(fn=_status)
    p = sub.add_parser("eval", parents=[target], help="score a candidate policy (exits 1 on a block verdict)")
    p.add_argument("--policy", required=True, help="Python file defining policy(d), optionally q(d)")
    p.add_argument("--default-reward", type=float, default=0.0, help="score of a decision with no outcome in time")
    p.add_argument("--bootstrap", type=int, default=1000)
    p.set_defaults(fn=_eval)
    p = sub.add_parser("plan", parents=[target], help="how many decisions a target precision needs")
    p.add_argument("--target-ci", type=float, required=True, help="confidence interval half-width")
    p.add_argument("--policy", help="plan from logged variance")
    p.add_argument("--epsilon", type=float)
    p.add_argument("--actions", type=int)
    p.set_defaults(fn=_plan)

    args = ap.parse_args(argv)
    try:
        root = store.location()
        sys.exit(args.fn(args, root, store.read_committed(root)))
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")
