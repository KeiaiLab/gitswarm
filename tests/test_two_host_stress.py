"""실프로세스 두 호스트 — 2 home × 2 spawn 프로세스 × ROUNDS 라운드, 한 file:// 원격.

    home-0 ──proc 0, proc 1──┐
                             ├──▶ file:// 원격 (meta CAS · ws 브랜치)
    home-1 ──proc 0, proc 1──┘

라운드 = create → publish | drop | gc(시계 +1 h, ttl 1 s). 형제의 workspace 도 gc 가 거둔다.
끝에서: 전원 exit 0, 원격 ws 브랜치 == dropped 아닌 레코드.

    proc 0..7 ──create → publish → drop──▶ file:// 원격 (meta CAS 작성자 8)

8 작성자 동시 라운드: meta CAS 가 소진되지 않는다(Conflict 0).
"""

import json
import multiprocessing as mp
import random
import subprocess
import sys
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
WRITERS = 8
WRITER_ROUNDS = 2
EXIT_CONFLICT = 3


def _commit(wt: Path, name: str) -> None:
    (wt / name).write_text(f"{name}\n")
    for args in (["add", "--", name], ["commit", "-q", "-m", name]):
        subprocess.run(["git", *args], cwd=wt, env=GIT_ENV, check=True, capture_output=True)


def _round(svc: WorkspaceService, gc_svc: WorkspaceService, op: str, tag: str) -> None:
    """한 라운드. 형제가 먼저 거둔 workspace 의 publish·drop 은 허용 예외로 끝난다."""
    checkout = Checkout.WORKTREE if op == "publish" else Checkout.NONE
    r = svc.create("main", {"name": tag}, TTL_S, None, checkout, {})
    try:
        if op == "publish":
            _commit(Path(r.path), f"{tag}.txt")
            svc.publish(r.id)
        elif op == "drop":
            svc.drop(r.id)
        else:
            gc_svc.gc()
    except ALLOWED:
        pass


def _worker(remote_url: str, home: str, seed: int, out: str) -> None:
    """예외 3종만 삼킨다 — 다른 예외는 프로세스를 exit 1 로 끝낸다."""
    rng = random.Random(seed)
    svc = open_service(remote_url, Path(home))
    gc_svc = WorkspaceService(svc.hive, svc.store, svc.adapter, clock=lambda: utcnow() + GC_SKEW)
    created = 0
    for n in range(ROUNDS):
        op = rng.choice(["publish", "drop", "gc"])
        try:
            _round(svc, gc_svc, op, f"s{seed}r{n}")
        except ALLOWED:  # create 자체가 진 경우 — 보상되어 기록도 브랜치도 없다
            continue
        created += 1
    Path(out).write_text(json.dumps({"created": created}))


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
    svc = open_service(remote_url, homes[0])
    records = svc.list(None)
    assert len(records) == sum(r["created"] for r in reports)

    # (ii) 원격 ws 브랜치 == 살아 있는 레코드 — 고아 브랜치도, 브랜치 잃은 레코드도 0
    alive = {ws.branch for ws in records if ws.state is not WsState.DROPPED}
    assert set(ls_remote_prefix(svc.hive.git.repo, ws_ref(""))) == alive


def _writer(remote_url: str, home: str, tag: str) -> None:
    """create → publish → drop × WRITER_ROUNDS. Conflict 는 EXIT_CONFLICT 로 끝난다."""
    svc = open_service(remote_url, Path(home))
    try:
        for n in range(WRITER_ROUNDS):
            r = svc.create("main", {"name": tag}, 0, None, Checkout.WORKTREE, {})
            _commit(Path(r.path), f"{tag}r{n}.txt")
            svc.publish(r.id)
            svc.drop(r.id)
    except Conflict:
        sys.exit(EXIT_CONFLICT)


def test_eight_writers_never_exhaust_meta_cas(remote_url: str, tmp_path: Path):
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_writer, args=(remote_url, str(tmp_path / f"w-{n}"), f"w{n}"))
        for n in range(WRITERS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(JOIN_TIMEOUT_S)
    assert [p.exitcode for p in procs] == [0] * WRITERS

    svc = open_service(remote_url, tmp_path / "w-0")
    states = [ws.state for ws in svc.list(None)]
    assert states == [WsState.DROPPED] * WRITERS * WRITER_ROUNDS
    assert ls_remote_prefix(svc.hive.git.repo, ws_ref("")) == {}


def test_drop_meta_exhaustion_leaves_branchless_open_record(
    remote_url: str, home: Path, monkeypatch: pytest.MonkeyPatch
):
    """drop 의 meta CAS 가 소진되면 브랜치 없는 open 레코드가 남는다 — 다음 gc 가 거둔다."""
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

    assert svc.get(r.id).state is WsState.OPEN
    assert hive.git.ls_remote(r.branch) is None

    # gc(STRICT)는 본 oid 가 없어도 원격에 브랜치가 없으면 지울 것이 없다 — 전이만 쓴다
    svc.clock = lambda: utcnow() + GC_SKEW
    assert svc.gc() == {"expired": [r.id], "invalid": [], "conflicted": []}
    assert svc.get(r.id).state is WsState.DROPPED
