"""속성 시험 — 원격 레코드·경로·push 문구·정규식은 신뢰할 수 없는 입력이다.

hypothesis 가 임의 입력을 만들고, 계약은 "값 아니면 정해진 예외 하나" 다.
"""

import json
import string
from datetime import UTC, datetime

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from gitswarm.constants import TOKEN_ID_RE, ws_ref
from gitswarm.driver.git import REJECTED_MARKERS, is_lease_rejection
from gitswarm.errors import InvalidState, NotFound
from gitswarm.events import parse_subject
from gitswarm.service.workspace import (
    OID_RE,
    ULID_RE,
    Workspace,
    WorkspaceService,
    _expired,
)

PROPS = settings(max_examples=200, deadline=None)

VALID_ID = "01J00000000000000000000000"
VALID_RECORD = {
    "id": VALID_ID,
    "state": "open",
    "base_ref": "refs/heads/main",
    "base_oid": "a" * 40,
    "branch": ws_ref(VALID_ID),
    "agent": {"name": "impl"},
    "parent": None,
    "created_at": "2026-01-01T00:00:00Z",
    "ttl_s": 60,
    "labels": {},
    "token_id": "42",
    "published_oid": None,
}
NOW = datetime(2026, 6, 1, tzinfo=UTC)

# JSON 으로 표현되는 모든 값. NaN 은 NaN != NaN 이라 왕복 동등 비교에서 뺀다.
JSON_SCALARS = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text()
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=12,
)


def _decode_or_invalid(data: bytes) -> Workspace | None:
    """from_json 의 계약: Workspace 이거나 InvalidState. 다른 예외는 그대로 올라와 시험을 깬다."""
    try:
        return Workspace.from_json(data)
    except InvalidState:
        return None


def _assert_round_trip(ws: Workspace) -> None:
    again = Workspace.from_json(ws.to_json())
    assert again == ws
    assert again.to_json() == ws.to_json()
    assert isinstance(_expired(ws, NOW), bool)


# ── Workspace.from_json ───────────────────────────────────────
@PROPS
@given(st.dictionaries(st.text(), JSON_VALUES))
def test_from_json_arbitrary_object(obj: dict):
    ws = _decode_or_invalid(json.dumps(obj).encode())
    if ws is not None:
        _assert_round_trip(ws)


@PROPS
@given(JSON_VALUES | st.binary().map(lambda b: b"\x00" + b))
def test_from_json_arbitrary_document(value):
    # 객체가 아닌 JSON, 깨진 바이트도 InvalidState 하나로 끝난다
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    assert _decode_or_invalid(data) is None


@PROPS
@given(st.sampled_from(sorted(VALID_RECORD)), JSON_VALUES)
@example("ttl_s", True)
@example("ttl_s", 10**400)
@example("created_at", "0001-01-01T00:00:00+14:00")
@example("created_at", "9999-12-31T23:59:59-14:00")
@example("published_oid", "A" * 40)
def test_from_json_mutated_field(name: str, value):
    # 유효 레코드의 필드 하나를 임의 JSON 으로 — 대부분 거절되고, 통과하면 왕복한다
    ws = _decode_or_invalid(json.dumps({**VALID_RECORD, name: value}).encode())
    if ws is not None:
        _assert_round_trip(ws)


@PROPS
@given(st.sampled_from(sorted(VALID_RECORD)))
def test_from_json_missing_field(name: str):
    record = {k: v for k, v in VALID_RECORD.items() if k != name}
    ws = _decode_or_invalid(json.dumps(record).encode())
    if ws is not None:
        _assert_round_trip(ws)


def test_valid_record_decodes():
    ws = Workspace.from_json(json.dumps(VALID_RECORD).encode())
    _assert_round_trip(ws)


# 이스케이프된 외톨이 서로게이트("\ud800")는 json.loads 를 통과하지만 UTF-8 로 못 쓴다.
SURROGATES = st.text(st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF), min_size=1)
FREE_STR_FIELDS = ("agent", "labels", "base_ref", "base_oid")


@pytest.mark.xfail(
    strict=True,
    raises=UnicodeEncodeError,
    reason="from_json accepts lone surrogates (\\ud800) in free-form fields; "
    "to_json then raises UnicodeEncodeError, so publish/drop crash outside GitswarmError",
)
@PROPS
@given(st.sampled_from(FREE_STR_FIELDS), SURROGATES)
def test_from_json_surrogate_round_trip(name: str, s: str):
    value = {"k": s} if name in {"agent", "labels"} else s
    ws = _decode_or_invalid(json.dumps({**VALID_RECORD, name: value}).encode())
    if ws is not None:
        _assert_round_trip(ws)


# ── _safe_path ────────────────────────────────────────────────
SEGMENT = st.text(alphabet=". a한 \t\\~:é", max_size=4)


