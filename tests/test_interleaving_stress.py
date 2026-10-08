"""두 hive 의 무작위 인터리빙 — 단일 프로세스, 시드 고정, 매 스텝 뒤 불변식.

hive A ──create/publish/drop/gc──┐
hive B ──publish/drop/gc─────────┼──▶ file:// 원격 ◀── foreign clone (push·delete·revive)
                                 │
                    매 스텝 뒤: (i) dropped 뒤 다른 상태 없음
                                (ii) 원격 ws 브랜치 == 살아 있는 레코드 ± 외부 삭제·부활
                                (iii) published_oid ∈ 누군가 push 한 oid
                                (iv) drop 한 hive 에 lease/peek ref 없음
"""

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.constants import LEASE, PEEK, lease_ref, peek_ref, ws_ref
from gitswarm.errors import Conflict, InvalidState, NotFound
from gitswarm.service.workspace import Checkout, WorkspaceService, WsState
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore
from tests.conftest import git, ls_remote_prefix

SEED = 1234
STEPS = 120  # 200 은 이 맥에서 72 s, 150 은 전체 묶음 안에서 53 s — 60 s 상한 여유
TTL_CHOICES = (0, 60)
CLOCK_STEP = timedelta(seconds=61)  # 한 번의 gc 전진이 ttl 60 을 넘긴다
LIVE_BIAS = 0.7
REVIVED_BIAS = 0.15
ALLOWED = (Conflict, InvalidState, NotFound)

# 동작 → 가중치. create 가 없으면 나머지는 대상이 없다.
ACTIONS = {
    "A.create": 4,
    "A.publish": 4,
    "B.publish": 3,
    "A.drop": 1,
    "B.drop": 1,
    "A.gc": 1,
    "B.gc": 1,
    "foreign.push": 3,
    "foreign.delete": 1,
    "foreign.revive": 1,
}


@dataclass
class World:
    a: WorkspaceService
    b: WorkspaceService
    foreign: Path
    clock: list[datetime]
    rng: random.Random
    ids: list[str] = field(default_factory=list)
    pushed: set[str] = field(default_factory=set)  # foreign·worktree 가 push 한 oid
    foreign_deleted: set[str] = field(default_factory=set)
    revived: set[str] = field(default_factory=set)  # dropped 인데 외부가 브랜치를 다시 만든 것
    dropped_by: dict[str, WorkspaceService] = field(default_factory=dict)
    states: dict[str, WsState] = field(default_factory=dict)
    published: dict[str, str | None] = field(default_factory=dict)
    seen_tip: str | None = None
    log: list[str] = field(default_factory=list)
    serial: int = 0

    def svc(self, name: str) -> WorkspaceService:
        return self.a if name == "A" else self.b

    def target(self) -> str:
        """대개 살아 있는 것, 가끔 되살아난 dropped 것·아무것(전이 거절 경로)."""
        alive = [i for i in self.ids if self.states.get(i) is not WsState.DROPPED]
        roll = self.rng.random()
        if alive and roll < LIVE_BIAS:
            return self.rng.choice(alive)
        if self.revived and roll < LIVE_BIAS + REVIVED_BIAS:
            return self.rng.choice(sorted(self.revived))
        return self.rng.choice(self.ids)

    def live_branches(self) -> list[str]:
        return sorted(ls_remote_prefix(self.foreign, ws_ref("")))


def _world(remote_url: str, tmp_path: Path) -> World:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]

    def service(host: str) -> WorkspaceService:
        hive = Hive.init(remote_url, tmp_path / host)
        return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), clock=lambda: clock[0])

    foreign = tmp_path / "foreign"
    git("clone", "-q", remote_url, str(foreign), cwd=tmp_path)
    return World(service("host-a"), service("host-b"), foreign, clock, random.Random(SEED))


# ── 동작 ──────────────────────────────────────────────────────
def _commit_in_worktree(w: World, wt: Path) -> None:
    w.serial += 1
    (wt / f"wt-{w.serial}.txt").write_text(f"{w.serial}\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", f"wt {w.serial}", cwd=wt)


def _publish(w: World, host: str) -> str:
    svc = w.svc(host)
    ws_id = w.target()
    wt = svc.hive.worktree_dir(ws_id)
    if wt.exists():
        if w.rng.random() < 0.7:
            _commit_in_worktree(w, wt)
        # worktree 가 push 하는 것은 그 HEAD 다
        w.pushed.add(git("rev-parse", "HEAD", cwd=wt))
    oid = svc.publish(ws_id)
    return f"{host}.publish {ws_id} -> {oid[:8]}"


def _drop(w: World, host: str) -> str:
    svc = w.svc(host)
    ws_id = w.target()
    before = w.states.get(ws_id)
    svc.drop(ws_id)
    # 다시 drop 해도 되살아난 브랜치를 지운다
    w.revived.discard(ws_id)
    if before is not WsState.DROPPED:
        w.dropped_by[ws_id] = svc
    return f"{host}.drop {ws_id}"


def _gc(w: World, host: str) -> str:
    svc = w.svc(host)
    w.clock[0] += CLOCK_STEP
    out = svc.gc()
    for ws_id in out["expired"]:
        w.dropped_by[ws_id] = svc
    return f"{host}.gc expired={out['expired']} conflicted={out['conflicted']}"


def _create(w: World) -> str:
    checkout = w.rng.choice([Checkout.NONE, Checkout.WORKTREE])
    ttl = w.rng.choice(TTL_CHOICES)
    r = w.a.create("main", {}, ttl, None, checkout, {})
    w.ids.append(r.id)
    return f"A.create {r.id} {checkout.value} ttl={ttl}"


