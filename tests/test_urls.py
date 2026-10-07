import pytest

from gitswarm.errors import InvalidState
from gitswarm.urls import is_ssh_url, validate_remote_url

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
