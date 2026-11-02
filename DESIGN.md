# Design Doc: Private Counterfactual Evaluation for LLM Agents

**Working name:** Gittins Counterfactual (GCF)
**Status:** Draft / proposal
**Date:** 2026-10-26
**Builds on:** [Gittins](https://www.getgittins.dev) contextual bandit engine

---

## 1. Summary

This document proposes a system that lets anyone answer the question:

> *"How would this new prompt, model, tool policy, or checkpoint have performed on last month's real agent traffic?"*

without shipping the change, and, in the federated mode, **without any transcript leaving the device or tenant where it was produced.**

It has three layers:

1. **Decision logging for agents.** Agent choice points (route, tool, prompt variant, escalation, retrieval ranking) go through a harness-randomized decision engine (Gittins). Every decision is recorded with an exact propensity, a candidate-set fingerprint, and a trace linkage. This works for closed-API models because the propensity comes from the engine's own randomization, not from token log-probabilities.
2. **Counterfactual estimation.** IPS, SNIPS, and doubly robust (DR) estimators over arbitrary *target policies*, including policies whose scorer is a different LLM or prompt, with mandatory diagnostics (effective sample size, max weight, support checks).
3. **Federated, differentially private aggregation.** Because these estimators are sums, each client computes its share locally and releases only clipped, noised, securely aggregated sufficient statistics. The analyst gets an estimate with a confidence interval and never sees a context.

The intended end state is **counterfactual evaluation on the real deployment distribution as a release gate** for agent changes and, eventually, for model checkpoints.

---

## 2. Motivation

### 2.1 Agent changes are tested by vibes and A/B tests

Teams change system prompts, models, tools and routing constantly. The options today are:

- **Static evals:** cheap, but measured on a distribution that differs from production.
- **Online A/B tests:** faithful, but slow, risky, and they cost real traffic per variant.
- **Vibes:** most common.

Off-policy evaluation (OPE) is the standard answer in contextual bandits: log decisions with propensities, then estimate any new policy's value from those logs. Agents are full of bandit-shaped decisions, yet almost nobody logs them in an OPE-valid way.

### 2.2 Token log-probs are not a reliable source of propensities

The tempting shortcut ("LLMs emit their own propensities via logprobs") does not hold for frontier APIs as of late 2026:

- OpenAI reasoning models do not return logprobs.
- Google stated that Gemini 3.x models no longer return logprobs (working as intended).
- Anthropic's API does not expose logprobs.
- Where logprobs exist, they are often the raw model distribution rather than the post-temperature/top-p *sampling* distribution, and they vary run to run.
- For reasoning models, the probability of the final action marginalizes over hidden reasoning and is not computable from visible tokens.
- Sequence-level probabilities of long outputs make importance weights explode, collapsing effective sample size.

**Design consequence:** propensities must come from an explicit randomization step in the harness, over a small, well-defined action set. That is exactly what a contextual bandit engine provides.

### 2.3 Evals that don't reflect deployment, and privacy

Labs and large deployers want to know how a candidate model behaves on *real* usage, but real usage is private. OPE estimators decompose into sums over decisions, so they can be computed where the data lives and aggregated privately.

---

## 3. Goals and non-goals

### Goals

- G1. Exact, verifiable propensities for agent decisions, independent of model provider.
- G2. Evaluate **arbitrary target policies** (new prompt, new model as scorer, new exploration rule) on logged traffic.
- G3. Handle delayed and trajectory-level rewards (outcome known hours or days later, shared across several decisions).
- G4. Interoperate with OpenTelemetry so existing observability vendors can ingest decision records.
- G5. Federated mode: produce estimates with formal differential privacy guarantees; raw contexts never leave the client.
- G6. Mandatory diagnostics: no estimate is reported without ESS, max weight, and support coverage.
- G7. Public validation: demonstrate that OPE predictions agree with live outcomes.

### Non-goals

- N1. Evaluating changes to the **candidate generator** itself (see §6.4). We evaluate choices among logged candidates.
- N2. Token-level / free-text OPE over full responses.
- N3. Replacing online experiments entirely. GCF triages and gates; high-stakes launches still deserve a confirmatory test.
- N4. Training frontier models. This is an evaluation and decision system.

---

## 4. Background: what Gittins already provides

Gittins is an online contextual bandit engine (Rust core; Python, npm and browser WASM bindings; bit-identical across platforms). Relevant existing properties:

| Existing capability | Why it matters here |
|---|---|
| `decide` returns a record with `decision_id`, `t`, `chosen`, `features`, `propensity`, `candidate_hash`, `model_version`, `salt`, plus `context`, `candidates`, `bits` | Already the core of an OPE-valid decision record |
| Epsilon-greedy with always-on exploration | Guarantees full support: every candidate has propensity ≥ ε/K, so importance weights are bounded by K/ε (critical for DP, §8) |
| `score` / `explore` / `train` callbacks | The scorer can be an LLM call; the random draw stays inside the engine, so the logged propensity is exact |
| Exactly-once resolution, `horizon`, `default_reward`, `expire` | Correct handling of delayed and missing rewards |
| `log_line` canonical JSONL, "recorded not assembled" | Logs are append-only and self-describing |
| `gittins verify` | Recomputes fingerprints, features, propensity bounds, resolution and ordering; rejects bad logs |
| `gittins eval` / `sweep` with IPS, SNIPS, ESS, max weight | Estimation and diagnostics exist, but only for Gittins' internal algorithms |
| `gittins replay` | Rebuilds a deployable state from logs |
| Deterministic, counter-based RNG keyed on decision id and salt | Any decision, and any client's contribution to an estimate, can be replayed and audited |

GCF is mostly **additive**: it extends the record schema, adds a target-policy evaluation interface, adds DR and trajectory estimators, an OTel exporter, and a federated aggregation protocol.

---

## 5. Architecture

```
          ┌──────────────────────── Client / tenant boundary ────────────────────────┐
          │                                                                          │
 user ──► │  Agent harness                                                           │
          │    │                                                                     │
          │    ├─ choice point ──► Gittins.decide(context, candidates, score=LLM)    │
          │    │                        │                                            │
          │    │                        ├─► decision record ──► Local decision log    │
          │    │                        │                        (append-only JSONL) │
          │    │                        └─► OTel span event (gen_ai.decision.*)  ────┼──► Observability
          │    │                                                                     │    (metadata only)
          │    └─ outcome (minutes–days later) ──► Gittins.learn / expire ──► log    │
          │                                                                          │
          │  Local Estimator (on request)                                            │
          │    reads log, re-scores under target policy π_e,                          │
          │    computes clipped sufficient statistics                                │
          │                        │                                                 │
          └────────────────────────┼─────────────────────────────────────────────────┘
                                   ▼
                       Secure aggregation + DP noise
                                   ▼
                         Analyst: V̂(π_e) ± CI, ESS, coverage
```

Three deployment modes, same log format:

| Mode | Where estimation runs | Who sees contexts | Typical user |
|---|---|---|---|
| **Local** | Analyst's machine via CLI | Analyst | Single team, own logs |
| **Tenant** | Inside each customer's environment | Customer only | SaaS agent vendor evaluating a change across customers |
| **Federated device** | On each user's device (WASM) | Nobody outside the device | Consumer apps, model labs |

---

## 6. Decision model for agents

### 6.1 Choice points

A *choice point* is a place in the agent loop where one of a finite set of options is selected. Initial supported types:

| Choice point | Candidates | Example reward |
|---|---|---|
| Model routing | Available models | Task success − λ·cost |
| Prompt variant | System-prompt versions | Resolution, user rating |
| Tool / sub-agent selection | Applicable tools | Step success, final outcome |
| Retrieval ranking (multislot) | Retrieved chunks | Answer groundedness, click |
| Escalation | {continue, ask user, hand to human} | Resolution − escalation cost |
| Response candidate selection | K LLM-generated drafts | Rating, downstream success |

### 6.2 Scoring and exploration

- The **scorer** (`score` callback) produces one estimated reward per candidate. It may be Gittins' built-in ridge model, a learned model, or an LLM prompted to rate candidates.
- The **explorer** (`explore` callback, or built-in epsilon-greedy) turns scores into a distribution.
- The engine samples from that distribution with its own RNG and records the propensity.

Because the draw happens in the engine, the propensity is exact even when the scorer is a non-deterministic closed-API LLM.

### 6.3 Exploration budget

OPE requires support: the logging policy must give non-zero probability to anything the target policy might choose. Each choice point declares an `epsilon` (default 0.05). This is a product-level knob, an explicit "exploration budget" traded against short-term reward. Decisions logged at ε = 0 are flagged as non-evaluable by `verify`.

### 6.4 The candidate-set limitation

When an LLM *generates* the candidates and Gittins chooses among them, propensities are conditional on that candidate set. Consequences:

- ✅ Can evaluate any target policy that chooses among the **same logged candidates** (proven by `candidate_hash`).
- ❌ Cannot evaluate a new generator that would have proposed *different* candidates.

So "would the new prompt be better?" must be framed as a choice the prompt drives over a logged set (which template, which route, which tool), or as a new *scorer* over logged candidates. This is stated in the docs and enforced: a target policy that assigns mass outside the logged candidate set is rejected.

---

## 7. Log schema extensions

Existing Gittins fields are kept unchanged. Proposed additions (all optional, for backward compatibility):

| Field | Type | Purpose |
|---|---|---|
| `choice_point` | string | Stable name of the decision site, e.g. `router.v1` |
| `trace_id` | string | Links all decisions in one agent run; matches the OTel trace id |
| `parent_decision_id` | string \| null | Hierarchical decisions (planner → sub-bandit) |
| `step` | int | Position of the decision within the trace |
| `context_ref` | string | Pointer to the full context (span id, object key) instead of embedding a conversation inline |
| `context_digest` | string | Hash of the full context, so a re-scorer can prove it used the right one |
| `policy_ref` | string | Identifier for the logging scorer configuration (model id, prompt hash, temperature) |
| `scores` | float[] | Logging policy's per-candidate scores (enables DR baselines and audits) |
| `probs` | float[] | Full logging distribution over candidates, not just the chosen propensity |

Resolution records gain:

| Field | Type | Purpose |
|---|---|---|
| `reward_scope` | `"decision"` \| `"trace"` | Whether the reward belongs to one decision or the whole trajectory |
| `reward_components` | object | Optional breakdown, e.g. `{success: 1, cost_eur: 0.012}`, so reward functions can be re-weighted offline |

**Trace-level rewards.** A new `resolve_trace(state, trace_id, reward, t)` call fans one outcome out to every open decision in the trace, preserving exactly-once semantics per decision.

**Context storage.** Inline `context` stays supported for small structured contexts. For conversations, logs store only `context_ref` and `context_digest`; the context lives in the tenant's or device's own store under its own retention policy.

---

## 8. Estimation

Notation: logged decision *i* has context xᵢ, candidate set Aᵢ, chosen action aᵢ, logging propensity pᵢ, and reward rᵢ. The target policy is π_e(a | x, A). Importance weight wᵢ = π_e(aᵢ | xᵢ, Aᵢ) / pᵢ.

### 8.1 Target-policy interface (main new capability)

Today `gittins eval` only evaluates Gittins' internal algorithms. GCF adds an interface for arbitrary target policies:

```python
import gittins.ope as ope

def target_policy(record, context):
    # e.g. the same candidates, scored by a new prompt or model,
    # passed through the same exploration rule
    scores = new_scorer(context, record.candidates)
    return my_explore(scores, epsilon=0.0)   # one prob per candidate

report = ope.evaluate(
    log="decisions.jsonl.gz",
    target=target_policy,
    choice_point="router.v1",
    estimators=["ips", "snips", "dr"],
    reward_model=judge_model,        # needed for DR
    context_store=my_store,          # resolves context_ref
)
print(report)   # values, CIs, ESS, max weight, coverage, warnings
```

Equivalent CLI path for precomputed probabilities:

```
gittins eval --log decisions.jsonl.gz --target-probs new_prompt_probs.jsonl
```

where each line is `{decision_id, probs: [...]}`.

### 8.2 Estimators

- **IPS:** V̂ = (1/n) Σ wᵢ rᵢ
- **SNIPS:** V̂ = Σ wᵢ rᵢ / Σ wᵢ
- **Doubly robust:** V̂ = (1/n) Σ [ Σₐ π_e(a|xᵢ) q̂(xᵢ, a) + wᵢ (rᵢ − q̂(xᵢ, aᵢ)) ]

  where q̂ is a reward model. A natural q̂ for agents is an **LLM judge** that already exists in the team's eval stack; DR then corrects the judge's bias using logged real outcomes.

- **Trajectories (per-decision importance sampling):** for traces whose target policy changes several decisions, weight the reward at step t by the product of weights up to t. Variance grows quickly with the number of changed steps, so the recommended practice is to **evaluate one choice point at a time** and hold others at their logged behaviour (weight 1).

### 8.3 Mandatory diagnostics

Every report includes, and the CLI refuses to print a value without:

- Effective sample size: ESS = (Σ wᵢ)² / Σ wᵢ²
- Max weight and the share of the estimate attributable to the top 1% of weights
- Support coverage: share of decisions where the target puts mass on actions with logged propensity ≥ threshold
- IPS vs SNIPS vs DR agreement
- Confidence intervals (bootstrap in local mode; analytic from DP-noised sums in federated mode)

Warnings fire when ESS < 5% of n, or when estimators disagree beyond their intervals.

---

## 9. Federated, private evaluation

### 9.1 Why it works

IPS, SNIPS, DR and their diagnostics are all functions of a small set of **sums**:

| Statistic | Used for |
|---|---|
| S_wr = Σ wᵢ rᵢ | IPS, SNIPS |
| S_w = Σ wᵢ | SNIPS, ESS |
| S_w2 = Σ wᵢ² | ESS, variance |
| S_dm = Σ Σₐ π_e(a\|xᵢ) q̂(xᵢ,a) | DR |
| S_corr = Σ wᵢ (rᵢ − q̂(xᵢ,aᵢ)) | DR |
| n | everything |
| S_wr2 = Σ (wᵢ rᵢ)² | variance / CI |

Each client computes its own sums; the server only ever needs the totals.

### 9.2 Protocol

1. **Evaluation request.** The analyst publishes an evaluation job: target policy artifact (prompt + model id, or a scorer bundle), choice point, time window, reward function, clipping bounds, and privacy budget.
2. **Local computation.** Each client:
   - verifies its log (`gittins verify`),
   - samples at most *k* decisions per user from the window (bounds per-user contribution),
   - computes π_e for each sampled decision (see §9.4),
   - clips weights at M and rewards to [0, 1],
   - computes its sufficient statistics.
3. **Secure aggregation.** Clients submit masked vectors; the server learns only the sum across clients.
4. **DP noise.** Gaussian noise calibrated to per-user sensitivity is added to the aggregate (central DP via secure aggregation, or local/distributed noise if the server is untrusted).
5. **Release.** The analyst receives V̂ for each estimator, CIs that include DP noise, ESS, and coverage.

### 9.3 Bounded weights make privacy tractable

Unbounded importance weights normally make DP calibration painful. Gittins' always-on epsilon-greedy exploration gives a **hard floor** on propensities: p ≥ ε/K for K candidates, hence w ≤ K/ε even without clipping. With per-user sampling cap k and rewards in [0, 1], the per-user L2 sensitivity of S_wr is at most k·K/ε. This is a direct, practical benefit of the existing Gittins design.

Clipping at M < K/ε further reduces noise at the cost of bias toward the logging policy; the report states the clipping level and the share of clipped weights.

### 9.4 Where target-policy scores come from

The target policy needs π_e(a | x) on the client's own context. Options, in increasing privacy:

| Option | How | Privacy note |
|---|---|---|
| A. Provider scoring call | Client sends the stored context to the model provider for re-scoring | Provider sees the context again (it typically already did at logging time); analyst still sees nothing |
| B. Tenant-hosted model | Re-score inside the customer's own cloud with an open-weight or private deployment | Nothing leaves the tenant |
| C. On-device scorer | Small model or distilled scorer runs on device (WASM / native) | Nothing leaves the device |
| D. Confidential compute | Re-scoring in an attested enclave | Context visible only inside the enclave |

Many valuable target policies don't need an LLM at all: epsilon or forgetfulness changes, routing thresholds, cost-weighting of rewards, or a learned scorer over hashed features all run trivially on device.

### 9.5 Auditability

Gittins' determinism makes each client's contribution replayable: given the same log, target artifact and seed, the local estimator must output bit-identical statistics. Clients can be challenged on samples (in tenant mode) and contributions verified, which matters if the estimate gates a release.

---

## 10. OpenTelemetry integration

Decision records are exported as span events on the agent's existing spans, using a proposed `gen_ai.decision.*` namespace that sits alongside the OTel GenAI agent conventions (`invoke_agent`, `execute_tool`, etc.):

| Attribute | Value |
|---|---|
| `gen_ai.decision.id` | `decision_id` |
| `gen_ai.decision.choice_point` | `choice_point` |
| `gen_ai.decision.num_candidates` | K |
| `gen_ai.decision.chosen_index` | `chosen` |
| `gen_ai.decision.propensity` | `propensity` |
| `gen_ai.decision.candidate_hash` | `candidate_hash` |
| `gen_ai.decision.policy_ref` | `policy_ref` |
| `gen_ai.decision.epsilon` | configured ε |

Rewards are emitted as `gen_ai.decision.reward` events, linked by `decision_id` or `trace_id`.

Contexts and candidate contents are **not** exported by default, consistent with OTel's opt-in content capture. This lets observability vendors show decision dashboards and coverage without becoming custodians of conversation data, and gives them a clean path to offer "counterfactual eval" features on top.

---

## 11. Validation plan

The project's credibility rests on showing predictions match reality.

**Study design**

1. Take an open-weight agent on a public task suite with automatic success signals.
2. Wrap 3–4 choice points (model routing, tool choice, prompt variant, escalation) with Gittins at ε = 0.05–0.1.
3. Run a logging period of N tasks.
4. Define 15–25 candidate changes (new prompts, a different routing model, cost re-weighting, new scorer).
5. For each, compute OPE estimates (IPS, SNIPS, DR) **before** running it.
6. Run each change live on fresh traffic and measure actual value.
7. Publish the predicted-vs-actual plot, rank correlation, interval coverage, and the ESS at which estimates became reliable.

**Federated replication**

Repeat steps 5–7 with the data partitioned across simulated clients, DP noise at several privacy budgets, and report the accuracy/privacy curve.

**Success criteria (initial)**

- Spearman rank correlation between predicted and actual ≥ 0.8 across changes.
- ≥ 90% of actual values inside the 95% DR interval when ESS ≥ 500.
- At a fixed per-user budget, federated estimates rank the top change correctly ≥ 90% of the time.

---

## 12. Milestones

| Phase | Deliverable |
|---|---|
| M1 | Schema extensions (§7), `resolve_trace`, `verify` updates |
| M2 | Target-policy `evaluate` API + `--target-probs` CLI; DR estimator; diagnostics gate |
| M3 | Agent SDK adapters (Python and TypeScript) exposing a `choose()` primitive |
| M4 | OTel exporter and proposed `gen_ai.decision.*` attribute spec |
| M5 | Public validation study (§11) |
| M6 | Federated estimator: local sufficient statistics, secure aggregation reference, DP accounting |
| M7 | Federated validation and accuracy/privacy curves |

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Teams refuse any exploration cost | Small ε, exploration only on low-stakes choice points, show the value of the resulting evals |
| High variance makes estimates useless | One choice point at a time, coarse actions, DR with judges, mandatory ESS gating |
| Context drift between logging and evaluation (tools or catalogues changed) | `candidate_hash`, `context_digest`, and refusing target policies that need unlogged candidates |
| LLM judge used in DR is biased | DR stays unbiased if propensities are correct; report DR vs IPS divergence as a judge-quality signal |
| Non-stationarity: last month ≠ next month | Report estimates over sliding windows; flag trends; keep confirmatory online tests for big launches |
| DP noise swamps signal at small scale | Federated mode targeted at large deployments; tenant mode for smaller ones |
| Non-deterministic scorer callbacks break auditability | Log `scores` and `probs` at decision time; audits use logged values, not re-execution |
| Reward hacking via chosen reward function | Log `reward_components`; reward functions are part of the evaluation job and reviewable |

---

## 14. Open questions

1. Should the target-policy interface accept stochastic policies only, or also deterministic ones (valid under full support, but higher variance)?
2. What is the right default per-user contribution cap *k* for consumer apps?
3. Can a provider-side "rescoring" endpoint (teacher-forced probability of a fixed choice under a new prompt or model) be standardized, so option A in §9.4 doesn't depend on any single vendor?
4. How should trajectory rewards be attributed when a target policy changes an early decision that alters which later choice points are even reached?
5. Is there demand for a hosted aggregation service, or should the protocol stay reference-implementation-only?

---

## 15. References

- Gittins documentation: https://www.getgittins.dev
- OpenTelemetry GenAI semantic conventions (agent spans: `create_agent`, `invoke_agent`, `execute_tool`; status in development as of 2026)
- Dudík, Langford, Li (2011). *Doubly Robust Policy Evaluation and Learning.*
- Swaminathan, Joachims (2015). *The Self-Normalized Estimator for Counterfactual Learning.*
- Precup, Sutton, Singh (2000). *Eligibility Traces for Off-Policy Policy Evaluation* (per-decision importance sampling).
- Saito, Joachims (2022). *Off-Policy Evaluation for Large Action Spaces via Embeddings.*
- Thomas, Theocharous, Ghavamzadeh (2015). *High-Confidence Off-Policy Evaluation.*
- Bonawitz et al. (2017). *Practical Secure Aggregation for Privacy-Preserving Machine Learning.*
- Dwork, Roth (2014). *The Algorithmic Foundations of Differential Privacy.*