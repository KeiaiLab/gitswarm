"""hive = 원격 하나에 대한 로컬 bare 미러 + worktree 자리. 상태는 들지 않는다."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
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
TMP_PREFIX = ".tmp-"  # 짓는 중인 hive: <hives>/.tmp-<id>-<무작위>
LOCK_PREFIX = ".lock-"  # 설치 구간의 hive 별 잠금 파일: <hives>/.lock-<id>
STALE_TMP_S = 3600  # 이보다 오래된 .tmp-<id>-* 는 죽은 init 의 잔해다


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
        url = tomllib.loads(hive_file.read_text(encoding="utf-8"))["url"]
        if not isinstance(url, str):
            raise TypeError(type(url).__name__)
        return validate_remote_url(url)
    except (OSError, ValueError, KeyError, TypeError, InvalidState):
        raise InvalidState(f"hive.toml is corrupt: {hive_file}") from None


def _hive_toml(url: str) -> str:
    # JSON 문자열 이스케이프는 TOML basic string 과 호환된다(`"`, `\`, \uXXXX)
    # ensure_ascii=False — \uXXXX 대리쌍(비-BMP)은 TOML 이 받지 않는다
    return f"url = {json.dumps(url, ensure_ascii=False)}\n"


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
    (at / HIVE_FILE).write_text(_hive_toml(url), encoding="utf-8")


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """path 의 설치 구간을 프로세스·스레드 사이에서 하나씩. open 마다 새 fd 라 같은 pid 끼리도 막힌다."""
    with open(path.with_name(f"{LOCK_PREFIX}{path.name}"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _sweep_stale(path: Path, now: float) -> None:
    """죽은 init 이 남긴 .tmp-<id>-* 를 치운다. 짓는 중인 것(새것)은 건드리지 않는다."""
    for d in path.parent.glob(f"{TMP_PREFIX}{path.name}-*"):
        # 진 init 은 잠금 밖에서 제 .tmp 를 지운다 — glob 과 stat 사이에 사라질 수 있다
        try:
            age = now - d.stat().st_mtime
        except FileNotFoundError:
            continue
        if age > STALE_TMP_S:
            shutil.rmtree(d, ignore_errors=True)


def _install(tmp: Path, path: Path) -> None:
    """잠금 안에서 tmp 를 path 로 옮긴다. 남이 먼저 끝냈으면 아무것도 안 한다(tmp 는 호출자가 버린다).

    hive.toml 없는 path 는 끊긴 옛 init 의 잔해다 — worktree 가 없을 때만 치운다.
    """
    if (path / HIVE_FILE).exists():
        return
    if path.exists():
        if any((path / WT_DIR).glob("*")):
            raise InvalidState(f"{path} has worktrees but no {HIVE_FILE}; not removing it")
        shutil.rmtree(path)
    os.rename(tmp, path)


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
        url = validate_remote_url(url.rstrip("/"))
        path = hive_path(url, home)
        if (path / HIVE_FILE).exists():
            return cls.open(url, home)

        # 호출마다 고유한 임시 자리에서 다 지은 뒤 잠금 안에서 rename 한 번
        #   — 최종 자리에는 완성본만 나타나고, 같은 프로세스의 스레드끼리도 서로를 지우지 않는다
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f"{TMP_PREFIX}{path.name}-", dir=path.parent))
        try:
            _build(tmp, url)
            with _locked(path):
                _sweep_stale(path, time.time())
                _install(tmp, path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)  # 옮겨졌으면 이미 없다
        return cls.open(url, home)

    @classmethod
    def open(cls, url: str, home: Path) -> Hive:
        path = hive_path(validate_remote_url(url.rstrip("/")), home)
        if not (path / HIVE_FILE).exists():
            raise NotFound(f"hive not initialized for {url}; run `gitswarm hive init`")
        stored = _stored_url(path / HIVE_FILE)
        return cls(path, stored, Git(path / REPO_DIR))

    def worktree_dir(self, ws_id: str) -> Path:
        return self.path / WT_DIR / ws_id

    def worktree_count(self) -> int:
        return sum(1 for p in (self.path / WT_DIR).glob("*") if p.is_dir())
