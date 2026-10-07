"""환경 점검. 검사는 서로 독립이다 — 하나가 실패해도 나머지를 본다.

git ─ home ─ config ─┬─ remote 없음 ──▶ 끝(원격 검사 생략)
                     └─ remote ─ hive ─ ssh_mux
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from gitswarm.adapters.select import FORGEJO, GITHUB
from gitswarm.config import CONFIG_FILE, Config, load_config
from gitswarm.driver.git import Git
from gitswarm.errors import InvalidState, NotFound, RemoteError
from gitswarm.events import sinks_from_config
from gitswarm.store.hive import REPO_DIR, Hive, hive_path

MIN_GIT = (2, 40, 0)
GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
CREDENTIAL_ADAPTERS = frozenset({FORGEJO, GITHUB})
SSH_SCHEME = "ssh://"
SCHEME_SEP = "://"
SCP_SEP = ":"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def diagnose(remote: str | None, home: Path) -> dict:
    """{ok, checks: [{name, ok, detail}]}. ok = 모든 검사 통과."""
    checks = [_git(), _home(home), _config(home)]
    if remote is None:
        checks.append(Check("remote", True, "none given or found; remote checks skipped"))
    else:
        checks += [_remote(remote), _hive(remote, home), _ssh_mux(remote, home)]
    return {"ok": all(c.ok for c in checks), "checks": [asdict(c) for c in checks]}


def _git() -> Check:
    try:
        version = Git.version()
    except RemoteError as e:
        return Check("git", False, e.detail)

    m = GIT_VERSION_RE.match(version)
    need = ".".join(map(str, MIN_GIT))
    if m is None or tuple(map(int, m.groups())) < MIN_GIT:
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


def _ssh_mux(remote: str, home: Path) -> Check:
    if not _is_ssh(remote):
        return Check("ssh_mux", True, "not an ssh remote")

    problem = Git(hive_path(remote, home) / REPO_DIR).mux_problem()
    if problem:
        return Check("ssh_mux", False, f"{problem}; use a shorter GITSWARM_HOME")
    return Check("ssh_mux", True, "connections are multiplexed")


def _is_ssh(url: str) -> bool:
    """ssh://… 또는 scp 꼴 user@host:path."""
    if url.startswith(SSH_SCHEME):
        return True
    return SCHEME_SEP not in url and SCP_SEP in url.split("/", 1)[0]
