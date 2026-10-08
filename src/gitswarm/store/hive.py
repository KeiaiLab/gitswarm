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
from gitswarm.urls import redact_url, validate_remote_url

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
STALE_TMP_S = 3600  # 이보다 오래된 .tmp-*·.lock-* 는 죽은 init 의 잔해다


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


def worktree_hives(ws_id: str, home: Path) -> list[str]:
    """wt/<ws_id> 디렉터리를 가진 hive 들의 URL. ws_id 는 호출자가 검증한 ULID 다."""
    return [
        _stored_url(hive_file)
        for hive_file in sorted((home / HIVES_DIR).glob(f"*/{HIVE_FILE}"))
        if (hive_file.parent / WT_DIR / ws_id).is_dir()
    ]


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
    lock = path.with_name(f"{LOCK_PREFIX}{path.name}")
    with open(lock, "a") as f:
        # "a" 로 열면 mtime 이 그대로다 — 쓰는 중인 잠금이 오래된 잔해로 보이지 않게 만진다
        os.utime(lock)
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _sweep_stale(hives: Path, pattern: str, now: float) -> None:
    """죽은 init 이 남긴 .tmp-* 디렉터리·.lock-* 파일을 치운다. 쓰는 중인 것(새것)은 건드리지 않는다."""
    for p in hives.glob(pattern):
        # 진 init 은 잠금 밖에서 제 .tmp 를 지운다 — glob 과 stat 사이에 사라질 수 있다
        try:
            age = now - p.stat().st_mtime
        except FileNotFoundError:
            continue
        if age <= STALE_TMP_S:
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


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
        tmp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=f"{TMP_PREFIX}{path.name}-", dir=path.parent))
            _build(tmp, url)
            with _locked(path):
                _sweep_stale(path.parent, f"{TMP_PREFIX}{path.name}-*", time.time())
                _install(tmp, path)
        except OSError as e:
            # 권한·디스크 문제 — JSON 계약 안의 오류로(날 traceback 아님)
            raise InvalidState(f"cannot write hive home: {type(e).__name__}") from None
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)  # 옮겨졌으면 이미 없다
        return cls.open(url, home)

    @classmethod
    def open(cls, url: str, home: Path) -> Hive:
        path = hive_path(validate_remote_url(url.rstrip("/")), home)
        if not (path / HIVE_FILE).exists():
            raise NotFound(f"hive not initialized for {redact_url(url)}; run `gitswarm hive init`")
        stored = _stored_url(path / HIVE_FILE)
        hive = cls(path, stored, Git(path / REPO_DIR))
        hive._tidy(time.time())
        return hive

    def _tidy(self, now: float) -> None:
        """명령마다 한 번: 죽은 init 의 잔해(.tmp-*·.lock-*)와 손으로 지운 worktree 의 메타데이터를 걷는다.

        init 안의 청소는 hive 가 생기기 전에만 돈다 — 다 지은 뒤의 잔해는 여기서만 치운다.
        """
        for pattern in (f"{TMP_PREFIX}*", f"{LOCK_PREFIX}*"):
            _sweep_stale(self.path.parent, pattern, now)
        self.git.worktree_prune()

    def worktree_dir(self, ws_id: str) -> Path:
        return self.path / WT_DIR / ws_id

    def worktree_count(self) -> int:
        return sum(1 for p in (self.path / WT_DIR).glob("*") if p.is_dir())
