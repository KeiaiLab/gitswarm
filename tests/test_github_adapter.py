import json
import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from gitswarm.adapters.github import GitHubAdapter
from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.adapters.select import adapter_for
from gitswarm.config import Config, RemoteSpec
from gitswarm.errors import InvalidState, RemoteError, Unsupported

API = "https://api.github.com"
APP_ID = 12
INSTALL_ID = 34
URL = f"{API}/app/installations/{INSTALL_ID}/access_tokens"
EXPIRES = "2026-10-07T12:00:00Z"
EXPIRES_EPOCH = int(datetime(2026, 10, 7, 12, tzinfo=UTC).timestamp())
JWT_SPAN_S = 600
GITHUB_EXP_MAX_S = 600
IAT_SKEW_S = 59


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def adapter(pem: str) -> GitHubAdapter:
    return GitHubAdapter(api=API, app_id=APP_ID, installation_id=INSTALL_ID, private_key=pem)


def created() -> httpx.Response:
    return httpx.Response(HTTPStatus.CREATED, json={"token": "ghs_x", "expires_at": EXPIRES})


def test_capabilities(adapter: GitHubAdapter):
    assert adapter.capabilities() == frozenset({Capability.TOKEN})


@respx.mock
def test_issue_token_ok(adapter: GitHubAdapter):
    respx.post(URL).mock(return_value=created())
    t = adapter.issue_token("org/repo", "01J", Scope.WRITE)
    assert (t.secret, t.scope) == ("ghs_x", Scope.WRITE)
    assert t.id.isdigit()
    assert int(t.id) == EXPIRES_EPOCH


@respx.mock
def test_jwt_is_signed_and_short_lived(adapter: GitHubAdapter, key: rsa.RSAPrivateKey):
    route = respx.post(URL).mock(return_value=created())
    adapter.issue_token("org/repo", "01J", Scope.READ)
    auth = route.calls[0].request.headers["authorization"]
    assert auth.startswith("Bearer ")
    h = route.calls[0].request.headers
    assert h["accept"] == "application/vnd.github+json"
    assert h["x-github-api-version"] == "2022-11-28"
    raw = auth.removeprefix("Bearer ")
    assert jwt.get_unverified_header(raw)["alg"] == "RS256"
    claims = jwt.decode(raw, key.public_key(), algorithms=["RS256"], options={"verify_iat": False})
    assert claims["iss"] == str(APP_ID) or claims["iss"] == APP_ID
    assert claims["exp"] - claims["iat"] == JWT_SPAN_S
    assert claims["exp"] - time.time() <= GITHUB_EXP_MAX_S
    assert claims["iat"] <= time.time() - IAT_SKEW_S


@respx.mock
@pytest.mark.parametrize(("scope", "perm"), [(Scope.READ, "read"), (Scope.WRITE, "write")])
def test_body_names_repo_only(adapter: GitHubAdapter, scope: Scope, perm: str):
    route = respx.post(URL).mock(return_value=created())
    adapter.issue_token("org/repo", "01J", scope)
    assert json.loads(route.calls[0].request.content) == {
        "repositories": ["repo"],
        "permissions": {"contents": perm},
    }


@respx.mock
@pytest.mark.parametrize("status", [HTTPStatus.UNAUTHORIZED, HTTPStatus.UNPROCESSABLE_ENTITY])
def test_http_failure_names_status_only(adapter: GitHubAdapter, pem: str, status: int):
    respx.post(URL).mock(return_value=httpx.Response(status, json={"message": "ghs_leak " + pem}))
    with pytest.raises(RemoteError, match=str(int(status))) as e:
        adapter.issue_token("org/repo", "01J", Scope.WRITE)
    assert "ghs_" not in str(e.value)
    assert "PRIVATE KEY" not in str(e.value)


@respx.mock
def test_network_failure(adapter: GitHubAdapter):
    respx.post(URL).mock(side_effect=httpx.ConnectError("boom api.github.com"))
    with pytest.raises(RemoteError, match="ConnectError") as e:
        adapter.issue_token("org/repo", "01J", Scope.WRITE)
    assert "api.github.com" not in str(e.value)


@respx.mock
def test_malformed_body(adapter: GitHubAdapter):
    respx.post(URL).mock(return_value=httpx.Response(HTTPStatus.CREATED, json={}))
    with pytest.raises(RemoteError, match="malformed"):
        adapter.issue_token("org/repo", "01J", Scope.WRITE)


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def post(self, url: str, **_: object) -> httpx.Response:
        self.calls.append(("POST", url))
        return created()


