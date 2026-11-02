# Whittle

Find out whether an agent change will help before you ship it, using your own production traffic.

Agent stacks make choices without randomizing, so logs can't answer "what would have happened with the other model?". Whittle randomizes a small share of those choices, logs them, and scores any candidate policy against the logs, with a ship, wait or block verdict. It's built on [Gittins](https://docs.getgittins.dev).

## Claude Code plugin

```
/plugin marketplace add maxpagels/whittle
/plugin install whittle@whittle
```

Requires [uv](https://docs.astral.sh/uv/). Then:

| Command | What it does |
| --- | --- |
| `/whittle:instrument` | Finds choice points in your agent, agrees alternatives with you, adds the calls |
| `/whittle:plan` | Estimates the traffic needed to evaluate a change |
| `/whittle:eval` | Turns a plain-language change into a policy, evaluates it, explains the verdict |

The plugin evaluates; it doesn't collect. Data comes from your running system.

## Library

Python 3.10+. `pip install "git+https://github.com/maxpagels/whittle"`, then:

```python
import whittle

with whittle.run(request_id):
    d = whittle.decide("router", ["model-a", "model-b"], context={"task": "code"})
    answer = call_model(d.action)
    whittle.learn(success=1, cost_eur=0.004, latency_s=2.1)
```

The first action is the current default; about 5% of calls explore. Outcomes can arrive from any process, within the horizon: `whittle.learn(run_id=request_id, success=1, feedback=1)`. The first name is the Gittins reward; all of them are kept for evaluation.

## CLI

```
whittle status
whittle eval --policy whittle_policies/long_code.py --objective "success - 50*cost_eur"
whittle plan --policy whittle_policies/long_code.py --target-ci 0.01
```

A policy is a Python file:

```python
def policy(d):
    return "model-b" if d.context.get("task") == "code" else d.default
```

Optionally define `q(d)`, returning `{action: predicted objective}`, to use a doubly robust estimate. `whittle eval` exits 1 on a block verdict, so it can gate CI. It only evaluates actions that were offered in `decide()`.

## Storage

There's nothing to set up. Every operation takes a file lock, loads the [Gittins](https://docs.getgittins.dev) state, runs, appends to the log and saves the state, so the ledger is on disk and any process can report an outcome. Log lines and state commit together, so a crash can't leave them disagreeing.

Without configuration, data goes in `~/.local/share/whittle/`. `whittle status` shows where. On disposable disks, point it at a persistent path once, in the deploy environment; Whittle warns if it detects you need to:

```
WHITTLE_STORE=/path/on/a/persistent/volume
```

The log is a standard Gittins experience log, so `gittins verify --log <path>` works on it directly. `whittle eval` runs that check too, and won't give a ship verdict without it, so install the [CLI](https://docs.getgittins.dev).

If logging fails, or the lock isn't free within a second, the agent gets the default action and a warning; the error doesn't reach your code. Context is for short features: strings over 256 characters and non-scalar values are stored as hashes.

| Variable | Default | Meaning |
| --- | --- | --- |
| `WHITTLE_STORE` | local | A path |
| `WHITTLE_EPSILON` | `0.05` | Share of calls that explore; `0` turns exploration off |
| `WHITTLE_HORIZON_DAYS` | `7` | How long a decision waits for its outcome; fixed when the state is first created |

## Limits

- Python only, on macOS or Linux.
- One host, or several sharing a volume with working file locks. No object stores or serverless.
- No online learning: nothing moves traffic off your default, even though outcomes are logged.
- Each decision resolves once, so report all its outcomes together.
- One choice per run is the sound case. Several points in a run are scored independently, and a warning fires if a run repeats a point.
- `gittins replay` (CLI 2.1.0 or later) can rebuild a state that continues this log, but it drops open decisions, so their outcomes are lost.
- The log only grows, and nothing expires it. Local stores are per project path, so a different checkout has separate data.

## Development

```
uv run pytest
```
