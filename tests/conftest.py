"""네트워크 0 — 원격은 임시 bare 레포(file://). 모든 시험이 이 둘을 쓴다."""

import os
import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def ls_remote_prefix(repo_dir: Path, prefix: str) -> dict[str, str]:
    """원격에서 prefix 로 시작하는 ref → oid."""
    out = git("ls-remote", "origin", f"{prefix}*", cwd=repo_dir)
    refs = {}
    for line in filter(None, out.split("\n")):
        oid, ref = line.split("\t", 1)
        refs[ref] = oid
    return refs


NETWORK_COMMANDS = ("fetch", "push", "ls-remote")


def count_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Git._run 중 원격을 타는 호출만 기록한다. 반환 목록이 호출마다 자란다."""
    from gitswarm.driver.git import Git

    real = Git._run
    calls: list[str] = []

    def run(self, *args, **kw):
        if args[0] in NETWORK_COMMANDS:
            calls.append(args[0])
        return real(self, *args, **kw)

    monkeypatch.setattr(Git, "_run", run)
    return calls


@pytest.fixture
def remote_url(tmp_path: Path) -> str:
    """main 에 커밋 1개(README.md)가 있는 bare 원격."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "seed"
    git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("init", "-b", "main", str(work), cwd=tmp_path)
    (work / "README.md").write_text("seed\n")
    git("add", "README.md", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    git("push", "-q", str(bare), "main", cwd=work)
    return bare.as_uri()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "gitswarm-home"
    h.mkdir()
    monkeypatch.setenv("GITSWARM_HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """원격 자동 발견이 이 레포(cwd)·사용자 홈·셸 env 를 집지 않게 한다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITSWARM_REMOTE", raising=False)
    monkeypatch.setenv("GITSWARM_HOME", str(tmp_path / "default-home"))
