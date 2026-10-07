"""실프로세스 두 호스트 — 2 home × 2 spawn 프로세스 × ROUNDS 라운드, 한 file:// 원격.

    home-0 ──proc 0, proc 1──┐
                             ├──▶ file:// 원격 (meta CAS · ws 브랜치)
    home-1 ──proc 0, proc 1──┘

라운드 = create → publish | drop | gc(시계 +1 h, ttl 1 s). 형제의 workspace 도 gc 가 거둔다.
끝에서: 전원 exit 0, 원격 ws 브랜치 == dropped 아닌 레코드.
"""

import json
import multiprocessing as mp
import random
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.constants import META_REF, ws_ref
from gitswarm.driver.git import Git
from gitswarm.errors import Conflict, InvalidState, NotFound
from gitswarm.service.workspace import (
    Checkout,
    WorkspaceService,
    WsState,
    open_service,
    utcnow,
)
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore
from tests.conftest import GIT_ENV, ls_remote_prefix

HOMES = 2
PROCS_PER_HOME = 2
ROUNDS = 16  # 20 은 이 맥에서 45 s — 느린 러너 여유
TTL_S = 1
GC_SKEW = timedelta(hours=1)  # gc 시계: 모든 ttl 1 s 레코드가 만료로 보인다
JOIN_TIMEOUT_S = 120
ALLOWED = (Conflict, InvalidState, NotFound)


def _commit(wt: Path, name: str) -> None:
    (wt / name).write_text(f"{name}\n")
    for args in (["add", "--", name], ["commit", "-q", "-m", name]):
        subprocess.run(["git", *args], cwd=wt, env=GIT_ENV, check=True, capture_output=True)


def _round(svc: WorkspaceService, gc_svc: WorkspaceService, op: str, tag: str) -> list[str]:
    """한 라운드. Conflict 로 끝난 id 를 돌려준다 — 브랜치를 지운 뒤 meta 를 못 쓴 후보다."""
    checkout = Checkout.WORKTREE if op == "publish" else Checkout.NONE
    r = svc.create("main", {"name": tag}, TTL_S, None, checkout, {})
    try:
        if op == "publish":
            _commit(Path(r.path), f"{tag}.txt")
            svc.publish(r.id)
        elif op == "drop":
            svc.drop(r.id)
        else:
            return gc_svc.gc()["conflicted"]
    except Conflict:
        return [r.id]
    except (InvalidState, NotFound):
        pass
    return []


def _worker(remote_url: str, home: str, seed: int, out: str) -> None:
    """예외 3종만 삼킨다 — 다른 예외는 프로세스를 exit 1 로 끝낸다."""
    rng = random.Random(seed)
    svc = open_service(remote_url, Path(home))
    gc_svc = WorkspaceService(svc.hive, svc.store, svc.adapter, clock=lambda: utcnow() + GC_SKEW)
    created = 0
    suspects: list[str] = []
    for n in range(ROUNDS):
        op = rng.choice(["publish", "drop", "gc"])
        try:
            suspects += _round(svc, gc_svc, op, f"s{seed}r{n}")
        except ALLOWED:  # create 자체가 진 경우 — 보상되어 기록도 브랜치도 없다
            continue
        created += 1
    Path(out).write_text(json.dumps({"created": created, "suspects": suspects}))


def test_two_hosts_concurrent_rounds_keep_branches_matched(remote_url: str, tmp_path: Path):
    homes = [tmp_path / f"home-{h}" for h in range(HOMES)]
    for h in homes:
        Hive.init(remote_url, h)

    ctx = mp.get_context("spawn")
    outs = [tmp_path / f"out-{i}-{p}.json" for i in range(HOMES) for p in range(PROCS_PER_HOME)]
    procs = [
        ctx.Process(target=_worker, args=(remote_url, str(homes[n // PROCS_PER_HOME]), n, str(o)))
        for n, o in enumerate(outs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(JOIN_TIMEOUT_S)
    assert [p.exitcode for p in procs] == [0] * len(procs)

    reports = [json.loads(o.read_text()) for o in outs]
    suspects = {i for r in reports for i in r["suspects"]}
    svc = open_service(remote_url, homes[0])
    records = svc.list(None)
    assert len(records) == sum(r["created"] for r in reports)

    # (ii) 고아 브랜치 0. 브랜치 잃은 살아 있는 레코드는 meta Conflict 로 끝난 drop 뿐이다
    # (그 결함은 아래 xfail 이 결정적으로 고정한다)
    alive = {ws.branch for ws in records if ws.state is not WsState.DROPPED}
    remote = set(ls_remote_prefix(svc.hive.git.repo, ws_ref("")))
    assert remote <= alive
    assert {b.rsplit("/", 1)[1] for b in alive - remote} <= suspects


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="drop deletes the remote branch before the meta CAS; when the CAS "
    "exhausts (Conflict) the record stays open with no branch and gc "
    "(STRICT, lease ref already cleared) reports it conflicted forever",
)
def test_drop_meta_exhaustion_leaves_branchless_open_record(
    remote_url: str, home: Path, monkeypatch: pytest.MonkeyPatch
):
    hive = Hive.init(remote_url, home)
    svc = WorkspaceService(hive, MetaStore(hive.git, sleep=lambda s: None), PlainAdapter())
    r = svc.create("main", {}, TTL_S, None, Checkout.NONE, {})

    # meta push 만 매번 진다 — 4 프로세스 경합에서 실측된 8 회 소진의 결정적 재현
    real_push = Git.push

    def push(self: Git, oid: str, ref: str, expected: str | None) -> bool:
        return False if ref == META_REF else real_push(self, oid, ref, expected)

    monkeypatch.setattr(Git, "push", push)
    with pytest.raises(Conflict):
        svc.drop(r.id)
    monkeypatch.undo()

    state = svc.get(r.id).state
    branch = hive.git.ls_remote(r.branch)
    assert (state is WsState.DROPPED) == (branch is None), f"{state.value} with branch {branch}"
