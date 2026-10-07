import multiprocessing as mp
from pathlib import Path

import pytest

from gitswarm.errors import Conflict
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore


@pytest.fixture
def store(remote_url: str, home: Path) -> MetaStore:
    return MetaStore(Hive.init(remote_url, home).git)


def test_empty_store(store: MetaStore):
    assert store.tip() is None
    assert store.read("ws/x.json") is None
    assert store.list("ws/") == []
    assert store.log(since=None) == []


def test_apply_creates_orphan_then_appends(store: MetaStore):
    c1 = store.apply(Change("ws/a.json", b"{}", "ws.created a"))
    c2 = store.apply(Change("ws/b.json", b"{}", "ws.created b"))
    assert store.tip() == c2
    assert store.list("ws/") == ["ws/a.json", "ws/b.json"]
    assert [e.subject for e in store.log(since=None)] == ["ws.created b", "ws.created a"]
    assert [e.oid for e in store.log(since=c1)] == [c2]
    c3 = store.apply(Change("ws/a.json", b'{"state":"dropped"}', "ws.dropped a"))
    assert store.read("ws/a.json") == b'{"state":"dropped"}'
    assert store.read_at(c1, "ws/a.json") == b"{}"
    assert store.read_at(c3, "ws/b.json") == b"{}"


def _writer(remote_url: str, home: str, name: str) -> None:
    hive = Hive.open(remote_url, Path(home))
    MetaStore(hive.git).apply(Change(f"ws/{name}.json", name.encode(), f"ws.created {name}"))


def test_concurrent_writers_all_land(remote_url: str, home: Path):
    Hive.init(remote_url, home)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(remote_url, str(home), n)) for n in ("p", "q", "r")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    store = MetaStore(Hive.open(remote_url, home).git)
    assert store.list("ws/") == ["ws/p.json", "ws/q.json", "ws/r.json"]


def test_apply_retries_on_concurrent_change(remote_url: str, home: Path):
    """CAS retry proves that apply retries when concurrent change moves remote."""
    hive = Hive.init(remote_url, home)

    # Wrapper to track push calls and inject interference
    class GitWithInterference:
        def __init__(self, delegate):
            self._git = delegate
            self.push_count = 0

        def __getattr__(self, name):
            return getattr(self._git, name)

        def push(self, oid: str, ref: str, expected: str | None) -> bool:
            self.push_count += 1
            if self.push_count == 1:
                # Apply competing change to move the remote
                store2 = MetaStore(self._git)
                store2.apply(Change("ws/competing.json", b"x", "ws.competing"))
            return self._git.push(oid, ref, expected)

    git_wrapper = GitWithInterference(hive.git)
    store = MetaStore(git_wrapper)

    # Apply should retry: first push fails (lease stale due to competing),
    # retry succeeds
    result = store.apply(Change("ws/own.json", b"y", "ws.own"))
    assert result is not None
    assert store.list("ws/") == ["ws/competing.json", "ws/own.json"]
    assert git_wrapper.push_count >= 2


def test_cas_exhaustion_is_conflict(store: MetaStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(type(store.git), "push", lambda self, oid, ref, expected: False)
    with pytest.raises(Conflict):
        store.apply(Change("ws/z.json", b"{}", "ws.created z"))
