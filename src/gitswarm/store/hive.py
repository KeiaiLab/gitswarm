"""hive = 원격 하나에 대한 로컬 bare 미러 + worktree 자리. 상태는 들지 않는다."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from gitswarm.driver.git import Git
from gitswarm.errors import InvalidState, NotFound
from gitswarm.urls import validate_remote_url

HOME_ENV = "GITSWARM_HOME"
DEFAULT_HOME = Path.home() / ".gitswarm"
HIVES_DIR = "hives"
REPO_DIR = "repo.git"
WT_DIR = "wt"
HIVE_FILE = "hive.toml"
HIVE_ID_LEN = 16
PROBE_REF = "HEAD"
TMP_PREFIX = ".tmp-"  # 짓는 중인 hive: <hives>/.tmp-<id>-<pid>
INSTALL_TRIES = 2  # 첫 rename 이 잔해에 막히면 치우고 한 번 더


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


def _stored_url(hive_file: Path) -> str:
    """hive.toml 의 url. 손상·허용 목록 밖이면 InvalidState — 이 값은 곧 git 에 넘어간다."""
    try:
        url = tomllib.loads(hive_file.read_text())["url"]
        if not isinstance(url, str):
            raise TypeError(type(url).__name__)
        return validate_remote_url(url)
    except (ValueError, KeyError, TypeError, InvalidState):
        raise InvalidState(f"hive.toml is corrupt: {hive_file}") from None


def _hive_toml(url: str) -> str:
    # JSON 문자열 이스케이프는 TOML basic string 과 호환된다(`"`, `\`, \uXXXX)
    return f"url = {json.dumps(url)}\n"


def worktree_owner(path: Path, home: Path) -> str | None:
    """path 가 어떤 hive 의 wt/<id> 아래면 그 hive 의 URL. wt/ 자체는 worktree 가 아니다."""
    where = path.resolve()
    for hive_file in (home / HIVES_DIR).glob(f"*/{HIVE_FILE}"):
        wt_root = (hive_file.parent / WT_DIR).resolve()
        if where != wt_root and where.is_relative_to(wt_root):
            return _stored_url(hive_file)
    return None


def _build(at: Path, url: str) -> None:
    """at 에 hive 하나를 다 짓는다. 원격이 안 닿으면 RemoteError."""
    git = Git.init_bare(at / REPO_DIR)
    git.set_origin(url)
    git.ls_remote(PROBE_REF)
    (at / WT_DIR).mkdir()
    (at / HIVE_FILE).write_text(_hive_toml(url))


def _install(tmp: Path, path: Path) -> None:
    """tmp 를 path 로 옮긴다. 남이 먼저 끝냈으면 그쪽을 쓰고 tmp 는 버린다.

    hive.toml 없는 path 는 끊긴 옛 init 의 잔해다 — 치우고 다시 옮긴다.
    """
    for _ in range(INSTALL_TRIES):
        try:
            os.rename(tmp, path)
            return
        except OSError:
            if (path / HIVE_FILE).exists():
                break
            shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)


def hive_path(url: str, home: Path) -> Path:
    """url 의 hive 자리. 있든 없든 계산만 한다."""
    return home / HIVES_DIR / hive_id(url)


@dataclass(frozen=True)
class Hive:
    path: Path
    url: str
    git: Git

    @classmethod
    def init(cls, url: str, home: Path) -> Hive:
        url = validate_remote_url(url).rstrip("/")
        path = hive_path(url, home)
        if (path / HIVE_FILE).exists():
            return cls.open(url, home)

        # 형제 임시 자리에서 다 지은 뒤 rename 한 번 — 최종 자리에는 완성본만 나타난다
        tmp = path.with_name(f"{TMP_PREFIX}{path.name}-{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _build(tmp, url)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        _install(tmp, path)
        return cls.open(url, home)

    @classmethod
    def open(cls, url: str, home: Path) -> Hive:
        path = hive_path(validate_remote_url(url), home)
        if not (path / HIVE_FILE).exists():
            raise NotFound(f"hive not initialized for {url}; run `gitswarm hive init`")
        stored = _stored_url(path / HIVE_FILE)
        return cls(path, stored, Git(path / REPO_DIR))

    def worktree_dir(self, ws_id: str) -> Path:
        return self.path / WT_DIR / ws_id

    def worktree_count(self) -> int:
        return sum(1 for p in (self.path / WT_DIR).glob("*") if p.is_dir())
