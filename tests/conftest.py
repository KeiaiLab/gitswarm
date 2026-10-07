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
