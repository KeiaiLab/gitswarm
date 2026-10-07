"""환경 점검. 검사는 서로 독립이다 — 하나가 실패해도 나머지를 본다.

git ─ home ─ config ─┬─ remote 없음 ──▶ 끝(원격 검사 생략)
                     └─ remote ─ hive ─ tokens ─ ssh_mux
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.adapters.select import FORGEJO, GITHUB
from gitswarm.config import CONFIG_FILE, Config, load_config
from gitswarm.driver.git import Git
from gitswarm.errors import GitswarmError, InvalidState, NotFound, RemoteError
from gitswarm.events import sinks_from_config
from gitswarm.service.workspace import WorkspaceService, unrevoked
from gitswarm.store.hive import REPO_DIR, Hive, hive_path
from gitswarm.store.meta import MetaStore
from gitswarm.urls import is_ssh_url, validate_remote_url

# 쓰는 것(ls-remote --symref · merge-base --is-ancestor · cat-file --batch ·
# push --force-with-lease · worktree · update-ref -d)은 2.39 에 다 있다.
# tested on 2.39 (CI, Debian bookworm) and 2.55 (dev)
GIT_MIN_VERSION = (2, 39, 0)
GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
CREDENTIAL_ADAPTERS = frozenset({FORGEJO, GITHUB})


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def diagnose(remote: str | None, home: Path) -> dict:
    """{ok, checks: [{name, ok, detail}]}. ok = 모든 검사 통과."""
    checks = [
        _guard("git", _git),
        _guard("home", lambda: _home(home)),
        _guard("config", lambda: _config(home)),
    ]
    if remote is None:
        checks.append(Check("remote", True, "none given or found; remote checks skipped"))
    elif (refused := _refused(remote)) is not None:
        checks.append(refused)
    else:
        hive = _guard("hive", lambda: _hive(remote, home))
        # tokens 는 hive 로 meta 를 읽는다 — hive 가 망가졌으면 같은 실패를 두 번 세지 않는다
        tokens = (
            _guard("tokens", lambda: _tokens(remote, home))
            if hive.ok
            else Check("tokens", True, "skipped; hive check failed")
        )
        checks += [
            _guard("remote", lambda: _remote(remote)),
            hive,
            tokens,
            _guard("ssh_mux", lambda: _ssh_mux(remote, home)),
        ]
    return {"ok": all(c.ok for c in checks), "checks": [asdict(c) for c in checks]}


def _guard(name: str, check: Callable[[], Check]) -> Check:
    """검사 하나의 예외를 실패한 검사로 바꾼다 — doctor 는 보고하지, 올리지 않는다."""
    try:
        return check()
    except GitswarmError as e:
        return Check(name, False, f"{e.kind.value}: {e.detail}")
    except Exception as e:
        return Check(name, False, type(e).__name__)


def _refused(remote: str) -> Check | None:
    """허용 목록 밖의 URL 은 git 에 넘기지 않는다 — 나머지 원격 검사도 생략."""
    try:
        validate_remote_url(remote)
    except InvalidState as e:
        return Check("remote", False, e.detail)
    return None


def _git() -> Check:
    try:
        version = Git.version()
    except RemoteError as e:
        return Check("git", False, e.detail)

    m = GIT_VERSION_RE.match(version)
    need = ".".join(map(str, GIT_MIN_VERSION))
    if m is None or tuple(map(int, m.groups())) < GIT_MIN_VERSION:
        return Check("git", False, f"git {version}; need >= {need}")
    return Check("git", True, f"git {version}")


def _home(home: Path) -> Check:
    # 실제로 파일 하나를 써 본다 — 권한 비트만으로는 읽기 전용 마운트를 못 가린다
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=home):
            pass
    except OSError as e:
        return Check("home", False, f"{home} not writable: {type(e).__name__}")
    return Check("home", True, str(home))


def _config(home: Path) -> Check:
    try:
        config = load_config(home)
        sinks_from_config(config)
    except (ValueError, TypeError, AttributeError, InvalidState) as e:
        return Check("config", False, f"{CONFIG_FILE}: {type(e).__name__}: {e}")

    missing = _missing_credentials(config)
    if missing:
        return Check("config", False, f"credential_file missing for {', '.join(missing)}")
    return Check("config", True, f"{len(config.remotes)} remote(s), {len(config.sinks)} sink(s)")


def _missing_credentials(config: Config) -> list[str]:
    missing = []
    for host, spec in config.remotes.items():
        if spec.adapter not in CREDENTIAL_ADAPTERS:
            continue
        if not spec.credential_file or not Path(spec.credential_file).expanduser().is_file():
            missing.append(host)
    return missing


def _remote(remote: str) -> Check:
    # hive 를 만들지 않는다 — 빈 임시 자리에서 URL 로 묻는다
    with tempfile.TemporaryDirectory() as tmp:
        try:
            branch = Git(Path(tmp)).default_branch(remote)
        except RemoteError as e:
            return Check("remote", False, f"{remote} unreachable: {e.detail}")

    if branch is None:
        return Check("remote", True, "reachable; HEAD names no branch — pass --base")
    return Check("remote", True, f"reachable; default branch {branch}")


def _hive(remote: str, home: Path) -> Check:
    try:
        hive = Hive.open(remote, home)
    except NotFound:
        return Check("hive", True, "absent; the first ws command creates it")
    return Check("hive", True, f"{hive.path}: {hive.worktree_count()} worktree(s)")


def _tokens(remote: str, home: Path) -> Check:
    """회수에 실패한 토큰. 읽기만 하므로 어댑터는 필요 없다. meta 를 못 읽으면 _guard 가 실패로 바꾼다."""
    try:
        hive = Hive.open(remote, home)
    except NotFound:
        return Check("tokens", True, "no hive; nothing recorded")

    records, _ = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter()).list_report(None)

    ids = unrevoked(records)
    if ids:
        detail = f"{len(ids)} unrevoked token(s) on {', '.join(ids)}"
        return Check(
            "tokens", False, f"{detail}; run `gitswarm ws drop <id>` again to retry revoke"
        )
    return Check("tokens", True, "every recorded token revoked")


def _ssh_mux(remote: str, home: Path) -> Check:
    if not is_ssh_url(remote):
        return Check("ssh_mux", True, "not an ssh remote")

    # hive 가 아직 없으면 빈 임시 자리에서 읽는다 — 홈을 감싼 레포의 설정은 bare hive 에 상속되지 않는다
    repo = hive_path(remote, home) / REPO_DIR
    if repo.is_dir():
        override = Git(repo).ssh_override()
    else:
        with tempfile.TemporaryDirectory() as tmp:
            override = Git(Path(tmp)).ssh_override()
    if override:
        return Check("ssh_mux", True, f"disabled: caller set {override}")

    problem = Git(repo).mux_problem()
    if problem:
        return Check("ssh_mux", False, f"{problem}; use a shorter GITSWARM_HOME")
    return Check("ssh_mux", True, "connections are multiplexed")