def _foreign_push(w: World) -> str:
    live = w.live_branches()
    if not live:
        return "foreign.push (none live)"
    ref = w.rng.choice(live)
    w.serial += 1
    git("fetch", "-q", "origin", f"+{ref}:refs/foreign/tip", cwd=w.foreign)
    tree = git("rev-parse", "refs/foreign/tip^{tree}", cwd=w.foreign)
    new = git("commit-tree", tree, "-p", "refs/foreign/tip", "-m", f"f {w.serial}", cwd=w.foreign)
    git("push", "-q", "origin", f"{new}:{ref}", cwd=w.foreign)
    w.pushed.add(new)
    return f"foreign.push {ref.rsplit('/', 1)[1]} -> {new[:8]}"


def _foreign_delete(w: World) -> str:
    live = w.live_branches()
    if not live:
        return "foreign.delete (none live)"
    ref = w.rng.choice(live)
    git("push", "-q", "origin", f":{ref}", cwd=w.foreign)
    ws_id = ref.rsplit("/", 1)[1]
    w.foreign_deleted.add(ws_id)
    w.revived.discard(ws_id)
    return f"foreign.delete {ws_id}"


def _foreign_revive(w: World) -> str:
    """dropped workspace 의 브랜치 이름으로 외부가 다시 push — 이후 publish 는 전이에서 막혀야 한다."""
    live = set(w.live_branches())
    gone = [i for i, s in w.states.items() if s is WsState.DROPPED and ws_ref(i) not in live]
    if not gone:
        return "foreign.revive (none dropped)"
    ws_id = w.rng.choice(sorted(gone))
    w.serial += 1
    tree = git("rev-parse", "origin/main^{tree}", cwd=w.foreign)
    new = git("commit-tree", tree, "-p", "origin/main", "-m", f"r {w.serial}", cwd=w.foreign)
    git("push", "-q", "origin", f"{new}:{ws_ref(ws_id)}", cwd=w.foreign)
    w.pushed.add(new)
    w.revived.add(ws_id)
    w.foreign_deleted.discard(ws_id)
    return f"foreign.revive {ws_id} -> {new[:8]}"


def _step(w: World, action: str) -> str:
    if action == "A.create" or not w.ids:
        return _create(w)
    if action.endswith(".publish"):
        return _publish(w, action[0])
    if action.endswith(".drop"):
        return _drop(w, action[0])
    if action.endswith(".gc"):
        return _gc(w, action[0])
    if action == "foreign.push":
        return _foreign_push(w)
    if action == "foreign.revive":
        return _foreign_revive(w)
    return _foreign_delete(w)


# ── 불변식 ────────────────────────────────────────────────────
def _absorb_events(w: World) -> None:
    """새 meta 커밋만 오래된 것부터 읽어 상태를 갱신한다. (i): dropped 뒤에는 dropped 뿐."""
    for ev in reversed(w.a.events(w.seen_tip)):
        state = WsState(ev.payload["state"])
        prev = w.states.get(ev.id)
        assert not (prev is WsState.DROPPED and state is not WsState.DROPPED), (
            f"(i) {ev.id}: dropped → {state.value} at {ev.oid} ({ev.kind})"
        )
        w.states[ev.id] = state
        w.published[ev.id] = ev.payload.get("published_oid")
        w.seen_tip = ev.oid


def _local_refs(svc: WorkspaceService) -> set[str]:
    out = git("for-each-ref", "--format=%(refname)", LEASE, PEEK, cwd=svc.hive.git.repo)
    return set(filter(None, out.split("\n")))


def _check(w: World) -> None:
    _absorb_events(w)

    # (ii) 원격 ws 브랜치 == dropped 아닌 레코드(외부가 지운 것은 빼고 되살린 것은 더한다)
    alive = {i for i, s in w.states.items() if s is not WsState.DROPPED}
    expected = {ws_ref(i) for i in (alive - w.foreign_deleted) | w.revived}
    assert set(w.live_branches()) == expected, "(ii) remote branches != live records"

    # (iii) 발행 기록은 누군가 실제로 push 한 oid 다
    for ws_id, oid in w.published.items():
        assert oid is None or oid in w.pushed, f"(iii) {ws_id}: published_oid {oid} never pushed"

    # (iv) drop 한 hive 에는 그 workspace 의 lease·peek ref 가 남지 않는다
    for svc in (w.a, w.b):
        refs = _local_refs(svc)
        for ws_id, dropper in w.dropped_by.items():
            if dropper is not svc:
                continue
            branch = ws_ref(ws_id)
            leftover = {lease_ref(branch), peek_ref(branch)} & refs
            assert not leftover, f"(iv) {ws_id}: {leftover} left in dropping hive"


def test_two_hive_interleaving_keeps_invariants(remote_url: str, tmp_path: Path):
    w = _world(remote_url, tmp_path)
    names = list(ACTIONS)
    weights = list(ACTIONS.values())

    for i in range(STEPS):
        action = w.rng.choices(names, weights)[0]
        try:
            w.log.append(_step(w, action))
        except ALLOWED as e:
            w.log.append(f"{action} !{type(e).__name__} {e.detail[:60]}")

        try:
            _check(w)
        except AssertionError as e:
            trace = "\n".join(f"{n:3} {s}" for n, s in enumerate(w.log))
            raise AssertionError(f"seed={SEED} step={i}: {e}\n{trace}") from None

    # 끝에서 한 번, 사건 재구성이 아니라 list 로도 같은지
    records = {ws.id: ws.state for ws in w.b.list(None)}
    assert records == w.states
    assert {s for s in w.states.values()} >= {WsState.DROPPED}
