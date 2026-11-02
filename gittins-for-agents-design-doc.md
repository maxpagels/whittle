# Gittins for Agents: Design Doc

2026-11-02 · Max Pagels

## Summary

Gittins becomes a tool that tells agent teams whether a change (model, prompt, tool policy, routing rule) will help before they ship it, using logged production traffic.

Agent teams today evaluate on fixed datasets or traces. That can't answer "what would production have looked like under this change?", because agent stacks make choices without randomization or logged propensities. Gittins already logs propensities at decision time, replays logs exactly, and runs IPS/SNIPS evaluation. This doc covers the small set of additions that make it fit agents.

## Goals and non-goals

Goals:

- Rank candidate agent changes from logs, with confidence intervals, before deployment.
- Make adoption a few lines of code, or zero code via a proxy.
- Keep determinism, replay and log verification as core guarantees.

Non-goals:

- Not a dataset/trace eval platform or an LLM-judge library. Gittins consumes judge scores; it doesn't produce them.
- No full long-horizon RL in v1.
- No hosted SaaS in v1. Local CLI and libraries only.

## Design

The loop has four steps:

1. **Decide.** The agent calls `decide()` at each choice point (model, prompt variant, tool policy, retrieval depth). A small exploration budget (e.g. 1–2% of traffic) randomizes the choice, and the propensity is logged.
2. **Learn.** Outcomes arrive later and are logged with `learn()` as a vector reward.
3. **Evaluate.** `gittins eval` scores any candidate policy against the logs.
4. **Ship or gate.** The report says whether to ship, wait for more data, or block the change in CI.

Additions:

- **Vector rewards.** Log success, cost, latency, feedback and judge score separately. The objective is chosen at eval time, so cost–quality trade-offs can be re-weighted without new data.
- **Doubly robust estimator.** Add DR next to IPS and SNIPS. The direct-method model is pluggable, so a team's existing eval suite or LLM judge becomes the reward model, and the logs correct its bias.
- **Arbitrary target policies.** Evaluate a policy file or a Python callable that returns action probabilities, not only Gittins configs.
- **Multi-step, scoped.** v1 supports one randomized decision per trajectory, or independent decision points scored against the final reward with per-decision weights. A loud warning fires when effective sample size collapses.
- **Exploration planner.** Answers "how many decisions at what ε to evaluate this change to ±X?"
- **Proxy mode.** An OpenAI-compatible gateway that randomizes model and prompt variants and writes Gittins logs, so teams can adopt it without changing agent code.

## API and log format

Whittle is a thin layer over Gittins, which owns the draw, propensities, the exactly-once ledger, expiry and the log format. Whittle pins the policy with `explore` and a no-op `train`, so there is no online learning, and stores the state on disk.

```python
with whittle.run(run_id):
    d = whittle.decide("router", ["model-a", "model-b", "local-7b"],
                       context={"task": "code", "len": "long"})
    # ...run the agent with d.action...
    whittle.learn(success=1, cost_eur=0.004, latency_s=2.1)
```

```
whittle eval --policy policies/long_code_to_b.py --objective "success - 50*cost_eur"
whittle plan --policy policies/long_code_to_b.py --target-ci 0.01
```

The policy file defines `policy(d)`, and `q(d)` to enable DR, so a team's judge plugs in as a function.

Lines stay canonical Gittins lines, so `gittins verify` works on them. Added fields:

| Field | Line type | Purpose |
| --- | --- | --- |
| `whittle.point` | decision | Names the choice point in the agent |
| `whittle.run_id` | decision | Groups decisions from one agent run |
| `whittle.default`, `whittle.epsilon` | decision | The logging policy, needed for support checks |
| `whittle.format_version` | decision | 2 |
| `rewards` | resolution | Vector reward; `reward` keeps the first value, so lines stay canonical |

## Distribution: Claude Code plugin, no backend

The evaluator ships as a library, a local CLI and a Claude Code plugin. It needs no backend. Name: Whittle.

**No backend.** Each operation takes a file lock, loads the Gittins state, runs, appends to the log and saves: the "load state, decide, learn, save state" pattern from the Gittins docs. The ledger is therefore on disk and any process can resolve a decision. State lives in a per-project data directory, or at the path in `WHITTLE_STORE`. Developers don't handle paths, state or log files. The CLI reads the log and evaluates locally; `gittins verify` checks it first. We never host or see user data. Proxy mode is the one running service, and users host it themselves.

**Plugin components:**

| Component | What it does |
| --- | --- |
| `/whittle:instrument` skill | Finds choice points in the user's agent code and adds `decide()`/`learn()` with propensity logging |
| `/whittle:eval` skill | Turns a plain-language change into a target-policy file, runs the eval, explains estimate, CI, ESS and the ship decision |
| `/whittle:plan` skill | Estimates the exploration traffic needed to evaluate a change |
| Hook | When a file with a choice point changes, points Claude to `/whittle:eval` |
| Reviewer agent | Checks diagnostics (ESS, max weight, propensity coverage) before a result is trusted |

Skills call the CLI directly. A local MCP server is optional, added only if typed tools are wanted. The plugin is published from a public GitHub repo with a marketplace file.

**Design rules:**

1. Claude never computes the numbers. Every estimate comes from the deterministic CLI. Claude writes policies, runs commands and interprets output.
2. The plugin evaluates; it doesn't collect. Logs come from production systems running the library. The README says this up front.
3. Storage is the library's job. Instrumented code never mentions it, and a logging failure never reaches the agent.

## Milestones

1. **M1: Core.** Vector rewards, arbitrary target policies, DR estimator, log format v2. Re-run the RouterBench study as the reference demo.
2. **M2: Adoption.** Proxy mode, exploration planner, CI gating, the Claude Code plugin, a new landing page built around the demo.
3. **M3: Multi-step.** Trajectory grouping, per-decision weights, ESS warnings, one worked multi-step agent example.

## Risks and open questions

- **Teams won't accept exploration traffic.** Mitigation: frame it as a budget, keep ε small, and show the planner's numbers.
- **ESS collapses on multi-step agents.** Mitigation: scope v1 tightly and warn loudly rather than report false precision.
- **Biased judge rewards.** DR corrects the direct-method model's bias but not bias in the logged reward itself. Document this clearly.
- **Open:** Can propensities be recovered from LLM logprobs when a team won't randomize explicitly?
- **Open:** Does proxy mode belong in this repo or a separate package?
- **Open:** Is this a product, a consulting tool, or both?