def test_injected_client_is_used(pem: str):
    stub = StubClient()
    a = GitHubAdapter(
        api=API,
        app_id=APP_ID,
        installation_id=INSTALL_ID,
        private_key=pem,
        client=stub,  # type: ignore[arg-type]
    )
    assert a.issue_token("o/r", "01J", Scope.READ).secret == "ghs_x"
    assert stub.calls == [("POST", URL)]


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"token": "ghs_x", "expires_at": "2026-10-07T12:00:00"},
        {"token": None, "expires_at": EXPIRES},
        {"token": "", "expires_at": EXPIRES},
        {"token": "ghs_x", "expires_at": "1960-01-01T00:00:00Z"},
    ],
)
def test_malformed_fields(adapter: GitHubAdapter, body: dict[str, object]):
    respx.post(URL).mock(return_value=httpx.Response(HTTPStatus.CREATED, json=body))
    with pytest.raises(RemoteError, match="malformed"):
        adapter.issue_token("org/repo", "01J", Scope.WRITE)


def test_token_repr_hides_secret():
    assert "ghs_x" not in repr(Token(id="1", secret="ghs_x", scope=Scope.READ))


@pytest.mark.parametrize("digits", [12, 20, 25])
def test_revoke_rejects_huge_id(adapter: GitHubAdapter, digits: int):
    with pytest.raises(InvalidState):
        adapter.revoke_token("9" * digits)


def test_from_spec_rejects_non_rsa_pem(tmp_path: Path, key: rsa.RSAPrivateKey):
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    for data in (pub, b"garbage"):
        f = tmp_path / "bad.pem"
        f.write_bytes(data)
        with pytest.raises(Unsupported, match="RSA private key"):
            GitHubAdapter.from_spec(RemoteSpec("github", "", "12/34", str(f)))


def test_from_spec_rejects_ec_key(tmp_path: Path):
    ec_key = ec.generate_private_key(ec.SECP256R1())
    f = tmp_path / "ec.pem"
    f.write_bytes(
        ec_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(Unsupported, match="RSA private key"):
        GitHubAdapter.from_spec(RemoteSpec("github", "", "12/34", str(f)))


def test_repr_hides_pem(adapter: GitHubAdapter, pem: str):
    assert "PRIVATE KEY" not in repr(adapter)
    assert pem not in repr(adapter)


def test_revoke_is_documented_noop(adapter: GitHubAdapter, capsys: pytest.CaptureFixture[str]):
    adapter.revoke_token("123")
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("gitswarm: github token expires at ")
    assert err.endswith("cannot revoke by id\n")


def test_revoke_rejects_bad_id(adapter: GitHubAdapter):
    with pytest.raises(InvalidState):
        adapter.revoke_token("../x")


def spec(tmp_path: Path, pem: str, user: str = "12/34", api: str = "") -> RemoteSpec:
    f = tmp_path / "app.pem"
    f.write_text(pem)
    return RemoteSpec("github", api, user, str(f))


def test_from_spec(tmp_path: Path, pem: str):
    a = GitHubAdapter.from_spec(spec(tmp_path, pem))
    assert (a.api, a.app_id, a.installation_id) == (API, 12, 34)
    b = GitHubAdapter.from_spec(spec(tmp_path, pem, api="https://ghe.example.com/api/v3/"))
    assert b.api == "https://ghe.example.com/api/v3"


@pytest.mark.parametrize("user", ["12", "a/b", "1/2/3", "", "0/5", "5/0", "1" * 5000 + "/1", "١/٢"])
def test_from_spec_bad_user(tmp_path: Path, pem: str, user: str):
    with pytest.raises(Unsupported, match="app_id"):
        GitHubAdapter.from_spec(spec(tmp_path, pem, user=user))


def test_from_spec_missing_pem(tmp_path: Path):
    with pytest.raises(Unsupported, match="pem"):
        GitHubAdapter.from_spec(RemoteSpec("github", "", "12/34", str(tmp_path / "none.pem")))
    with pytest.raises(Unsupported, match="pem"):
        GitHubAdapter.from_spec(RemoteSpec("github", "", "12/34", ""))


def test_adapter_for_github(tmp_path: Path, pem: str):
    cfg = Config(remotes={"github.com": spec(tmp_path, pem)})
    assert isinstance(adapter_for("https://github.com/o/r", cfg), GitHubAdapter)
