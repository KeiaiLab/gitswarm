"""hive = 원격 하나에 대한 로컬 bare 미러 + worktree 자리. 상태는 들지 않는다."""

from __future__ import annotations

import hashlib
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from gitswarm.driver.git import Git
from gitswarm.errors import NotFound, RemoteError

HOME_ENV = "GITSWARM_HOME"
DEFAULT_HOME = Path.home() / ".gitswarm"
HIVES_DIR = "hives"
REPO_DIR = "repo.git"
WT_DIR = "wt"
HIVE_FILE = "hive.toml"
HIVE_ID_LEN = 16
PROBE_REF = "HEAD"


def resolve_home() -> Path:
    return Path(os.environ.get(HOME_ENV, DEFAULT_HOME))


def normalize_url(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    if sep:
        url = scheme.lower() + sep + rest
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url


def hive_id(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:HIVE_ID_LEN]


@dataclass(frozen=True)
class Hive:
    path: Path
    url: str
    git: Git

    @classmethod
    def init(cls, url: str, home: Path) -> Hive:
        url = url.rstrip("/")
        path = home / HIVES_DIR / hive_id(url)
        if (path / HIVE_FILE).exists():
            return cls.open(url, home)

        git = Git.init_bare(path / REPO_DIR)
        git.set_origin(url)
        try:
            git.ls_remote(PROBE_REF)
        except RemoteError:
            shutil.rmtree(path, ignore_errors=True)
            raise

        (path / WT_DIR).mkdir(exist_ok=True)
        (path / HIVE_FILE).write_text(f'url = "{url}"\n')
        return cls(path, url, git)

    @classmethod
    def open(cls, url: str, home: Path) -> Hive:
        path = home / HIVES_DIR / hive_id(url)
        if not (path / HIVE_FILE).exists():
            raise NotFound(f"hive not initialized for {url}; run `gitswarm hive init`")
        stored = tomllib.loads((path / HIVE_FILE).read_text())["url"]
        return cls(path, stored, Git(path / REPO_DIR))

    def worktree_dir(self, ws_id: str) -> Path:
        return self.path / WT_DIR / ws_id
