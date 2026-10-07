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


@pytest.mark.parametrize("url", ["--upload-pack=touch x;", "ext::sh -c x", "/", "///"])
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


# ── 동시·재개 가능한 init ─────────────────────────────────────
def _hive_dirs(home: Path) -> list[str]:
    """완성된 hive 자리들. 짓는 중(.tmp-*)·잠금(.lock-*)이 남았으면 그것도 드러낸다."""
    names = sorted(d.name for d in (home / "hives").iterdir())
    return [n for n in names if not n.startswith(".lock-")]


FIRST_LISTERS = 6


def _first_list(remote_url: str, home: str) -> None:
    from gitswarm.service.workspace import open_service

    open_service(remote_url, Path(home)).list(None)


def test_concurrent_first_use_builds_one_hive(remote_url: str, home: Path):
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_first_list, args=(remote_url, str(home))) for _ in range(FIRST_LISTERS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert [p.exitcode for p in procs] == [0] * FIRST_LISTERS
    assert _hive_dirs(home) == [hive_id(remote_url)]


def test_interrupted_init_is_rebuilt(remote_url: str, home: Path):
    from gitswarm.driver.git import Git
    from gitswarm.store.hive import hive_path

    leftover = hive_path(remote_url, home)
    Git.init_bare(leftover / "repo.git")  # hive.toml 없이 끊긴 옛 init
    hive = Hive.init(remote_url, home)
    assert (hive.path / "hive.toml").exists() and hive.git.origin_url() == remote_url


def test_lost_race_uses_the_winner(remote_url: str, home: Path, monkeypatch):
    """rename 직전에 다른 프로세스가 끝냈다 — 내 임시 자리를 버리고 그쪽을 연다."""
    import gitswarm.store.hive as hive_mod

    winner = Hive.init(remote_url, home)
    real_exists = Path.exists
    calls = {"n": 0}

    # 첫 존재 확인만 "아직 없다"로 속여 init 이 임시 자리를 짓게 한다
    def exists(self):
        if self.name == "hive.toml" and calls["n"] == 0:
            calls["n"] += 1
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", exists)
    again = hive_mod.Hive.init(remote_url, home)
    assert again.path == winner.path
    assert _hive_dirs(home) == [hive_id(remote_url)]


def test_init_bare_failure_is_remote_error(tmp_path: Path, monkeypatch):
    import subprocess

    from gitswarm.driver.git import Git

    def fail(cmd, **kw):
        raise subprocess.CalledProcessError(128, cmd, b"", b"fatal: cannot mkdir")

    monkeypatch.setattr("gitswarm.driver.git.subprocess.run", fail)
    with pytest.raises(RemoteError, match="cannot mkdir"):
        Git.init_bare(tmp_path / "repo.git")


INIT_THREADS = 6


def test_concurrent_init_in_one_process_builds_one_hive(remote_url: str, home: Path):
    """FastMCP 는 sync 도구를 스레드 풀에서 돌린다 — 같은 pid 의 init 들이 서로를 지우면 안 된다."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(INIT_THREADS) as pool:
        hives = list(pool.map(lambda _: Hive.init(remote_url, home), range(INIT_THREADS)))
    assert {h.path for h in hives} == {hives[0].path}
    assert _hive_dirs(home) == [hive_id(remote_url)]


def test_leftover_with_worktrees_is_not_removed(remote_url: str, home: Path):
    from gitswarm.driver.git import Git
    from gitswarm.errors import InvalidState
    from gitswarm.store.hive import hive_path

    leftover = hive_path(remote_url, home)
    Git.init_bare(leftover / "repo.git")
    (leftover / "wt" / "01J00000000000000000000000").mkdir(parents=True)
    with pytest.raises(InvalidState, match="has worktrees"):
        Hive.init(remote_url, home)
    assert (leftover / "wt" / "01J00000000000000000000000").is_dir()
    assert _hive_dirs(home) == [hive_id(remote_url)]


def test_stale_temp_dirs_are_swept(remote_url: str, home: Path):
    import os
    import time

    from gitswarm.store.hive import STALE_TMP_S

    hives = home / "hives"
    stale = hives / f".tmp-{hive_id(remote_url)}-old"
    fresh = hives / f".tmp-{hive_id(remote_url)}-busy"
    for d in (stale, fresh):
        d.mkdir(parents=True)
    old = time.time() - STALE_TMP_S - 1
    os.utime(stale, (old, old))

    Hive.init(remote_url, home)
    assert not stale.exists() and fresh.exists()


def test_sweep_skips_a_temp_dir_that_vanishes(remote_url: str, home: Path, monkeypatch):
    """남의 .tmp 는 잠금 밖에서 지워진다(진 init 의 정리) — glob 과 stat 사이에 사라져도 된다."""
    gone = home / "hives" / f".tmp-{hive_id(remote_url)}-gone"
    gone.mkdir(parents=True)
    real_stat = Path.stat

    def stat(self, *a, **kw):
        if self == gone:
            raise FileNotFoundError(self)
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", stat)
    assert Hive.init(remote_url, home).url == remote_url
