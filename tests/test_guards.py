"""방어 분기마다 그것이 없으면 죽는 시험 하나. 위조 레코드·경합·표면 배선."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.constants import META_REF, lease_ref, meta_path, peek_ref, tracking_ref, ws_ref
from gitswarm.errors import Conflict, InvalidState, NotFound, RemoteError
from gitswarm.service import workspace as ws_mod
from gitswarm.service.doctor import diagnose
from gitswarm.service.stats import summarize
from gitswarm.service.workspace import Checkout, Workspace, WorkspaceService, WsState
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore
from gitswarm.surfaces import mcp as mcp_mod
from gitswarm.surfaces.common import parse_state
from tests.conftest import ls_remote_prefix
from tests.test_workspace_lifecycle import RevokeFailsAdapter, TokenAdapter

FORGED_ID = "01J00000000000000000000001"
OTHER_ID = "01J00000000000000000000002"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def svc(remote_url: str, home: Path) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())


def _create(svc: WorkspaceService, mode: Checkout = Checkout.NONE):
    return svc.create("main", {}, 0, None, mode, {})


def _write(svc: WorkspaceService, ws_id: str, body: bytes) -> None:
    svc.store.apply(Change(meta_path(ws_id), lambda _: body, f"forge {ws_id}"))


def _valid_body(ws_id: str) -> bytes:
    return Workspace(
        id=ws_id,
        state=WsState.OPEN,
        base_ref="refs/heads/main",
        base_oid="0" * 40,
        branch=ws_ref(ws_id),
        created_at="2026-01-01T00:00:00Z",
    ).to_json()


# ── constants ────────────────────────────────────────────────
@pytest.mark.parametrize("fn", [tracking_ref, peek_ref, lease_ref])
def test_ref_helpers_reject_non_branch_refs(fn):
    with pytest.raises(ValueError, match="not a branch ref"):
        fn("refs/tags/v1")


# ── record forgery ───────────────────────────────────────────
def test_get_rejects_record_whose_id_is_not_a_ulid(svc: WorkspaceService):
    body = json.dumps(
        {"id": "nope", "state": "open", "base_ref": "r", "base_oid": "o", "branch": "b"}
    ).encode()
    _write(svc, FORGED_ID, body)
    with pytest.raises(InvalidState, match="id 'nope'"):
        svc.get(FORGED_ID)


def test_get_rejects_record_whose_id_differs_from_its_path(svc: WorkspaceService):
    _write(svc, FORGED_ID, _valid_body(OTHER_ID))
    with pytest.raises(InvalidState, match="record carries id"):
        svc.get(FORGED_ID)


def test_force_drop_of_non_json_record_succeeds(svc: WorkspaceService):
    _write(svc, FORGED_ID, b"not json at all")
    assert svc.drop(FORGED_ID).state is WsState.DROPPED


def test_force_drop_revokes_token_of_malformed_record(remote_url: str, home: Path):
    hive = Hive.init(remote_url, home)
    adapter = TokenAdapter()
    svc = WorkspaceService(hive, MetaStore(hive.git), adapter)
    r = _create(svc)
    d = json.loads(svc.store.read(meta_path(r.id))) | {"ttl_s": -5}
    _write(svc, r.id, json.dumps(d).encode())

    dropped = svc.drop(r.id)
    assert dropped.state is WsState.DROPPED
    assert adapter.revoked == ["42"]
    # 회수됐으면 token_id 를 지운다
    assert dropped.token_id is None and svc.get(r.id).token_id is None


def test_force_drop_keeps_token_when_revoke_fails(remote_url: str, home: Path, capsys):
    hive = Hive.init(remote_url, home)
    svc = WorkspaceService(hive, MetaStore(hive.git), TokenAdapter())
    r = _create(svc)
    d = json.loads(svc.store.read(meta_path(r.id))) | {"ttl_s": -5}
    _write(svc, r.id, json.dumps(d).encode())

    # 회수 실패는 상태에 남는다 — 다음 drop 이 다시 시도한다
    svc.adapter = RevokeFailsAdapter()
    dropped = svc.drop(r.id)
    assert dropped.state is WsState.DROPPED and dropped.token_id == "42"
    assert svc.get(r.id).token_id == "42"
    assert "not revoked" in capsys.readouterr().err


# ── meta paths ───────────────────────────────────────────────
BAD_PATH = "ws/a\nb.json"  # push 권한이 있으면 누구나 만들 수 있는 트리 이름


def _forge_meta_entry(svc: WorkspaceService, name: str, subject: str) -> None:
    """ws/ 아래에 이름이 이상한 블롭을 더한 meta 커밋을 push 한다(mktree -z 는 \\n 을 받는다)."""
    git = svc.hive.git
    tip = svc.store.tip()
    files = git.ls_tree_recursive(tip) if tip else {}
    ws_entries = {p.removeprefix("ws/"): oid for p, oid in files.items()}
    ws_entries[name.removeprefix("ws/")] = git.hash_object(b"{}")
    lines = "".join(f"100644 blob {oid}\t{n}\0" for n, oid in ws_entries.items())
    ws_tree = git._out("mktree", "-z", data=lines.encode())
    root = git._out("mktree", "-z", data=f"040000 tree {ws_tree}\tws\0".encode())
    new = git.commit_tree(root, [tip] if tip else [], subject)
    assert git.push(new, META_REF, expected=tip)


def test_unexpected_meta_path_is_reported_not_read(remote_url: str, home: Path):
    hive = Hive.init(remote_url, home)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())
    good = _create(svc)
    # 제목은 멀쩡한 이벤트로 위장한다 — 이벤트 로그(stats)도 그대로 읽힌다
    _forge_meta_entry(svc, BAD_PATH, f"ws.created {good.id}")
    assert BAD_PATH in svc.store.list("ws/")

    # 읽지 않고 invalid 로 — list·gc·stats·doctor 가 트레이스백 없이 끝난다
    records, invalid = svc.list_report(None)
    assert [w.id for w in records] == [good.id]
    assert invalid == [{"id": BAD_PATH, "detail": "unexpected meta path"}]
    assert svc.gc()["invalid"] == invalid
    assert summarize(svc)["invalid"] == 1
    tokens = next(c for c in diagnose(remote_url, home)["checks"] if c["name"] == "tokens")
    assert tokens["ok"] is True


def test_events_rejects_subject_without_workspace_id(svc: WorkspaceService):
    svc.store.apply(Change("ws/x.json", lambda _: b"{}", "ws.created ../../etc"))
    with pytest.raises(InvalidState, match="unexpected subject"):
        svc.events(None)


# ── text fields ──────────────────────────────────────────────
SURROGATE = "\ud800"  # json.dumps 가 "\\ud800" 으로 내보내고 json.loads 가 그대로 되살린다


def _with(ws_id: str, **fields) -> bytes:
    return json.dumps(json.loads(_valid_body(ws_id)) | fields).encode()


@pytest.mark.parametrize(
    "fields,detail",
    [
        ({"agent": {"name": SURROGATE}}, "non-UTF-8 text"),
        ({"labels": {SURROGATE: "v"}}, "non-UTF-8 text"),
        ({"base_ref": SURROGATE}, "non-UTF-8 text"),
        ({"agent": ["name"]}, "agent is not a string map"),
        ({"labels": "k=v"}, "labels is not a string map"),
        ({"agent": {"pid": 1}}, "agent is not a string map"),
    ],
    ids=["agent-value", "labels-key", "base_ref", "agent-list", "labels-str", "agent-int"],
)
def test_from_json_rejects_bad_text(fields: dict, detail: str):
    with pytest.raises(InvalidState, match=detail):
        Workspace.from_json(_with(FORGED_ID, **fields))


def test_force_drop_salvages_record_with_bad_text(svc: WorkspaceService):
    r = _create(svc)
    _write(svc, r.id, _with(r.id, agent={"name": SURROGATE}, base_ref=SURROGATE))

    # 서로게이트는 옮기지 않는다 — 옮기면 기록(to_json)에서 UnicodeEncodeError 로 죽는다
    dropped = svc.drop(r.id)
    assert dropped.state is WsState.DROPPED
    assert dropped.agent == {} and dropped.base_ref == ""
    assert svc.hive.git.ls_remote(r.branch) is None


@pytest.mark.parametrize(
    "base,agent,labels",
    [
        ("main", {"name": SURROGATE}, {}),
        ("main", {}, {"k": SURROGATE}),
        ("main", {}, {"k": 1}),
        (SURROGATE, {}, {}),
    ],
    ids=["agent", "label", "label-int", "base"],
)
def test_create_rejects_bad_text_before_push(svc: WorkspaceService, base, agent, labels):
    with pytest.raises(InvalidState):
        svc.create(base, agent, 0, None, Checkout.NONE, labels)
    assert ls_remote_prefix(svc.hive.git.repo, ws_ref("")) == {}
    assert svc.list(None) == []


# ── record builders called directly ──────────────────────────
def test_salvage_without_record_is_not_found():
    with pytest.raises(NotFound):
        ws_mod._salvage(FORGED_ID, None, NOW)


def test_new_only_over_existing_record_conflicts():
    ws = Workspace.from_json(_valid_body(FORGED_ID))
    assert ws_mod._new_only(None, ws) is ws
    with pytest.raises(Conflict):
        ws_mod._new_only(b"{}", ws)


def test_transition_of_missing_record_is_not_found(svc: WorkspaceService):
    with pytest.raises(NotFound):
        svc._transition(FORGED_ID, "ws.dropped", lambda cur: cur)


# ── list / create / read ─────────────────────────────────────
def test_list_report_fails_when_listed_blob_vanishes(
    svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch
):
    _create(svc)
    monkeypatch.setattr(MetaStore, "read_many_at", lambda *a, **k: {})
    with pytest.raises(InvalidState, match="unreadable"):
        svc.list_report(None)


def test_failed_cleanup_step_logs_one_line_and_keeps_original_error(
    svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def meta_fails(*a, **k):
        raise Conflict("meta exhausted")

    def delete_fails(*a, **k):
        raise RemoteError("boom")

    monkeypatch.setattr(MetaStore, "apply", meta_fails)
    monkeypatch.setattr(type(svc.hive.git), "delete_remote", delete_fails)
    with pytest.raises(Conflict, match="meta exhausted"):
        _create(svc)

    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 1 and "delete remote branch" in lines[0] and "RemoteError" in lines[0]


def test_read_after_remote_branch_deleted_is_not_found(svc: WorkspaceService):
    r = _create(svc)
    svc.hive.git.delete_remote(r.branch, None)
    with pytest.raises(NotFound, match="not on remote"):
        svc.read(r.id, "README.md")


def test_tree_of_missing_path_is_not_found(svc: WorkspaceService):
    r = _create(svc)
    with pytest.raises(NotFound, match="not in"):
        svc.tree(r.id, "nope")


# ── drop / publish races ─────────────────────────────────────
def test_drop_follow_when_branch_vanishes_between_check_and_peek(
    svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch
):
    r = _create(svc)
    real = type(svc.hive.git).delete_remote

    def vanish_then_reject(self, ref, expected):
        # lease 삭제가 거절된 것으로 보이되, 그 사이 남이 브랜치를 지웠다
        real(self, ref, None)
        return False

    monkeypatch.setattr(type(svc.hive.git), "delete_remote", vanish_then_reject)
    assert svc.drop(r.id).state is WsState.DROPPED
    assert svc.hive.git.ls_remote(r.branch) is None


def test_publish_without_resolvable_head_is_invalid_state(
    svc: WorkspaceService, monkeypatch: pytest.MonkeyPatch
):
    r = _create(svc, Checkout.WORKTREE)
    real = type(svc.hive.git).rev_parse

    def no_head(self, rev):
        if rev == "HEAD" and self.repo == Path(r.path):
            return None
        return real(self, rev)

    monkeypatch.setattr(type(svc.hive.git), "rev_parse", no_head)
    with pytest.raises(InvalidState, match="no HEAD"):
        svc.publish(r.id)


# ── surfaces ─────────────────────────────────────────────────
@pytest.mark.parametrize("value", ["", None])
def test_parse_state_empty_is_none(value):
    assert parse_state(value) is None


def test_serve_runs_the_mcp_server(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []
    monkeypatch.setattr(mcp_mod.mcp, "run", lambda *a, **k: calls.append(1))
    mcp_mod.serve()
    assert calls == [1]
