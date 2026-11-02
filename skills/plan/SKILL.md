---
description: Estimate how much traffic and exploration is needed to evaluate an agent change to a given precision. Use when the user asks how long data collection will take or what epsilon to use.
argument-hint: "<the change or target precision>"
---

# Plan data collection with Whittle

Request: $ARGUMENTS

Numbers come from the CLI only.

With logged data and a policy file (see `/whittle:eval`):

```
"${CLAUDE_PLUGIN_ROOT}/bin/whittle" plan --policy whittle_policies/<name>.py --objective "<expr>" --target-ci 0.01 [--point router]
```

Before any data exists, for a 0-1 reward, where `--actions` is the number of actions at the choice point:

```
"${CLAUDE_PLUGIN_ROOT}/bin/whittle" plan --epsilon 0.05 --actions 3 --target-ci 0.01
```

Explain the result in traffic terms. The number needed scales with 1/epsilon and with 1/target-ci², so doubling `WHITTLE_EPSILON` halves the time and doubles how many calls take a non-default path. Choosing epsilon is the user's call, because it is a budget of real traffic.
