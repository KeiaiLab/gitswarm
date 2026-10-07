from pathlib import Path

import pytest

from gitswarm.errors import NotFound, RemoteError
from gitswarm.store.hive import Hive, hive_id, normalize_url, resolve_home


def test_normalize_url_variants():
    a = normalize_url("ssh://git@host/org/repo.git")
    b = normalize_url("SSH://git@host/org/repo/")
    assert a == b == "ssh://git@host/org/repo"
    assert hive_id(a) == hive_id(b)
    assert len(hive_id(a)) == 16


def test_init_is_idempotent_and_open_finds_it(remote_url: str, home: Path):
    h1 = Hive.init(remote_url, home)
    h2 = Hive.init(remote_url, home)
    assert h1.path == h2.path
    assert (h1.path / "repo.git" / "HEAD").exists()
    assert (h1.path / "hive.toml").read_text().strip() == f'url = "{remote_url}"'
    assert Hive.open(remote_url, home).url == remote_url
    assert h1.git.origin_url() == remote_url
    assert Hive.init(remote_url + "/", home).path == h1.path


def test_open_missing_is_not_found(remote_url: str, home: Path):
    with pytest.raises(NotFound):
        Hive.open(remote_url, home)


def test_init_unreachable_remote_leaves_nothing(tmp_path: Path, home: Path):
    bad = (tmp_path / "nope.git").as_uri()
    with pytest.raises(RemoteError):
        Hive.init(bad, home)
    assert list(home.glob("hives/*")) == []


def test_resolve_home_env(home: Path):
    assert resolve_home() == home


@pytest.mark.parametrize("url", ["--upload-pack=touch x;", "ext::sh -c x"])
def test_hive_refuses_bad_urls(url: str, home: Path):
    from gitswarm.errors import InvalidState

    with pytest.raises(InvalidState):
        Hive.init(url, home)
    with pytest.raises(InvalidState):
        Hive.open(url, home)


# ── hive.toml ────────────────────────────────────────────────
@pytest.mark.parametrize("name", ['r"#x.git', "b\\q.git"])
def test_hive_toml_round_trips_odd_urls(tmp_path: Path, home: Path, name: str):
    from tests.conftest import git

    bare = tmp_path / name
    git("init", "--bare", "-q", str(bare), cwd=tmp_path)
    url = str(bare)
    assert Hive.init(url, home).url == url
    assert Hive.open(url, home).url == url


@pytest.mark.parametrize(
    "text", ["url = [", "nope = 1\n", "url = 7\n", 'url = "--upload-pack=x"\n']
)
def test_corrupt_hive_toml_is_invalid_state(remote_url: str, home: Path, text: str):
    from gitswarm.errors import InvalidState

    hive = Hive.init(remote_url, home)
    (hive.path / "hive.toml").write_text(text)
    with pytest.raises(InvalidState, match=r"hive\.toml is corrupt"):
        Hive.open(remote_url, home)
