"""Randomize agent choices a little and log them, with storage handled for you.

    with whittle.run(request_id):
        d = whittle.decide("router", ["model-a", "model-b"], context={"task": "code"})
        ...run the agent with d.action...
        whittle.learn(success=1, cost_eur=0.004)

Gittins does the work: its ledger resolves each decision exactly once, and its log lines are what
`gittins verify` checks. Whittle pins the policy to your default, so nothing shifts traffic on its
own, and keeps the state on disk behind a lock, so any process can report an outcome.
A logging failure never reaches the agent: it gets the default action and a warning.
"""

import contextlib, contextvars, hashlib, json, math, os, uuid
from dataclasses import dataclass
from typing import Any, Optional

import gittins

from . import store
from .store import warn_once

__all__ = ["Decision", "decide", "learn", "run"]

FORMAT_VERSION = 2
DEFAULT_EPSILON = 0.02
MAX_STRING = 256  # longer values are hashed: context is for features, not prompts or documents

_run = contextvars.ContextVar("whittle_run", default=None)


@dataclass(frozen=True)
class Decision:
    id: Optional[str]  # None when logging failed and the default was used
    action: Any
    point: str
    run_id: Optional[str]

    def learn(self, **reward):
        """Report how this decision went, as named numbers. Only the first report counts."""
        if self.id is None:
            return 0
        return _resolve(lambda tx: [self.id], reward)


def _feature(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, str) and len(value) <= MAX_STRING:
        return value
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def decide(point, actions, context=None, default=None, epsilon=None):
    """Pick an action: `default` (first action if unset) except for a small random share of calls.

    Returns a Decision; use d.action. WHITTLE_EPSILON sets the share, and 0 turns randomization off.
    """
    by_id = {str(a): a for a in actions}
    if not by_id:
        raise ValueError("decide() needs at least one action")
    default_id = str(next(iter(by_id)) if default is None else default)
    if default_id not in by_id:
        raise ValueError(f"default {default_id!r} is not one of the actions {list(by_id)}")
    ids = list(by_id)
    run_id = _run.get()
    try:
        eps = float(os.environ.get("WHITTLE_EPSILON", DEFAULT_EPSILON)) if epsilon is None else float(epsilon)
        if not 0 <= eps <= 1:
            raise ValueError(f"epsilon must be in [0, 1], got {eps}")
        probs = [(1 - eps if i == default_id else 0.0) + eps / len(ids) for i in ids]
        features = {str(k): _feature(v) for k, v in (context or {}).items()}
        with store.transaction() as tx:
            record = gittins.decide(tx.bandit, features, [(i, {}) for i in ids], tx.t, tx.salt,
                                    explore=lambda *_: probs)
            tx.add(record, whittle={"format_version": FORMAT_VERSION, "point": point, "run_id": run_id,
                                    "default": default_id, "epsilon": eps})
            if run_id is not None:
                tx.add_to_run(run_id, record.decision_id)
    except Exception as e:
        # an unlogged random choice would be invisible to evaluation, so fall back to the default
        warn_once(f"decide({point!r}) failed, using the default action: {e}")
        return Decision(None, by_id[default_id], point, run_id)
    return Decision(record.decision_id, by_id[ids[record.chosen]], point, run_id)


def learn(run_id=None, **reward):
    """Report how a whole run went, as named numbers, to every decision still open in it.

    Works inside `with whittle.run():`, or from any process given the run id. Returns how many
    decisions were resolved. Each resolves once, so report everything together.
    """
    run_id = run_id or _run.get()
    if run_id is None:
        warn_once("learn() outside a run: pass run_id=, or use d.learn()")
        return 0
    return _resolve(lambda tx: tx.take_run(str(run_id)), reward)


def _resolve(find, reward):
    try:
        clean = {k: float(v) for k, v in reward.items()}
        if not clean or not all(map(math.isfinite, clean.values())):
            raise ValueError(f"reward must be finite numbers, got {reward}")
        resolved = 0
        with store.transaction() as tx:
            for decision_id in find(tx):
                # gittins' scalar reward is the first name given; all of them stay in `rewards`
                res = gittins.learn(tx.bandit, decision_id, next(iter(clean.values())), tx.t, train=store._ignore)
                if res:  # None: already resolved or unknown
                    tx.add(res, **({"rewards": clean} if res.kind == "rewarded" else {}))
                    resolved += 1
        return resolved
    except Exception as e:
        warn_once(f"could not record an outcome: {e}")
        return 0


@contextlib.contextmanager
def run(run_id=None):
    """Group the decisions of one agent run. Pass your own id if outcomes arrive elsewhere or later."""
    run_id = str(run_id) if run_id else uuid.uuid4().hex
    token = _run.set(run_id)
    try:
        yield run_id
    finally:
        _run.reset(token)
