import base64
import json
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
import respx

from gitswarm.adapters.forgejo import ForgejoAdapter
from gitswarm.adapters.remote import Capability, Scope
from gitswarm.adapters.select import adapter_for
from gitswarm.config import Config, RemoteSpec
from gitswarm.errors import InvalidState, RemoteError, Unsupported

API = "https://git.example.com"


@pytest.fixture
def adapter() -> ForgejoAdapter:
    return ForgejoAdapter(api=API, user="bot", credential="s3cret")


def test_capabilities(adapter: ForgejoAdapter):
    assert adapter.capabilities() == frozenset({Capability.TOKEN})


@respx.mock
def test_issue_token_is_repo_scoped(adapter: ForgejoAdapter):
    route = respx.post(f"{API}/api/v1/users/bot/tokens").mock(
        return_value=httpx.Response(
            HTTPStatus.CREATED, json={"id": 7, "sha1": "tok", "name": "gitswarm-01J"}
        )
    )
    t = adapter.issue_token("org/repo", "01J", Scope.WRITE)
    assert (t.id, t.secret, t.scope) == ("7", "tok", Scope.WRITE)
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "name": "gitswarm-01J",
        "scopes": ["write:repository"],
        "repositories": ["org/repo"],
    }
    assert route.calls[0].request.headers["authorization"].startswith("Basic ")


@respx.mock
def test_revoke_404_is_success_and_500_is_error(adapter: ForgejoAdapter):
    respx.delete(f"{API}/api/v1/users/bot/tokens/7").mock(
        return_value=httpx.Response(HTTPStatus.NOT_FOUND)
    )
    adapter.revoke_token("7")
    respx.delete(f"{API}/api/v1/users/bot/tokens/8").mock(
        return_value=httpx.Response(HTTPStatus.INTERNAL_SERVER_ERROR)
    )
    with pytest.raises(RemoteError):
        adapter.revoke_token("8")


@respx.mock
def test_issue_failure_does_not_leak_credential(adapter: ForgejoAdapter):
    respx.post(f"{API}/api/v1/users/bot/tokens").mock(
        return_value=httpx.Response(HTTPStatus.UNAUTHORIZED, text="bad")
    )
    with pytest.raises(RemoteError) as ei:
        adapter.issue_token("o/r", "01J", Scope.READ)
    assert "s3cret" not in str(ei.value)
    assert "s3cret" not in repr(adapter)


def test_from_spec_reads_credential_file(tmp_path: Path):
    cred = tmp_path / "cred"
    cred.write_text("abc\n")
    a = ForgejoAdapter.from_spec(RemoteSpec("forgejo", API, "bot", str(cred)))
    assert a.credential == "abc"
    with pytest.raises(Unsupported):
        ForgejoAdapter.from_spec(RemoteSpec("forgejo", API, "bot", str(tmp_path / "missing")))


def test_adapter_for_picks_forgejo(tmp_path: Path):
    cred = tmp_path / "cred"
    cred.write_text("abc")
    cfg = Config(remotes={"git.example.com": RemoteSpec("forgejo", API, "bot", str(cred))})
    assert isinstance(adapter_for("ssh://git@git.example.com/o/r.git", cfg), ForgejoAdapter)


@respx.mock
def test_injected_client_still_carries_credential():
    route = respx.post(f"{API}/api/v1/users/bot/tokens").mock(
        return_value=httpx.Response(HTTPStatus.CREATED, json={"id": 1, "sha1": "t"})
    )
    with httpx.Client() as c:
        ForgejoAdapter(api=API, user="bot", credential="s3cret", client=c).issue_token(
            "o/r", "01J", Scope.READ
        )
        assert not c.is_closed
    expect = "Basic " + base64.b64encode(b"bot:s3cret").decode()
    assert route.calls[0].request.headers["authorization"] == expect


@respx.mock
def test_network_error_becomes_remote_error(adapter: ForgejoAdapter):
    respx.post(f"{API}/api/v1/users/bot/tokens").mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(RemoteError, match="ConnectError") as ei:
        adapter.issue_token("o/r", "01J", Scope.READ)
    assert "s3cret" not in str(ei.value)


@respx.mock
def test_malformed_created_body_is_remote_error(adapter: ForgejoAdapter):
    respx.post(f"{API}/api/v1/users/bot/tokens").mock(
        return_value=httpx.Response(HTTPStatus.CREATED, json={})
    )
    with pytest.raises(RemoteError):
        adapter.issue_token("o/r", "01J", Scope.READ)


@respx.mock
def test_revoke_rejects_path_like_token_id(adapter: ForgejoAdapter):
    route = respx.route().mock(return_value=httpx.Response(HTTPStatus.NO_CONTENT))
    with pytest.raises(InvalidState):
        adapter.revoke_token("../../admin/users/x")
    assert not route.called
