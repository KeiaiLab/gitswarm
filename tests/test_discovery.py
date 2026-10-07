from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.driver.git import Git
from gitswarm.service.discovery import discover_remote
from gitswarm.service.workspace import Checkout, WorkspaceService
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore
from tests.conftest import git


def _worktree(remote_url: str, home: Path) -> Path:
    hive = Hive.init(remote_url, home)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())
    return Path(svc.create("main", {}, 0, None, Checkout.WORKTREE, {}).path)


def test_hive_worktree_names_its_hive(remote_url: str, home: Path, monkeypatch):
    wt = _worktree(remote_url, home)
    (wt / "sub").mkdir()

    # 규칙 1 은 git 을 부르지 않는다 — hive.toml 로 판정한다
    def boom(self, name):
        raise AssertionError("rule 2 used")

    monkeypatch.setattr(Git, "remote_url", boom)
    assert discover_remote(wt / "sub", home) == remote_url


def test_plain_clone_uses_origin(remote_url: str, home: Path, tmp_path: Path):
    clone = tmp_path / "clone"
    git("clone", "-q", remote_url, str(clone), cwd=tmp_path)
    (clone / "deep").mkdir()
    assert discover_remote(clone / "deep", home) == remote_url


def test_hive_dir_outside_worktrees_falls_through(remote_url: str, home: Path):
    hive = Hive.init(remote_url, home)
    assert discover_remote(hive.path / "wt", home) is None


def test_repo_without_origin_is_none(tmp_path: Path, home: Path):
    repo = tmp_path / "bare-local"
    git("init", "-q", str(repo), cwd=tmp_path)
    assert discover_remote(repo, home) is None


def test_plain_dir_is_none(tmp_path: Path, home: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert discover_remote(empty, home) is None


@pytest.mark.parametrize("name", ["origin", "upstream"])
def test_remote_url_by_name(remote_url: str, tmp_path: Path, name: str):
    clone = tmp_path / "c"
    git("clone", "-q", "-o", name, remote_url, str(clone), cwd=tmp_path)
    assert Git(clone).remote_url(name) == remote_url
    assert Git(clone).remote_url("nope") is None
