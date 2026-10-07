from datetime import UTC, datetime, timedelta
from pathlib import Path

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.service.stats import summarize
from gitswarm.service.workspace import Checkout, WorkspaceService
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore
from tests.conftest import git

T0 = datetime(2026, 10, 7, tzinfo=UTC)


def test_stats_after_create_publish_drop(remote_url: str, home: Path):
    now = [T0]
    hive = Hive.init(remote_url, home)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), clock=lambda: now[0])
    assert summarize(svc) == {
        "by_kind": {},
        "by_state": {},
        "open_oldest_age_s": None,
        "total_events": 0,
        "invalid": 0,
    }

    a = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    b = svc.create("main", {}, 0, None, Checkout.NONE, {})
    git("commit", "-q", "--allow-empty", "-m", "a", cwd=Path(a.path))
    svc.publish(a.id)
    svc.drop(b.id)
    now[0] = T0 + timedelta(seconds=30)
    svc.create("main", {}, 0, None, Checkout.NONE, {})
    now[0] = T0 + timedelta(seconds=130)

    assert summarize(svc) == {
        "by_kind": {"ws.created": 3, "ws.published": 1, "ws.dropped": 1},
        "by_state": {"open": 1, "published": 1, "dropped": 1},
        "open_oldest_age_s": 100,
        "total_events": 5,
        "invalid": 0,
    }