@PROPS
@given(st.lists(SEGMENT, max_size=6))
@example([])
@example([""])
@example(["", "etc", "passwd"])
@example(["a", "..", "b"])
@example(["...", ". .", ".. "])
def test_safe_path(segments: list[str]):
    path = "/".join(segments)
    bad = path == "" or path.startswith("/") or ".." in segments
    if bad:
        with pytest.raises(NotFound):
            WorkspaceService._safe_path(path)
        return
    assert WorkspaceService._safe_path(path) == path


# ── is_lease_rejection ────────────────────────────────────────
# 측정 문구 5종 — 실제 stderr 한 줄의 모양으로
MEASURED = (
    " ! [rejected]        x -> refs/heads/gitswarm/meta (stale info)",
    " ! [remote rejected] x -> refs/heads/gitswarm/meta (stale info)",
    " ! [remote rejected] x -> y (incorrect old value provided)",
    " ! [remote rejected] x -> y (reference already exists)",
    " ! [remote rejected] x -> y (cannot lock ref 'refs/heads/y': is at a but expected b)",
)
HOOK_DECLINE = (
    "remote: denied by policy\n"
    "To file:///tmp/remote.git\n"
    " ! [remote rejected] x -> refs/heads/gitswarm/meta (pre-receive hook declined)\n"
    "error: failed to push some refs to 'file:///tmp/remote.git'\n"
)
NOISE = st.text(alphabet=string.digits + " \n\t-_")


def test_measured_wordings_cover_markers():
    assert all(any(m in line for line in MEASURED) for m in REJECTED_MARKERS)


@PROPS
@given(st.sampled_from(MEASURED), st.text(), st.text())
def test_lease_rejection_embedded(line: str, before: str, after: str):
    assert is_lease_rejection(before + line + after)


@PROPS
@given(NOISE, NOISE)
def test_hook_decline_is_not_lease_rejection(before: str, after: str):
    assert not is_lease_rejection(before + HOOK_DECLINE + after)


# ── parse_subject ─────────────────────────────────────────────
KIND = st.text(alphabet=string.ascii_letters + "._", min_size=1)


@PROPS
@given(KIND, st.text())
def test_parse_subject_round_trip(kind: str, rest: str):
    assert parse_subject(f"{kind} {rest}") == (kind, rest)


@PROPS
@given(KIND)
def test_parse_subject_without_space(kind: str):
    assert parse_subject(kind) == (kind, "")


def test_parse_subject_edges():
    assert parse_subject("") == ("", "")
    assert parse_subject(" x") == ("", "x")
    assert parse_subject("ws.created  x") == ("ws.created", " x")
    assert parse_subject("ws.created x\n") == ("ws.created", "x\n")


# ── 정규식 경계 ───────────────────────────────────────────────
HEX = "0123456789abcdef"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _boundary_cases(alphabet: str, n: int, outside: str) -> list[tuple[str, bool]]:
    """길이 n±1, 알파벳 양 끝, 밖의 글자, 끝 줄바꿈."""
    lo, hi = alphabet[0], alphabet[-1]
    cases = [
        (lo * n, True),
        (hi * n, True),
        (lo * (n - 1), False),
        (lo * (n + 1), False),
        (lo * n + "\n", False),
        ("\n" + lo * n, False),
        (" " + lo * (n - 1), False),
        ("", False),
    ]
    cases += [(lo * (n - 1) + c, False) for c in outside]
    return cases


@pytest.mark.parametrize(("value", "ok"), _boundary_cases(HEX, 40, "gAF/:`" + "٠"))
def test_oid_re_boundaries(value: str, ok: bool):
    assert bool(OID_RE.fullmatch(value)) is ok


@pytest.mark.parametrize(("value", "ok"), _boundary_cases(CROCKFORD, 26, "ILOUaz/" + "٠"))
def test_ulid_re_boundaries(value: str, ok: bool):
    assert bool(ULID_RE.fullmatch(value)) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("0", True),
        ("9", True),
        ("0123456789", True),
        ("", False),
        ("1\n", False),
        ("-1", False),
        ("1.0", False),
        ("١", False),
        (" 1", False),
        ("a", False),
    ],
)
def test_token_id_re_boundaries(value: str, ok: bool):
    assert bool(TOKEN_ID_RE.fullmatch(value)) is ok


@PROPS
@given(st.text())
def test_token_id_re_is_ascii_digits(s: str):
    assert bool(TOKEN_ID_RE.fullmatch(s)) is (s != "" and all(c in string.digits for c in s))


@PROPS
@given(st.text())
def test_oid_re_is_40_lower_hex(s: str):
    assert bool(OID_RE.fullmatch(s)) is (len(s) == 40 and all(c in HEX for c in s))


@PROPS
@given(st.text())
def test_ulid_re_is_26_crockford(s: str):
    assert bool(ULID_RE.fullmatch(s)) is (len(s) == 26 and all(c in CROCKFORD for c in s))
