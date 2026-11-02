---
description: Evaluate a proposed agent change against logged traffic and give a ship, wait or block verdict. Use when the user asks whether a model, prompt or routing change will help.
argument-hint: "<the change, in plain language>"
---

# Evaluate a change with Whittle

Change to evaluate: $ARGUMENTS

Rule: you never compute estimates. Every number comes from the CLI. You write the policy, run commands and explain the output.

1. Run `"${CLAUDE_PLUGIN_ROOT}/bin/whittle" status` to see the choice points, the actions offered and the reward names.

2. Write the change as a policy file in `whittle_policies/<short-name>.py`, kept in the repo so the evaluation can be re-run. Standard library only.

   ```python
   def policy(d):
       """d.context, d.actions, d.default, d.point, d.run_id. Return an action, or {action: probability}."""
       return "model-b" if d.context.get("long") else d.default
   ```

   To enable the doubly robust estimator, also define `q(d)` returning `{action: predicted objective}` for every action in `d.actions`, for example from the team's eval suite or judge scores.

3. If the change uses an action that was never offered, it can't be evaluated from logs. Say so and suggest adding it to the `decide()` call and collecting data with `/whittle:plan`. Don't approximate it with a similar action.

4. Agree the objective with the user if it isn't obvious, and run:

   ```
   "${CLAUDE_PLUGIN_ROOT}/bin/whittle" eval --policy whittle_policies/<name>.py --objective "success - 50*cost_eur" [--point router]
   ```

5. Hand the output to the `whittle:reviewer` agent, then explain the result:
   - `base` is the current default, `lift` the candidate minus the default, with a 95% interval.
   - `verdict` is `ship` (lift is confidently positive), `block` (confidently negative) or `wait`. Report it as given, never upgrade it. A `holding verdict` line means the data can't support a decision yet. If it says the log is unverified, tell the user to install the `gittins` CLI with `curl -fsSL https://getgittins.dev/install.sh | sh`.
   - The verdict concerns only the logged objective. If the objective leaves something out, say so.
