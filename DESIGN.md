# whittle: design doc

> **Status:** draft, pre-implementation
> **Working name:** `whittle` (after the Whittle index, the restless-bandit generalisation of the Gittins index). Placeholder; rename freely.
> **Author:** Max Pagels
> **Last updated:** 2026-10-17

## 0. Start here: v0

Everything below is the roadmap. What exists today is one file, `.whittle/whittle.py` (~70 lines, one dependency: `gittins`), wired into this repo's Claude Code via `.claude/settings.json`:

- **Arms are files.** Each `.whittle/arms/*.md` is an arm; `baseline.md` is empty. Add an arm by adding a file. Edit an arm's text and you have, in effect, a different arm (§10.3 is the eventual fix).
- **`SessionStart`:** gittins picks an arm (context: model, hour) and the hook prints its text into the session. Resumed or compacted sessions get the same arm again.
- **`UserPromptSubmit`:** a prompt of just `+` or `-` rewards the session's decision with 1 or 0. The prompt is blocked, so Claude never sees it. Each session can be rated once.
- **`PostToolUse`** (Edit/Write/MultiEdit) records the lines Claude adds. Lines Claude later replaces itself don't count against it. Edits made through Bash (`sed` and the like) aren't seen.
- **Unrated sessions are scored automatically.** At the first `SessionStart` at least 8h after a session started, whittle settles it. The score is the share of Claude's lines still in their files (retention). It loses 0.15 for each interrupt, rejected tool call and correcting prompt ("no", "revert", "don't"…), counting at most 3; with no edits, the base is 0.5. These weights are guesses. A `+`/`-` rating always takes precedence, and a session scored this way can't be rated afterwards. Every settled session, rated or not, appends its signals to `.whittle/signals.jsonl`, so the weights can later be fitted against ratings (§16.1).
- **Sessions with no signal** (no edits, no friction), or that aren't settled within 24h, expire and train as 0.5.
- **`/whittle`** (`.claude/commands/whittle.md`) runs `whittle.py status`. Per arm, it shows rated, auto-scored and expired sessions and the mean reward, plus the arm the state prefers at each hour. The verdict is "not enough data" until every arm has 50 scored sessions; after that it adds `gittins eval` if the CLI is on PATH. Open sessions are counted only as a total, so the current session's arm isn't shown. `status` reads files and changes nothing.
- **The log is `.whittle/log.jsonl`**, a plain gittins experience log. `gittins verify` and `gittins eval --bits 18 --epsilon 0.2` run on it directly. Settings: ε = 0.2, `bits` = 18 (8 collides the arms' hashed features, see `.whittle/simulate.py`; 18 leaves room for more arms and context values).
- **Fail open:** any error means nothing is injected and nothing is logged.

Grow it only when using it shows the need: plugin packaging when it's wanted in a second repo, proxy rewards (§7) when `+`/`-` turns out too sparse, more sites when one has visibly learned something.

## 1. Summary

`whittle` plugs into an agent harness, starting with Claude Code, and **learns which agent configuration works best in which situation**. It does this with a contextual bandit, running [gittins](https://getgittins.dev) under the hood.

At defined points in a session (session start, each prompt, each subagent launch) `whittle` asks gittins to choose among a small set of user-defined options: instruction variants, workflow nudges, context snippets. It applies the choice through the harness, watches what happens, turns the outcome into a reward and feeds it back to gittins. Over time gittins shifts towards the options that pay off, conditioned on the context: the repo, the kind of task, the size of the prompt.

Every choice is randomised with a logged, exact propensity, so the same log that drives learning is also an off-policy evaluation (OPE) dataset. That gives `whittle` its second job: **answering "is the learned policy actually better than what I had?" from logged behaviour**, and refusing to answer when the data can't support it.

`whittle` is a harness adapter and a reward layer, not a bandit library. Exploration, learning, the decision log, exactly-once reward resolution and OPE estimators belong to gittins. `whittle` owns what is specific to agents: where decisions happen, how they are applied, how context is featurised, how outcomes become rewards, and when an estimate is safe to report.

## 2. Motivation

People tune their agents by hand. They edit `CLAUDE.md`, add instructions, try a workflow for a few days, and form an impression of whether it helped. That process has three problems:

1. **No counterfactual.** You only see the sessions that ran with the new setup, on whatever tasks happened that week.
2. **No context.** An instruction that helps on a large refactor may hurt on a one-line fix. Hand tuning picks one global setting.
3. **No memory.** Impressions fade, settings drift, and nothing records why a change was made.

A contextual bandit addresses all three. It randomises (counterfactual), it conditions on features (context), and it logs every decision with its propensity (memory). The obstacle has been plumbing: harnesses don't expose decision points, outcomes are delayed and noisy, and bandit libraries aren't built for a process that lives for half a second inside a hook.

gittins removes most of the bandit-side obstacle: it is a value, not a service. State is one serialisable string, decisions are deterministic given a salt, and the log is the OPE dataset. `whittle` removes the harness-side obstacle.

See also *Off-policy Estimation in the Agentic Era* (maxpagels.com/ope-llm, July 2026).

## 3. Goals and non-goals

### Goals

- G1. Install into Claude Code as a plugin in one command and start collecting data with no code changes.
- G2. Let users declare **decision sites** (where a choice happens) and **arms** (the options) in a config file.
- G3. Make every decision through gittins, so every propensity is exact and every decision is replayable.
- G4. Turn harness events and delayed outcomes into rewards, distinguishing *missing* from *zero*.
- G5. Learn online, with a permanent baseline arm and guardrails that keep exploration cheap.
- G6. Report whether the learned policy beats the baseline, using gittins' OPE, and refuse when overlap or reward coverage is too thin.
- G7. Never break the host harness. A `whittle` failure degrades to the baseline arm, silently.

### Non-goals

- Implementing bandit algorithms or OPE estimators. gittins does that.
- Generating new arms. In v1 humans write arms; `whittle` chooses among them. (LLM-proposed arms are a later idea, §17.)
- Controlling what the harness doesn't expose. In Claude Code, hooks cannot choose the model or rewrite the user's prompt, so `whittle` doesn't either (§6.3).
- Evaluating free-text output quality. Use eval sets or LLM-as-judge; at most they become one reward signal.
- Being a tracing platform.

## 4. Core concepts

| Term | Meaning |
|---|---|
| **Harness** | The agent runtime `whittle` plugs into: Claude Code first, the Agent SDK and custom agents later. |
| **Decision site** | A named, stable point where a choice is made, bound to a harness event (e.g. `cc.session_instructions` on `SessionStart`). Each site owns one gittins state. |
| **Arm** | One option at a site: an id, a payload (usually text to inject), and optional features. |
| **Baseline arm** | The "no change" arm. Every site has one. All comparisons are against it. |
| **Context** | Features of the situation at decision time: repo, language, prompt length, task type, time of day. A flat dict, because that is what gittins consumes. |
| **Episode** | The unit a reward is attributed to: usually a session, sometimes a single prompt-to-stop turn. |
| **Signal** | A raw observation that might bear on reward: tests passed, user interrupted, tool errors, explicit rating, commit made. |
| **Reward function** | Per-site mapping from an episode's signals to one number. |
| **Mode** | A site's lifecycle state: `collect`, `learn`, `frozen` or `off` (§9). |

A site is the unit of everything: one gittins state, one reward function, one horizon, one evaluation.

## 5. Architecture

```
 Claude Code ──hook event──▶ [1] harness adapter ──▶ [2] site router ──▶ [3] featuriser
      ▲                                                                       │
      │ additionalContext                                                     ▼
      └──────────────────── [5] apply ◀── arm ◀── [4] gittins.decide (state per site)
                                                              │
                                                              ▼
                                         gittins log  +  whittle sidecar   (local, append-only)
                                                              ▲
 harness events ─▶ [6] signal collector ─▶ [7] reward fn ─▶ [8] resolver ─▶ gittins.learn / expire
 external outcomes (git, CI, ratings) ──┘
                                                              │
                                                              ▼
                               [9] evaluator (gittins eval + whittle diagnostics) ─▶ report / mode gate
```

Decision path (1–5) runs inside a hook process and must be fast. Reward path (6–8) runs at episode end, mostly asynchronously. Evaluation (9) runs on demand.

### 5.1 What gittins provides, and what `whittle` adds

| Concern | gittins | `whittle` |
|---|---|---|
| Exploration and choice | `decide`, ε-greedy with deterministic salted draws, exact propensity | Which arms are valid at this site right now |
| Learning | Built-in linear model over hashed features, with forgetting | Nothing; uses gittins' learner |
| Feature encoding | Hashes flat dicts into 2^bits slots | Turns harness input into that flat dict |
| Reward ledger | `learn` exactly once, `expire` at horizon | Collects signals, computes reward, maps episode to decision ids |
| State | `serialize` / `deserialize` | File-locked load/decide/save per site, across concurrent sessions |
| Log | Canonical JSONL via `log_line`, `verify` | Sidecar with harness-specific data (§10) |
| OPE | `eval`: IPS, SNIPS, ESS, max weight | Reward coverage, missing-vs-expired handling, ok/warn/refuse, mode gates |
| Pooling | `replay` merged logs into one state | Team pooling workflow (§11.3) |

## 6. Claude Code integration

### 6.1 Packaging

`whittle` ships as a Python package plus a Claude Code plugin that the package installs:

```
pip install whittle        # or: uv tool install whittle
whittle init               # writes .whittle/sites.toml, installs/enables the plugin
```

The plugin is thin. `hooks/hooks.json` points each relevant event at one entrypoint, `whittle hook <event>`, which reads the hook's JSON from stdin and writes the hook's JSON to stdout. All logic lives in the Python package so it can be tested without Claude Code.

State and logs live under `${CLAUDE_PLUGIN_DATA}/whittle/` for user-global sites, or `.whittle/` in the project for project-scoped sites. Nothing leaves the machine unless the user exports it (§11.3).

### 6.2 Hook events used

| Event | Role in `whittle` |
|---|---|
| `SessionStart` | Decide session-level sites; inject the chosen arm via `additionalContext`. Opens the episode. Input includes `model` and `effort`, which become features. |
| `UserPromptSubmit` | Decide prompt-level sites; inject via `additionalContext`. Featurise the prompt. Also catches explicit feedback commands (§7.2). |
| `SubagentStart` | Decide subagent-level sites (instructions per `agent_type`). |
| `PostToolUse`, `PostToolUseFailure` | Signals: tool errors, test runs and their results, edits made. Never blocks. |
| `Stop` | Signals: turn finished, `last_assistant_message`. Closes a turn-level episode. Run `async`. |
| `SessionEnd` | Closes the session episode; parses the transcript for remaining signals. Run `async`: the synchronous budget is 1.5s. |

`additionalContext` is capped at 10,000 characters per hook, which bounds arm payloads.

### 6.3 What can and cannot be an arm in Claude Code

Hooks can **add context** at session start, per prompt and per subagent. They cannot choose the model, set effort, or rewrite the user's prompt. That gives three kinds of site in v1:

- **Instruction variants.** "Plan before editing" vs "write a failing test first" vs baseline. The highest-value site, and the default one `whittle init` creates.
- **Workflow nudges.** Suggest a specific skill or subagent for this prompt, or none.
- **Context selection.** Which project notes or doc snippets to inject. Choosing k of n is a slate; v1 treats each combination as one arm and keeps n small (§12.2).

Choices hooks can't make (model, effort, permission mode) can be made at launch by a wrapper, `whittle run -- claude ...`, which decides a session-level site and passes the flags through. This is a later milestone; it changes the user's workflow and needs care.

Out of scope for Claude Code: steering individual tool choices. `PreToolUse` can deny a call, but exploring by veto is disruptive and the resulting propensities are hard to interpret. Tool-level decisions belong in custom agents (§12.1).

### 6.4 Latency and failure

The decision path runs on every session start and every prompt, so it must be fast and must not fail visibly:

- The `whittle hook` import path loads only the stdlib and the `gittins` wheel. Target: under 150ms end to end, most of it interpreter startup. gittins' own decision cost is negligible.
- Each site's state is a file. A decision is: take an advisory lock, `deserialize`, `decide`, `serialize`, write atomically, release, append the log lines. Concurrent Claude Code sessions serialise on the lock; the critical section is milliseconds.
- **Fail open.** Any exception, lock timeout or corrupt state means the baseline arm is applied and *no decision is logged*. A decision that wasn't randomised must not enter the OPE log. Errors go to a local `whittle.err.log` and show up in `whittle status`.
- `UserPromptSubmit` hooks time out at 30s; `whittle` sets its own internal deadline far below that and falls back to baseline.

### 6.5 Other harnesses

The harness adapter is an interface: map native events to `episode_start`, `decision_point`, `signal`, `episode_end`, and provide an `apply(arm)` that injects the payload. Claude Code is the first implementation. The Agent SDK is the natural second: same event model, but with more control (model choice, tool choice), which unlocks §12.1.

## 7. Rewards

Reward design is where this system succeeds or fails. The bandit will optimise whatever number it is given, including a bad one.

### 7.1 Signals

Collected per episode from hook inputs and the transcript at `transcript_path`:

| Signal | Source | Notes |
|---|---|---|
| `rating` | Explicit user feedback (§7.2) | Strongest signal, sparsest. |
| `interrupts` | Transcript | User stopped Claude mid-turn. |
| `corrections` | Transcript, `UserPromptSubmit` | Follow-up prompts that look like corrections ("no", "that's wrong", "revert"). Heuristic. |
| `tool_errors` | `PostToolUseFailure` | Count and rate. |
| `tests` | `PostToolUse` on test commands | Last test run's pass/fail. Matcher is configurable per project. |
| `committed` | git, via `whittle reward` from a post-commit hook | Delayed. |
| `ci_passed`, `pr_merged` | External, via `whittle reward` | Delayed by hours or days. |
| `turns`, `tokens`, `duration` | Transcript | Cost signals. Usually penalties, never the whole reward. |

### 7.2 Explicit feedback

A plugin command `/whittle:rate <1-5>` and the CLI `whittle rate <1-5> [--session id]`. Ratings are optional; the reward function must work without them. Asking for them is a nudge in `whittle status`, never an interruption.

### 7.3 Reward functions

Each site declares a reward function: a weighted combination of signals with explicit handling for absent ones.

```toml
[site.cc_session_instructions.reward]
episode = "session"
horizon = "24h"
terms = [
  { signal = "rating",      weight = 1.0, scale = [1, 5] },
  { signal = "tests",       weight = 0.5 },
  { signal = "corrections", weight = -0.2, per = "count", cap = 3 },
  { signal = "interrupts",  weight = -0.3, per = "count", cap = 3 },
]
require_any = ["rating", "tests", "committed"]   # otherwise the episode reward is missing
```

`require_any` matters. A session with no informative signal is *missing*, not a zero and not a neutral half. Missing episodes are resolved in gittins as expired (§7.4) and reported as missing coverage.

Built-in presets ship for common cases (`session_outcome`, `turn_outcome`), and arbitrary Python reward functions are allowed for users who want them.

### 7.4 Resolution through gittins

- Each decision's id is stored in the sidecar against its episode id (Claude Code `session_id`, plus turn index for turn episodes).
- At episode end the resolver computes the reward. If enough signal is present, it calls `gittins.learn(state, decision_id, reward, t)`. For an episode with several decisions (one per prompt at a prompt-level site) each gets the episode reward; §8 covers why that is acceptable.
- Delayed signals (`committed`, `ci_passed`) mean the reward may not be final at `SessionEnd`. Sites using them hold resolution until the signal arrives or the horizon passes. gittins resolves each decision exactly once, so `whittle` never sends a provisional reward it might later want to revise.
- Decisions still unresolved at the horizon are resolved by `gittins.expire`, which trains them as gittins' `default_reward`. **`whittle` treats `kind == "expired"` as missing for reporting**, never as an observed zero. The per-site `default_reward` is set deliberately (usually the baseline arm's expected reward, so silence doesn't systematically punish whichever arm was chosen), and the report shows the expired share.

### 7.5 Reward hacking

Proxies get gamed. "Fewer corrections" rewards an agent that gives up early. "Fewer tokens" rewards doing less. Mitigations:

- Cost signals are capped and can't dominate the reward.
- `whittle status` shows per-arm signal breakdowns, not just reward, so a shift in *how* an arm wins is visible.
- Arms are human-written text, so the bandit can only pick among behaviours someone chose to allow.

## 8. Credit assignment across sites

A session may pass through several sites. Two problems arise.

**Several decisions per episode.** A prompt-level site may decide ten times in one session, and a session-level outcome is shared across all ten. This is noisy but not biased for the question the site answers: "which arm should I pick at prompts like this, given everything else stays as it is?". Where turn-level signals exist (`tests` after this turn, `corrections` in the next prompt) a turn episode is better, and the reward config can say so.

**Several sites learning at once.** If two sites learn simultaneously, each sees the other as part of a changing environment. gittins' forgetting tolerates slow drift, but evaluation gets murky. Default: **at most one site per harness in `learn` mode**. Others can be in `collect` (randomising, logging, not learning), which keeps their data valid for later OPE. A user can override this; the report then flags it.

Joint decisions (choose instructions *and* a context snippet together) are better modelled as one site with combined arms, or as a two-level hierarchy using gittins' hierarchical pattern (router picks a category, leaf picks within it; joint propensity is the product).

## 9. Lifecycle, modes and guardrails

### 9.1 Modes

| Mode | gittins configuration | Purpose |
|---|---|---|
| `collect` | ε = 1.0: uniform over arms, learning off | A randomised experiment. Produces the cleanest possible OPE data. |
| `learn` | ε = site setting (default 0.1), learning on | The bandit optimises. Exploration never drops below ε. |
| `frozen` | Small ε, `train` callback is a no-op | Exploits the learned state without changing it; keeps logging so drift can be detected. |
| `off` | No decisions; baseline applied | |

### 9.2 Default lifecycle

1. `whittle init` creates one site in `collect` with a baseline and two or three starter arms.
2. After N resolved episodes (default 50 per arm), `whittle eval` uses gittins' OPE to estimate how a `learn`-mode configuration would have done on that log, against the baseline arm's observed mean.
3. If the report is `ok` and the estimate isn't worse than baseline, `whittle promote <site>` moves the site to `learn`. Promotion is a user action in v1, never automatic.
4. In `learn`, a **regression guard** compares the realised reward of non-baseline choices with the baseline arm's realised reward on the exploration traffic, both of which keep accruing because ε never reaches 0. If the learned policy falls below baseline by a configured margin over a window, the site drops back to `collect` and `whittle status` says why.

### 9.3 Cost of exploration

For a single developer, exploration costs their own time: a worse instruction variant in some fraction of sessions. Mitigations: few arms, conservative ε in `learn`, a guaranteed baseline, and `collect` mode limited to arms the user wrote and accepted. `whittle status` reports how many sessions ran a non-baseline arm in the last week.

## 10. Data model

### 10.1 The gittins log is canonical

Each site appends gittins' own `decision` and `resolution` lines, written with `gittins.log_line`, to `logs/<site>.jsonl`. That file passes `gittins verify` and can be fed straight to `gittins eval`, `sweep` and `replay`. `whittle` never writes it by any other route.

The gittins context is the featuriser's output: a flat dict of strings, numbers, booleans and None. Arms are gittins candidates: `(arm_id, arm_features)`.

### 10.2 The sidecar

The gittins log parser rejects unknown event types and validates each event strictly, so harness data goes in a sidecar, `logs/<site>.sidecar.jsonl`, keyed by `decision_id`:

```python
@dataclass(frozen=True)
class DecisionMeta:
    decision_id: str             # gittins id, "{salt}:{seq}"
    site: str
    harness: str                 # "claude_code"
    episode_id: str              # session_id[:turn]
    mode: Literal["collect", "learn", "frozen"]
    arm_payload_hash: str        # detects arm text edited after the fact
    raw_input: dict | None       # hook input; redacted before write; optional
    whittle_version: str
    schema_version: int

@dataclass(frozen=True)
class EpisodeOutcome:
    episode_id: str
    signals: dict[str, float | None]
    reward: float | None         # None = missing
    resolved_via: Literal["learn", "expire"]
```

A `DecisionRecord` in `whittle`'s Python API is a read-time join of gittins decision, gittins resolution, `DecisionMeta` and `EpisodeOutcome`.

### 10.3 Arms are versioned

Editing an arm's text changes what it means. `whittle` hashes each arm's payload. If the text changes, the arm gets a new id (`plan-first@2`). The old id remains in the log for history but is no longer offered, and evaluation treats the two as different actions.

## 11. Evaluation and reporting

### 11.1 What gittins answers

`gittins eval --log logs/<site>.jsonl --bits B --epsilon E` estimates how a gittins learner configuration would have performed on the logged traffic (IPS, SNIPS, logged mean, ESS, max weight). `gittins sweep` compares a grid of `epsilon` and `forgetfulness`. This covers the main questions for `whittle` v1: should this site move to `learn`, and with what settings?

### 11.2 What `whittle` adds

Per site, the report adds:

- **Baseline comparison.** The estimate against the baseline arm's realised mean, not just the logging policy's.
- **Reward coverage.** Share of decisions observed, missing (expired) or pending. A high missing share means a poor reward function, not a quiet week.
- **Arm and context coverage.** Episodes per arm, and per arm within the main context buckets.
- **Concurrent learners.** Whether other sites were in `learn` during the logging window (§8).
- **Arm drift.** Whether arm payloads changed during the window (§10.3).

It ends in one of three states:

- **ok:** estimates shown with intervals.
- **warn:** estimates shown, with the failing diagnostic named next to them.
- **refuse:** no point estimate, only the diagnostic that failed. Default triggers: ESS below a floor, missing share above a threshold, fewer than N resolved episodes per arm.

The defaults are conservative on purpose. A tool that confidently reports a wrong number is worse than no tool.

### 11.3 Pooling across a team

One developer produces maybe 10–30 sessions a day, which is slow for learning. A team sharing a site definition can pool data: each member exports their site log (`whittle export --site X`), logs are merged, and `gittins replay` rebuilds a shared state, which is distributed back as a plain file. This is gittins' fleet-pooling pattern, unchanged. Salts include a per-machine id so decision ids never collide. Pooling is opt-in and exports only the gittins log and the outcome sidecar, never `raw_input`.

## 12. Beyond Claude Code hooks

### 12.1 Decisions inside custom agents

In agents that the user writes (Agent SDK or plain Python) `whittle` can reach decisions that Claude Code hooks can't: tool choice, model routing, retrieval selection. The mechanism is a thin wrapper around `gittins.decide`:

```python
from whittle import explore

choice = explore(
    site="support.tool_choice",
    arms=valid_tools + ["__answer__"],
    greedy=lambda: call_model(messages, tools=valid_tools, temperature=0),
    parse=lambda resp: resp.tool_calls[0].name if resp.tool_calls else "__answer__",
    force=lambda arm: call_model(messages, tools=valid_tools, tool_choice=arm),
    context=featurise(messages),
)
```

`greedy`'s pick is passed to gittins as a one-hot `score`. gittins' ε-greedy then gives exactly π(a | x) = (1 − ε)·𝟙[a = a*] + ε/k, sampled and logged by gittins. On an exploration step that picks a different arm, `force` is called to produce that arm's arguments, so exploration costs about ε·(1 − 1/k) extra model calls per decision. Safe subsets (never explore `refund`) use a custom gittins `explore` callback that spreads ε only over the safe arms. Two points to remember:

- **"Answer directly" is an arm.** Leaving it out biases every estimate.
- **Temperature-0 isn't perfectly deterministic.** The sidecar records `a*`, so periodic re-scoring can measure how often it would flip.

### 12.2 Slates

Context selection and parallel tool calls choose k of n. gittins has no slate API. v1 treats each allowed combination as one arm and keeps n small; a hierarchy (§8) covers some cases. Real slate support depends on gittins.

### 12.3 Evaluating a change before shipping it

The original motivation for this project still applies: estimate whether a new prompt or model improves agent-internal decisions before deploying it, by scoring the candidate's action distribution on logged contexts (π₁(a | x)) with one short model call per decision and no tool execution. This needs the stored model input (sidecar `raw_input`) and one gittins feature that doesn't exist yet: evaluation against **externally supplied target probabilities** (§14). It is planned for after the online loop works.

Propensities reconstructed from logprobs (for traces `whittle` didn't log itself) are also deferred. They are approximate at best, and they would need gittins to accept decisions it didn't make (§14).

## 13. Package layout

```
whittle/
  core/          config.py (sites.toml), ids.py, records.py (DecisionMeta, EpisodeOutcome, joins)
  engine/        state.py (locked load/decide/save), log.py (gittins log + sidecar writers)
  sites/         site.py (Site, Arm, modes), presets.py
  features/      base.py, claude_code.py (prompt, repo, session features)
  rewards/       signals.py, functions.py (weighted terms, presets), resolver.py (learn/expire)
  harness/
    base.py      adapter interface
    claude_code/ hook.py (stdin/stdout entrypoint), transcript.py, plugin/ (hooks.json, commands/)
    sdk/         explore.py (§12.1)
  evaluate/      gittins_eval.py, diagnostics.py, report.py, guard.py (regression guard)
  export/        pool.py, vw.py
  testing/       fake_harness.py, fake_agent.py
  cli.py
tests/
docs/
```

Dependencies: `gittins` and the stdlib on the hook path. CLI extras (`whittle[cli]`) for richer output; provider SDKs only for §12. Python ≥ 3.10, `uv`, `pytest` + `hypothesis`, MIT, versioned docs.

CLI:

```
whittle init                         # sites.toml + plugin
whittle status                       # per-site mode, traffic, coverage, errors
whittle hook <event>                 # plugin entrypoint (internal)
whittle rate <1-5> [--session id]    # explicit feedback
whittle reward --signal committed [--session id | --cwd .]   # delayed signals
whittle eval <site> [--sweep]        # gittins eval + whittle diagnostics
whittle promote|freeze|off <site>    # mode changes
whittle export --site <site>         # for pooling or VW
```

## 14. Dependencies on gittins

What v0.1 needs from gittins already exists: `create`, `decide` with `score`/`explore` callbacks, `learn`, `expire`, `serialize`, `log_line`, and `verify`/`eval`/`sweep`/`replay`. Later milestones need:

1. **External target policies in `eval`.** gittins OPE currently evaluates only its own learner configurations. §12.3 needs `evaluate(events, target=...)` with per-decision π₁ supplied by the caller.
2. **Logging decisions gittins didn't make.** For routers that sample themselves, logprob-derived propensities and imported traces: something like `gittins.record(context, candidates, chosen, propensity, t, salt)` that produces a verifiable log line. Today `verify` recomputes hashes with gittins' internal encoding, so `whittle` can't write these lines itself.
3. **Extension fields or a sidecar convention** in the log, so harness metadata can travel with it.
4. **Slates**, eventually (§12.2).

## 15. Testing strategy

- `testing.fake_harness` replays scripted Claude Code hook payloads (session start, prompts, tool results, stop, end) through `whittle hook`, with a simulated user whose reward depends on arm and context in a known way.
- **Learning tests:** over many seeds, a site in `learn` converges to the known-best arm per context bucket and beats baseline.
- **OPE tests:** with `collect` logs, `whittle eval` estimates match the simulated true value of the evaluated configuration.
- **Resolver property tests:** every decision is resolved at most once, no reward attaches to the wrong decision, missing never appears as an observed zero in reports.
- **Fail-open tests:** a corrupt state file, lock contention or exception in a reward function yields the baseline arm and no logged decision.
- **Latency test:** `whittle hook UserPromptSubmit` stays under budget on CI hardware.
- **Golden logs:** a fixed fake session produces a gittins log that passes `gittins verify` and a byte-identical serialised state (gittins' determinism makes this cheap).

The gittins repo has a simulator (`sim/`). `fake_harness` should reuse its environments where possible.

## 16. Open questions

1. **Reward without ratings.** Which proxies actually correlate with "that session went well"? This needs a small study: collect ratings and proxies together, then fit default weights.
2. **Episode boundaries.** Sessions are resumed, forked and compacted. What exactly is an episode for a resumed session?
3. **Featurisation of prompts.** Cheap heuristics (length, keywords, file mentions) vs a local embedding hashed into a few buckets. Embeddings add latency and a dependency on the hook path.
4. **Arm drift and CLAUDE.md interaction.** Arms inject text alongside the user's own `CLAUDE.md`, which also changes over time. Should `CLAUDE.md` changes be a context feature, or reset sites?
5. **Low traffic.** How few arms and features keep a single developer's site learnable within weeks? Defaults need evidence from simulation.
6. **Name.** `whittle` is a placeholder.

## 17. Milestones

**v0.1: Claude Code, collect and learn one site**
- Plugin with `SessionStart`, `UserPromptSubmit`, `PostToolUse(Failure)`, `Stop`, `SessionEnd` hooks
- Instruction-variant sites from `sites.toml`; locked state; fail-open
- gittins log + sidecar; signals from hooks and transcript; `rate`
- Reward functions with presets; resolver over `learn`/`expire`
- `collect` → `learn` via `whittle eval` and `promote`; regression guard
- `fake_harness` end-to-end tests

**v0.2: better rewards and more sites**
- Delayed signals (`committed`, CI) via `whittle reward`
- `SubagentStart` sites, workflow-nudge sites
- `frozen` mode, `gittins sweep` integration, team pooling
- Reward-proxy study (§16.1) informs preset weights

**v0.3: beyond hooks**
- Agent SDK harness and `explore` for agent-internal decisions (§12.1)
- `whittle run` launcher for model and effort sites
- VW export

**Later (depends on gittins, §14)**
- Target scoring of candidate prompts and models (§12.3)
- Logprob propensities and trace import
- Slates, LLM-proposed arms evaluated offline before being offered

## 18. Risks

- **Bad rewards.** The bandit optimises the wrong thing very effectively. Mitigation: `require_any`, capped cost terms, per-arm signal breakdowns, a rating study before tuning defaults.
- **Too little data.** Individual developers may never reach statistical clarity. Mitigation: few arms, coarse features, `collect` before `learn`, team pooling, honest refusals.
- **Breaking the user's flow.** A slow or failing hook is worse than no `whittle`. Mitigation: stdlib-only hook path, latency budget in CI, fail-open to baseline.
- **False confidence.** Reports trusted beyond the data. Mitigation: conservative refusal defaults; coverage and diagnostics in every report.
- **Harness churn.** Hook events and fields change. Mitigation: everything Claude Code-specific lives in `harness/claude_code/`, pinned with recorded hook payload fixtures.
- **Privacy.** Prompts and transcripts are sensitive. Mitigation: local-only by default, `raw_input` optional and redacted, pooling never exports it.
