"""Where the bandit lives, so callers never have to think about it.

Each operation takes a file lock, loads the gittins state, runs, appends its log lines and saves the
state: the "load state, decide, learn, save state" pattern from the gittins docs. So the ledger is
on disk, any process can resolve any decision, and the log is in order, as `gittins verify` requires.
The default location is a per-project user data directory; WHITTLE_STORE (a path) overrides it.
"""

import contextlib, fcntl, hashlib, json, logging, os, re, time, uuid
from pathlib import Path

import gittins

log = logging.getLogger("whittle")
BITS = 8  # the model is never trained, so this only sizes the hashed features in the log
HORIZON_DAYS = 7
LOCK_TIMEOUT = 1.0  # past this the agent moves on without logging, rather than waiting on a stuck process
SWEEP_SECONDS = 3600
_warned = set()


def warn_once(message):
    if message not in _warned:
        _warned.add(message)
        log.warning("whittle: %s", message)


def project_root():
    cwd = Path.cwd()
    for path in (cwd, *cwd.parents):
        if (path / ".git").exists() or (path / "pyproject.toml").exists():
            return path
    return cwd


def default_location():
    root = project_root()
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    name = re.sub(r"[^\w.-]", "-", root.name) or "project"
    return data / "whittle" / f"{name}-{hashlib.sha256(str(root).encode()).hexdigest()[:8]}"


def ephemeral_platform():
    """The platform name when local disk won't outlive this process, else None."""
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return "AWS Lambda"
    if os.environ.get("K_SERVICE"):
        return "Cloud Run"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "Kubernetes"
    if os.path.exists("/.dockerenv"):
        return "Docker"
    return None


def location():
    configured = os.environ.get("WHITTLE_STORE")
    if configured:
        if "://" in configured:
            raise ValueError(f"WHITTLE_STORE must be a path, got {configured}")
        return Path(configured).expanduser()
    platform = ephemeral_platform()
    if platform:
        warn_once(f"running on {platform} with no WHITTLE_STORE: data goes to local disk and will be lost")
    return default_location()


def log_path(root):
    return root / "log.jsonl"


def _new_bandit(root):
    # a new salt keeps ids unique when a state is rebuilt next to an existing log
    days = float(os.environ.get("WHITTLE_HORIZON_DAYS", HORIZON_DAYS))
    size = log_path(root).stat().st_size if log_path(root).exists() else 0
    return gittins.create(bits=BITS, horizon=days * 86400), {"salt": uuid.uuid4().hex[:12], "log_bytes": size, "t": 0.0}


def _load(root):
    """The bandit and its commit record, undoing any operation that died before committing."""
    try:
        saved = json.loads((root / "state.json").read_text())
    except FileNotFoundError:
        return _new_bandit(root)
    size = log_path(root).stat().st_size if log_path(root).exists() else 0
    if size < saved["log_bytes"]:
        warn_once("the log is shorter than the state expects: starting a new bandit, open decisions are lost")
        return _new_bandit(root)
    if size > saved["log_bytes"]:
        os.truncate(log_path(root), saved["log_bytes"])
    return gittins.deserialize(saved["state"]), {k: saved[k] for k in ("salt", "log_bytes", "t")}


def _save(root, bandit, meta):
    tmp = root / f"state.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"state": gittins.serialize(bandit), **meta}, f)
    os.replace(tmp, root / "state.json")  # the commit point


def _acquire(lock):
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            return fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise TimeoutError("timed out waiting for the store lock") from None
            time.sleep(0.001)


def _ignore(*_):
    pass  # no online learning: resolutions are logged, the model stays untrained


def _sweep_runs(root):
    """Drop run files too old to have open decisions left."""
    marker = root / "swept"
    now = time.time()
    if marker.exists() and now - marker.stat().st_mtime < SWEEP_SECONDS:
        return
    marker.touch()
    days = float(os.environ.get("WHITTLE_HORIZON_DAYS", HORIZON_DAYS))
    for path in (root / "runs").glob("*"):
        with contextlib.suppress(OSError):
            if now - path.stat().st_mtime > (days + 1) * 86400:
                path.unlink()


class Transaction:
    def __init__(self, root, bandit, meta):
        self.root, self.bandit, self.salt, self.events = root, bandit, meta["salt"], []
        self.t = max(time.time(), meta["t"])  # gittins requires time order, and clocks can step back

    def add(self, item, **extra):
        self.events.append({**json.loads(gittins.log_line(item)), **extra})

    def _run_file(self, run_id):
        return self.root / "runs" / hashlib.sha256(run_id.encode()).hexdigest()

    def add_to_run(self, run_id, decision_id):
        (self.root / "runs").mkdir(exist_ok=True)
        with open(self._run_file(run_id), "a") as f:
            f.write(decision_id + "\n")

    def take_run(self, run_id):
        """The run's decision ids, forgetting the run: a resolve leaves nothing open in it."""
        path = self._run_file(run_id)
        if not path.exists():
            return []
        ids = path.read_text().split()
        path.unlink()
        return ids


@contextlib.contextmanager
def transaction():
    """One locked operation. Its log lines and state commit together, or not at all."""
    root = location()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(root / "lock", "w") as lock:
        _acquire(lock)
        bandit, meta = _load(root)
        tx = Transaction(root, bandit, meta)
        yield tx
        for resolution in gittins.expire(bandit, tx.t, train=_ignore):
            tx.add(resolution)
        data = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in tx.events).encode()
        fd = os.open(log_path(root), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _save(root, bandit, {"salt": tx.salt, "log_bytes": meta["log_bytes"] + len(data), "t": tx.t})
        _sweep_runs(root)


def read_committed(root):
    """The log's committed bytes. Needs no lock, because committed lines never change."""
    try:
        size = json.loads((root / "state.json").read_text())["log_bytes"]
        with open(log_path(root), "rb") as f:
            return f.read(size)
    except FileNotFoundError:
        return b""
