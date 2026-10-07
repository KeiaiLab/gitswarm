import pytest

from gitswarm.errors import InvalidState
from gitswarm.urls import REDACT_MAX_CHARS, is_ssh_url, redact_url, validate_remote_url

GOOD = [
    "ssh://git@host/org/repo.git",
    "git+ssh://git@host/org/repo.git",
    "SSH://git@host:2222/org/repo",
    "https://host/org/repo.git",
    "http://host/org/repo",
    "git://host/org/repo",
    "file:///srv/r.git",
    "git@host:org/repo.git",
    "host:repo",
    "/srv/repos/r.git",
    '/tmp/r"#x.git',
    "ssh://git@[::1]/repo",
    "ssh://git@host:2222/org/repo",
    "ssh://git@[fe80::1]:22/repo",
    "ssh://host/~user/repo",
    "git@[::1]:repo",
    "file://localhost/srv/r.git",
    "/tmp/한글 저장소.git",
    "/tmp/😀.git",
]

BAD = [
    "",
    "--upload-pack=touch /tmp/gs-poc/pwned-doctor;",
    "-oProxyCommand=x",
    "ext::sh -c touch% /tmp/pwned",
    "fd::17",
    "ssh-helper::x",
    "ftp://host/r",
    "foo://host/r",
    "file:///r\n.git",
    "/r\x00.git",
    "/r\x7f.git",
    "/r\t.git",
    "relative/path",
    "host:",
    "git@-oProxyCommand=x:repo",
    "ssh://-oProxyCommand=x/repo",
    "ssh://git@-oProxyCommand=x/repo",
    # 재심 표: ssh 의 host 검사(OpenSSH ≥ 9.6)에 기대지 않는다
    "ssh://user@[-oPC=x]:22/r",
    "ssh://host:-1/x",
    "ssh://ho%0ast/x",
    "user@host:-oPC",
    "host;id",
    "a;touch$IFS",
    "ssh://us;er@host/r",
    "ssh://host:22x/r",
    "https://user:token@host/r",
    "ssh:///repo",
    "git@ho st:repo",
    "/r\udc80.git",
]


@pytest.mark.parametrize("url", GOOD)
def test_accepts(url: str):
    assert validate_remote_url(url) == url


@pytest.mark.parametrize("url", BAD)
def test_rejects(url: str):
    with pytest.raises(InvalidState, match="invalid remote url"):
        validate_remote_url(url)


def test_rejects_non_string():
    with pytest.raises(InvalidState):
        validate_remote_url(7)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url,ssh",
    [
        ("ssh://h/r", True),
        ("git+ssh://h/r", True),
        ("git@h:r", True),
        ("ext::ssh h", False),
        ("https://h/r", False),
        ("/srv/r", False),
        ("file:///r", False),
    ],
)
def test_is_ssh_url(url: str, ssh: bool):
    assert is_ssh_url(url) is ssh


SECRET_URL = "https://bot:s3cret@host/r"


def test_refusal_redacts_userinfo():
    with pytest.raises(InvalidState) as e:
        validate_remote_url(SECRET_URL)
    assert "***@host/r" in e.value.detail and "s3cret" not in e.value.detail


@pytest.mark.parametrize(
    "url,shown",
    [
        (SECRET_URL, "https://***@host/r"),
        ("https://ghp_token@github.com/o/r", "https://***@github.com/o/r"),
        ("ssh://git@host/r", "ssh://***@host/r"),
        ("https://a:b@c@host/r", "https://***@host/r"),
        ("https://host/r", "https://host/r"),
        ("git@host:r", "git@host:r"),
        ("/srv/r.git", "/srv/r.git"),
    ],
)
def test_redact_url(url: str, shown: str):
    assert redact_url(url) == shown


def test_redact_url_truncates():
    shown = redact_url("https://host/" + "x" * 500)
    assert len(shown) == REDACT_MAX_CHARS and shown.endswith("…")


def test_refusal_of_non_string_names_its_type():
    with pytest.raises(InvalidState, match="int"):
        validate_remote_url(7)  # type: ignore[arg-type]
