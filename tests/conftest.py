import pytest

from whittle import evaluate as ev, store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WHITTLE_STORE", str(tmp_path / "store"))
    monkeypatch.delenv("WHITTLE_EPSILON", raising=False)
    monkeypatch.delenv("WHITTLE_HORIZON_DAYS", raising=False)
    monkeypatch.chdir(tmp_path)
    store._warned.clear()


@pytest.fixture
def data():
    return lambda: store.read_committed(store.location())


@pytest.fixture
def unverified(monkeypatch):
    """For estimator tests that shouldn't depend on the gittins CLI being installed."""
    monkeypatch.setattr(ev, "verify", lambda data: None)
