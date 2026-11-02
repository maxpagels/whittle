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

Read the code and list decisions that are hardcoded or configured and could reasonably differ: model names, prompt variants, retrieval depth, temperature, tool policy, routing rules. For each, note the file, line and current value.

## 3. Agree on the actions

Show the list and ask which to instrument and which alternatives to try. Rules:

- The current value is always the default. Never change it.
- Never invent alternatives. Each must be something the user accepts running on real traffic: about 2% of calls are randomized, and those calls will really use it.
- Skip choices where an alternative could cause harm (for example skipping a safety check or an irreversible action) and say why.

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

- Call `decide()` as close to the choice as possible, and always use `d.action`.
- Use `whittle.run()` around one agent run, and `whittle.learn(...)` for run-level outcomes, or `whittle.learn(run_id=request_id, ...)` from another process or days later. With a single decision, `d.learn(...)` also works.
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

Run the project's tests, then exercise one instrumented path and run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/whittle" status
```

Confirm decisions and outcomes both appear. If tests mock the model call, make sure randomization doesn't make them flaky: set `WHITTLE_EPSILON=0` in the test environment.

Finish by telling the user which points were instrumented, with which actions, which rewards are logged, and the estimated traffic using `/whittle:plan`.
