import os
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


def test_non_bmp_url_round_trips(tmp_path: Path, home: Path):
    from tests.conftest import git

    bare = tmp_path / "😀.git"
    git("init", "--bare", "-q", str(bare), cwd=tmp_path)
    assert Hive.init(str(bare), home).url == str(bare)
    assert Hive.open(str(bare), home).url == str(bare)


def test_unreadable_hive_toml_is_invalid_state(remote_url: str, home: Path):
    from gitswarm.errors import InvalidState

    hive = Hive.init(remote_url, home)
    (hive.path / "hive.toml").unlink()
    (hive.path / "hive.toml").mkdir()  # 읽으면 IsADirectoryError
    with pytest.raises(InvalidState, match=r"hive\.toml is corrupt"):
        Hive.open(remote_url, home)


LOCK_THREADS = 4
SLOW_RENAME_S = 0.2


def test_install_is_serialized_by_the_lock(remote_url: str, home: Path, monkeypatch):
    """재확인과 rename 사이를 늘린다 — 잠금이 없으면 진 스레드의 rename 이 실패한다."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    real_rename = os.rename

    def slow_rename(src, dst):
        time.sleep(SLOW_RENAME_S)
        real_rename(src, dst)

    monkeypatch.setattr("gitswarm.store.hive.os.rename", slow_rename)
    with ThreadPoolExecutor(LOCK_THREADS) as pool:
        hives = list(pool.map(lambda _: Hive.init(remote_url, home), range(LOCK_THREADS)))
    assert len({h.path for h in hives}) == 1
    assert _hive_dirs(home) == [hive_id(remote_url)]


def _deny(*args, **kwargs):
    raise PermissionError("read-only")


@pytest.mark.parametrize(
    "primitive",
    [
        "gitswarm.store.hive.Path.mkdir",
        "gitswarm.store.hive.tempfile.mkdtemp",
        "gitswarm.store.hive.os.rename",
    ],
    ids=["mkdir", "mkdtemp", "rename"],
)
def test_unwritable_home_is_invalid_state(remote_url: str, home: Path, monkeypatch, primitive):
    """권한 비트 대신 쓰기 원시 연산을 막는다 — root(CI 파드)에서도 같은 길을 탄다."""
    from gitswarm.errors import InvalidState

    monkeypatch.setattr(primitive, _deny)
    with pytest.raises(InvalidState, match="cannot write hive home: PermissionError"):
        Hive.init(remote_url, home)


# ── hive 위생: 열 때마다 잔해를 치운다 ──────────────────────────
def _ws_list(remote_url: str) -> None:
    from typer.testing import CliRunner

    from gitswarm.surfaces.cli import app

    res = CliRunner().invoke(app, ["ws", "list"], env={"GITSWARM_REMOTE": remote_url})
    assert res.exit_code == 0, res.output


def test_open_sweeps_stale_residue_and_keeps_fresh(remote_url: str, home: Path):
    import time

    from gitswarm.store.hive import STALE_TMP_S

    Hive.init(remote_url, home)
    hives = home / "hives"
    stale_tmp = hives / ".tmp-0123456789abcdef-dead"
    stale_lock = hives / f".lock-{hive_id(remote_url)}"
    fresh_tmp = hives / ".tmp-0123456789abcdef-busy"
    stale_tmp.mkdir()
    fresh_tmp.mkdir()
    stale_lock.touch()
    old = time.time() - STALE_TMP_S - 1
    for p in (stale_tmp, stale_lock):
        os.utime(p, (old, old))

    _ws_list(remote_url)
    assert not stale_tmp.exists() and not stale_lock.exists()
    assert fresh_tmp.exists()


def test_open_prunes_a_hand_deleted_worktree_once(remote_url: str, home: Path, monkeypatch):
    import shutil

    from gitswarm.driver.git import Git
    from gitswarm.service.workspace import Checkout, open_service

    svc = open_service(remote_url, home)
    wt = Path(svc.create("main", {}, 0, None, Checkout.WORKTREE, {}).path)
    meta = svc.hive.git.repo / "worktrees" / wt.name
    assert meta.is_dir()
    shutil.rmtree(wt)

    real = Git._run
    prunes: list[tuple[str, ...]] = []

    def run(self, *args, **kw):
        if args[:2] == ("worktree", "prune"):
            prunes.append(args)
        return real(self, *args, **kw)

    monkeypatch.setattr(Git, "_run", run)
    Hive.open(remote_url, home)
    assert len(prunes) == 1 and not meta.exists()


def test_lock_in_use_is_touched_so_the_sweep_keeps_it(tmp_path: Path):
    import time

    from gitswarm.store.hive import STALE_TMP_S, _locked

    # "a" 로 열면 mtime 이 그대로다 — 쓰는 중인 잠금이 오래된 잔해로 보여 지워지면 안 된다
    target = tmp_path / "hiveid"
    lock = tmp_path / ".lock-hiveid"
    lock.touch()
    old = time.time() - STALE_TMP_S - 1
    os.utime(lock, (old, old))

    with _locked(target):
        assert time.time() - lock.stat().st_mtime < STALE_TMP_S


def _stale(p: Path) -> None:
    import time

    from gitswarm.store.hive import STALE_TMP_S

    old = time.time() - STALE_TMP_S - 1
    os.utime(p, (old, old))


def test_sweep_tolerates_a_file_removed_concurrently(remote_url: str, home: Path, monkeypatch):
    # 다른 명령의 청소가 먼저 지웠다 — 목록과 unlink 사이에 사라져도 오류가 아니다
    Hive.init(remote_url, home)
    lock = home / "hives" / ".lock-0123456789abcdef"
    lock.touch()
    _stale(lock)
    real = Path.unlink
    raised: list[Path] = []

    def unlink(self, *a, **kw):
        if self == lock and not raised:
            raised.append(self)
            raise FileNotFoundError(self)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert Hive.open(remote_url, home).url == remote_url
    assert raised == [lock]


def test_sweep_skips_a_hive_whose_init_lock_is_held(remote_url: str, home: Path):
    import fcntl

    # 잠금이 잡혀 있으면 그 hive 의 init 이 도는 중이다 — 오래된 .tmp 라도 건드리지 않는다
    Hive.init(remote_url, home)
    hives = home / "hives"
    tmp = hives / ".tmp-0123456789abcdef-busy"
    tmp.mkdir()
    _stale(tmp)
    with open(hives / ".lock-0123456789abcdef", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        Hive.open(remote_url, home)
        assert tmp.exists()
        fcntl.flock(held, fcntl.LOCK_UN)

    Hive.open(remote_url, home)
    assert not tmp.exists()
