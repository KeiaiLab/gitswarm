"""원격 왕복 예산 — 서비스 동작 하나가 타는 fetch·push·ls-remote 수.

SSH 원격에서 왕복 하나는 RTT 이상이다. 예산을 넘는 변경은 지연으로 바로 드러난다.
"""

from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.service.workspace import Checkout, WorkspaceService
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore
from tests.conftest import count_network, git

# 동작별 상한: create = base peek·브랜치 push·meta fetch·meta push (+1 여유)
BUDGET_CREATE = 5
BUDGET_PUBLISH = 4
BUDGET_DROP = 4
BUDGET_READ = 2
BUDGET_QUERY = 1


@pytest.fixture
def svc(remote_url: str, home: Path) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())


def test_create_budget(svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch):
    calls = count_network(monkeypatch)
    svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    assert len(calls) <= BUDGET_CREATE, calls


def test_publish_and_drop_budget(svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    wt = Path(r.path)
    (wt / "n.txt").write_text("n\n")
    git("add", "n.txt", cwd=wt)
    git("commit", "-q", "-m", "n", cwd=wt)
    calls = count_network(monkeypatch)

    svc.publish(r.id)
    assert len(calls) <= BUDGET_PUBLISH, calls

    calls.clear()
    svc.drop(r.id)
    assert len(calls) <= BUDGET_DROP, calls


@pytest.mark.parametrize(
    "op,budget",
    [
        (lambda s, i: s.read(i, "README.md"), BUDGET_READ),
        (lambda s, i: s.tree(i), BUDGET_READ),
        (lambda s, i: s.get(i), BUDGET_QUERY),
        (lambda s, i: s.list(None), BUDGET_QUERY),
        (lambda s, i: s.events(None), BUDGET_QUERY),
        (lambda s, i: s.gc(), BUDGET_QUERY),
    ],
    ids=["read", "tree", "get", "list", "events", "gc"],
)
def test_query_budget(svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch, op, budget: int):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    calls = count_network(monkeypatch)
    op(svc, r.id)
    assert len(calls) <= budget, calls
