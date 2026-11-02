---
description: Find an agent's choice points (model, prompt variant, retrieval depth, tool policy) and add Whittle decide/learn calls so production traffic can evaluate changes. Use when the user wants to instrument an agent or start collecting evaluation data.
argument-hint: "[path or choice point]"
---

# Instrument an agent with Whittle

Whittle randomizes a small share of an agent's choices and logs them, so a later change can be evaluated from real traffic. The user should not have to think about storage: never add paths, log files, state objects, or storage configuration to their code.

Focus: $ARGUMENTS

## 1. Check it is a fit

The library is Python only. If the agent is not Python, stop and say so.

## 2. Find the choice points

Read the code and list every decision that is hardcoded or configured and could reasonably differ: model names, prompt variants, skills, retrieval depth, temperature, tool policy, routing rules. Don't stop at the first one: an agent usually has several separate choices. For each, note the file, line and current value.

## 3. Agree on the actions

Show the list and ask which to instrument and which alternatives to try. Suggest two to four points: each one's traffic also carries noise from the others, so more points mean each needs more data. Rules:

- The current value is always the default. Never change it.
- Never invent alternatives. Each must be something the user accepts running on real traffic: about 5% of calls are randomized, and those calls will really use it.
- Skip choices where an alternative could cause harm (for example skipping a safety check or an irreversible action) and say why.
- Each point is its own decision. Options that only work together, such as a prompt written for one model, are one point whose actions are the valid pairs.
- A point is made once per run. If a choice repeats inside a run, such as a tool choice at every step, say that its results will be less reliable, and prefer a choice made once per run. `eval` warns if a point repeats.

## 4. Find the outcome

Identify where the agent learns how a run went, and what can be measured: success, cost, latency, user feedback, judge score. Each is logged as its own named number, so the objective can be picked at evaluation time. If there is no outcome signal, ask; without one nothing can be evaluated.

## 5. Add the dependency

Add `whittle @ git+https://github.com/maxpagels/whittle` to the project in whatever way it manages dependencies (`uv add`, `pyproject.toml`, `requirements.txt`, `poetry add`).

## 6. Edit the code

```python
import whittle

with whittle.run(request_id):             # your own id, if outcomes arrive later or elsewhere
    d = whittle.decide("router", ["model-a", "model-b"], context={"task": kind, "long": n > 2000})
    answer = call_model(d.action)         # the first action is the default
    whittle.learn(success=1, cost_eur=0.004, latency_s=2.1)
```

With several points, give each its own `decide()` in the same run. One `learn` resolves them all:

```python
with whittle.run(request_id):
    model = whittle.decide("model", ["small-v1", "large-v1"], context=ctx)
    skills = whittle.decide("skills", ["base", "base+search"], context=ctx)
    result = run_agent(model.action, skills.action)
    whittle.learn(success=int(result.ok), cost_eur=result.cost)
```

- Call `decide()` as close to the choice as possible, and always use `d.action`.
- Give each point a short, stable name that describes the choice. Renaming a point starts it with no history.
- Don't change one point's action list based on another point's result. Pass the same `context` to related points.
- Use `whittle.run()` around one agent run, and `whittle.learn(...)` for run-level outcomes, or `whittle.learn(run_id=request_id, ...)` from another process or days later. With several points, always use these. `d.learn(...)` resolves only that one decision, so use it only when a run has a single decision.
- A point that only applies to some runs is fine: call `decide()` only when the choice is actually made.
- A decision resolves exactly once, so report all outcomes in one call. If some arrive later (such as user feedback), call `learn` when the last one is in. The first name given is the `gittins` reward, so put the main outcome first.
- Run ids must be unique per run.
- `actions` can be any values, including numbers. Pass `default=` if the current value isn't first.
- `context` holds short facts a future policy might branch on: task type, tenant tier, length bucket. Never put prompts, user text or personal data in it. Long values are hashed, which also makes them useless to a policy.
- Don't catch Whittle errors or add feature flags. `decide()` and `learn()` don't raise on logging failures: the agent gets the default action. `WHITTLE_EPSILON=0` turns randomization off.

## 7. Deployment

Without configuration, data goes to the user's local data directory, which is right for development and for a single host. Check for a Dockerfile, Kubernetes manifests or serverless config. If any exist, tell the user to set one variable in the deploy environment and put nothing in code:

```
WHITTLE_STORE=/path/on/a/persistent/volume
```

All processes must share that path, and the filesystem must support file locks. Whittle doesn't support object stores or serverless platforms without a shared volume, and several hosts need a shared volume. If the deployment can't provide one, say so and stop rather than instrumenting.

## 8. Verify

Run the project's tests, then exercise each instrumented path and run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/whittle" status
```

Confirm every point appears, with both decisions and outcomes. If tests mock the model call, make sure randomization doesn't make them flaky: set `WHITTLE_EPSILON=0` in the test environment.

Finish by telling the user which points were instrumented, with which actions, which rewards are logged, and the estimated traffic using `/whittle:plan`. Each point is evaluated separately with `--point`, and combined changes across points can't be evaluated yet.
