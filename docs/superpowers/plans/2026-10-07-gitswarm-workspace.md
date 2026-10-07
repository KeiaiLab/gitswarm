# gitswarm Workspace(A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 임의 git 원격 위에서 LLM 에이전트마다 격리 workspace 를 만들고·읽고·발행하고·버리는 CLI+MCP 서비스를 실무에서 쓸 수 있는 품질로 낸다.

**Architecture:** 조율 상태는 전부 원격 git 에 둔다(`refs/heads/gitswarm/meta` 고아 브랜치 = 상태 + 이벤트 로그, `refs/heads/gitswarm/ws/<id>` = workspace 브랜치). 쓰기는 `push --force-with-lease` 하나로 CAS. 층은 surfaces → service → store → driver, 어댑터는 service 가 쓴다. 서비스는 무상태·데몬 없음.

**Tech Stack:** Python ≥3.11 · uv · hatchling · typer 0.27 · fastmcp 4.0 · httpx 0.28 · python-ulid 4 · pytest 9 · respx 0.23 · ruff 0.15 · git ≥2.40(subprocess, pygit2 없음)

**Spec:** `docs/superpowers/specs/2026-10-06-gitswarm-workspace-design.md`

## Global Constraints

- `requires-python = ">=3.11"`, 라이선스 MIT, 패키지·CLI 이름 `gitswarm`.
- git 서브프로세스는 `src/gitswarm/driver/git.py` 에서만 띄운다. 다른 모듈에 `subprocess` import 금지.
- 층 경계: surfaces→service→store→driver. 위층이 두 층 아래를 직접 부르지 않는다. adapters 는 service 만 부른다.
- ref 접두 상수 `REF_PREFIX = "gitswarm/"`. meta ref = `refs/heads/gitswarm/meta`, ws ref = `refs/heads/gitswarm/ws/<id>`.
- CAS 재시도 상수 `META_CAS_RETRIES = 5`. 기본 TTL `DEFAULT_TTL_S = 7200`, `0` = 무기한.
- 오류 enum 5종과 CLI 종료코드: NotFound 2 · Conflict 3 · InvalidState 4 · Unsupported 5 · RemoteError 6. MCP 는 `{ok:false, error:{kind, detail}}`.
- 테스트는 네트워크 0 — 원격은 임시 bare 레포 `file://`. Forgejo 는 respx mock.
- ruff `>=0.15,<0.16` 핀, `line-length = 100`, keiai-sans 와 같은 lint 선택/무시 집합.
- 자격(관리 PAT)은 파일 경로로만 받고 로그·stdout·오류 detail 에 절대 싣지 않는다.
- 커밋 메시지는 전역 CLAUDE.md 7규칙. 매 태스크 끝에 `uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q` 초록.

## Review Focus

1. `--base` 가 없는 브랜치(오타)일 때 — 기대: `NotFound`, 아무 ref 도 push 되지 않음. → Task 6 `test_create_unknown_base_is_not_found`.
2. 원격이 닿지 않을 때(URL 오타·권한 없음) — 기대: `RemoteError` 종료코드 6, 로컬에 hive 디렉터리가 남지 않음. → Task 3 `test_init_unreachable_remote_leaves_nothing`.
3. `drop` 을 두 번 하거나 원격 브랜치가 이미 지워진 뒤 drop — 기대: 멱등 성공. → Task 6 `test_drop_twice_is_idempotent`.
4. `read`/`tree` 경로에 `..`·절대경로·한글·공백 — 기대: `..`·절대경로는 `NotFound`, 한글·공백은 정상. → Task 7 `test_read_rejects_parent_traversal`, `test_read_unicode_and_space_path`.
5. 같은 호스트의 두 프로세스가 동시에 `create` — 기대: 둘 다 성공, meta 에 둘 다 기록. → Task 6 `test_concurrent_create_both_recorded`.

---

## File Structure

```
gitswarm/
  pyproject.toml  LICENSE  README.md  .gitignore
  .forgejo/workflows/build.yml
  scripts/smoke.py                      # 실제 원격 대상 수동 생명주기 점검(PEP 723)
  src/gitswarm/
    __init__.py
    constants.py                        # REF_PREFIX·META_REF·상한·기본값
    errors.py                           # ErrorKind enum · 예외 5종 · EXIT_CODES
    config.py                           # ~/.gitswarm/config.toml 로드
    events.py                           # Event · Sink(webhook·jsonl)
    driver/__init__.py  driver/git.py   # 유일한 subprocess 자리
    store/__init__.py   store/hive.py   # hive 디렉터리·bare 레포·origin
                        store/meta.py   # MetaStore: read/list/apply(CAS)/log
    adapters/__init__.py adapters/remote.py  # Protocol·Capability·Scope·Token
                         adapters/plain.py
                         adapters/forgejo.py
                         adapters/select.py  # URL host → 어댑터
    service/__init__.py service/workspace.py  # Workspace 모델 + WorkspaceService
    surfaces/__init__.py surfaces/cli.py surfaces/mcp.py
  tests/
    conftest.py                         # file:// 원격 픽스처·GITSWARM_HOME 격리
    test_errors.py test_git_driver.py test_hive.py test_meta_store.py
    test_adapters.py test_workspace_lifecycle.py test_workspace_read_publish.py
    test_cli.py test_mcp.py test_events.py test_forgejo_adapter.py
```

---

### Task 1: 프로젝트 골격과 오류 어휘

**Files:**
- Create: `pyproject.toml`, `LICENSE`, `.gitignore`, `README.md`, `src/gitswarm/__init__.py`, `src/gitswarm/constants.py`, `src/gitswarm/errors.py`, `tests/__init__.py`(빈 파일 — `from tests.conftest import git` 를 위해), `tests/conftest.py`, `tests/test_errors.py`

**Interfaces:**
- Produces: `errors.ErrorKind`(NOT_FOUND·CONFLICT·INVALID_STATE·UNSUPPORTED·REMOTE_ERROR), 예외 `NotFound·Conflict·InvalidState·Unsupported·RemoteError`(모두 `GitswarmError(kind, detail)`), `EXIT_CODES: dict[ErrorKind,int]`; `constants.REF_PREFIX·META_REF·ws_ref(id)·tracking_ref(ref)·META_CAS_RETRIES·DEFAULT_TTL_S·ULID_LEN`.
- conftest 픽스처 `remote_url`(file:// bare 레포, `main` 에 커밋 1개 `README.md`), `home`(임시 GITSWARM_HOME).

- [ ] **Step 1: pyproject·LICENSE·.gitignore 작성**

```toml
# pyproject.toml
[project]
name = "gitswarm"
version = "0.1.0"
description = "Git coordination layer for concurrent LLM agents — isolated workspaces over any git remote"
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
authors = [{ name = "KeiaiLab" }]
dependencies = [
    "typer>=0.12",
    "fastmcp>=2",
    "httpx>=0.27",
    "python-ulid>=2",
]

[project.scripts]
gitswarm = "gitswarm.surfaces.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/gitswarm"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "N", "RUF"]
ignore = ["E501", "RUF001", "RUF002", "RUF003"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["N802"]

[tool.pyrefly]
project-includes = ["src", "tests"]
python-version = "3.11"

[dependency-groups]
dev = ["ruff>=0.15,<0.16", "pytest>=9,<10", "respx>=0.21"]
```

`LICENSE` 는 `../keiai-sans/LICENSE` 를 복사(MIT, Copyright (c) 2026 KeiaiLab). `.gitignore`:

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
uv.lock
```

`README.md` 는 제목과 한 줄 설명만(Task 12 에서 채운다):

```markdown
# gitswarm

LLM 에이전트 다수를 위한 git 위 조율 계층 — 임의 git 원격 위에 격리 workspace.
```

- [ ] **Step 2: 실패 시험 작성**

```python
# tests/test_errors.py
import pytest

from gitswarm.errors import (
    EXIT_CODES,
    Conflict,
    ErrorKind,
    GitswarmError,
    InvalidState,
    NotFound,
    RemoteError,
    Unsupported,
)


@pytest.mark.parametrize(
    ("exc", "kind", "code"),
    [
        (NotFound, ErrorKind.NOT_FOUND, 2),
        (Conflict, ErrorKind.CONFLICT, 3),
        (InvalidState, ErrorKind.INVALID_STATE, 4),
        (Unsupported, ErrorKind.UNSUPPORTED, 5),
        (RemoteError, ErrorKind.REMOTE_ERROR, 6),
    ],
)
def test_error_kind_and_exit_code(exc, kind, code):
    err = exc("detail")
    assert isinstance(err, GitswarmError)
    assert err.kind is kind
    assert EXIT_CODES[err.kind] == code
    assert err.to_payload() == {"ok": False, "error": {"kind": kind.value, "detail": "detail"}}
```

- [ ] **Step 3: 실패 확인**

Run: `uv run pytest tests/test_errors.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.errors`

- [ ] **Step 4: constants·errors 구현**

```python
# src/gitswarm/__init__.py
"""gitswarm — git coordination layer for concurrent LLM agents."""

__version__ = "0.1.0"
```

```python
# src/gitswarm/constants.py
"""레포 전체가 공유하는 이름·상한. 매직 값은 여기에만 둔다."""

REF_PREFIX = "gitswarm/"
HEADS = "refs/heads/"
TRACKING = "refs/remotes/origin/"

META_REF = f"{HEADS}{REF_PREFIX}meta"
WS_DIR = "ws"  # meta 트리 안의 디렉터리: ws/<id>.json

META_CAS_RETRIES = 5
DEFAULT_TTL_S = 7200
TTL_FOREVER = 0
ULID_LEN = 26

COMMIT_AUTHOR = "gitswarm"
COMMIT_EMAIL = "gitswarm@localhost"


def ws_ref(ws_id: str) -> str:
    return f"{HEADS}{REF_PREFIX}ws/{ws_id}"


def ws_branch(ws_id: str) -> str:
    """worktree add 가 받는 짧은 브랜치 이름."""
    return f"{REF_PREFIX}ws/{ws_id}"


def tracking_ref(ref: str) -> str:
    """refs/heads/X → refs/remotes/origin/X."""
    if not ref.startswith(HEADS):
        raise ValueError(f"not a branch ref: {ref}")
    return TRACKING + ref[len(HEADS) :]


def meta_path(ws_id: str) -> str:
    return f"{WS_DIR}/{ws_id}.json"
```

```python
# src/gitswarm/errors.py
"""오류 어휘 5종. CLI 종료코드·MCP 페이로드가 같은 enum 을 쓴다."""

from enum import StrEnum


class ErrorKind(StrEnum):
    NOT_FOUND = "NotFound"
    CONFLICT = "Conflict"
    INVALID_STATE = "InvalidState"
    UNSUPPORTED = "Unsupported"
    REMOTE_ERROR = "RemoteError"


EXIT_CODES: dict[ErrorKind, int] = {
    ErrorKind.NOT_FOUND: 2,
    ErrorKind.CONFLICT: 3,
    ErrorKind.INVALID_STATE: 4,
    ErrorKind.UNSUPPORTED: 5,
    ErrorKind.REMOTE_ERROR: 6,
}


class GitswarmError(Exception):
    kind: ErrorKind

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def to_payload(self) -> dict:
        return {"ok": False, "error": {"kind": self.kind.value, "detail": self.detail}}


class NotFound(GitswarmError):
    kind = ErrorKind.NOT_FOUND


class Conflict(GitswarmError):
    kind = ErrorKind.CONFLICT


class InvalidState(GitswarmError):
    kind = ErrorKind.INVALID_STATE


class Unsupported(GitswarmError):
    kind = ErrorKind.UNSUPPORTED


class RemoteError(GitswarmError):
    kind = ErrorKind.REMOTE_ERROR
```

- [ ] **Step 5: conftest 작성**

```python
# tests/conftest.py
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
        ["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True,
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
```

- [ ] **Step 6: 통과 확인**

Run: `uv sync --dev && uv run pytest tests/test_errors.py -q`
Expected: 5 passed

- [ ] **Step 7: 커밋**

```bash
git add pyproject.toml LICENSE .gitignore README.md src tests
git commit -m "Add project skeleton and error vocabulary"
```

---

### Task 2: git 드라이버

**Files:**
- Create: `src/gitswarm/driver/__init__.py`, `src/gitswarm/driver/git.py`, `tests/test_git_driver.py`

**Interfaces:**
- Produces `driver.git.Git(repo: Path)` 메서드:
  - `init_bare(path) -> Git` (classmethod) · `set_origin(url)` · `origin_url() -> str`
  - `ls_remote(ref) -> str | None`
  - `fetch(ref) -> str | None` (origin 의 ref 를 `tracking_ref(ref)` 로 강제 갱신, 없으면 None)
  - `push(oid, ref, expected: str | None) -> bool` (lease. 거절 False, 그 외 실패 RemoteError)
  - `delete_remote(ref) -> None` (이미 없으면 성공)
  - `rev_parse(rev) -> str | None` · `exists(rev) -> bool`
  - `hash_object(data: bytes) -> str` · `cat_file(rev) -> bytes`
  - `ls_tree(tree_ish) -> list[TreeEntry]` (한 단계) · `ls_tree_recursive(tree_ish) -> dict[str, str]` (path→blob oid)
  - `build_tree(files: dict[str, str]) -> str` (path→blob oid, 중첩 디렉터리 처리)
  - `commit_tree(tree, parents: list[str], message) -> str`
  - `log(ref, since: str | None) -> list[LogEntry(oid, subject, committed_at)]` 최신순
  - `update_ref(ref, oid)` · `delete_ref(ref)`
  - `worktree_add(path, branch)` · `worktree_remove(path)`
  - `TreeEntry(mode, kind, oid, name)`
- git 실패는 `RemoteError(stderr)`.

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_git_driver.py
from pathlib import Path

import pytest

from gitswarm.constants import tracking_ref
from gitswarm.driver.git import Git
from gitswarm.errors import RemoteError


@pytest.fixture
def repo(tmp_path: Path, remote_url: str) -> Git:
    g = Git.init_bare(tmp_path / "hive.git")
    g.set_origin(remote_url)
    return g


def test_fetch_main_and_missing_ref(repo: Git):
    oid = repo.fetch("refs/heads/main")
    assert oid and len(oid) == 40
    assert repo.rev_parse(tracking_ref("refs/heads/main")) == oid
    assert repo.fetch("refs/heads/nope") is None


def test_ls_remote(repo: Git):
    assert repo.ls_remote("refs/heads/main") == repo.fetch("refs/heads/main")
    assert repo.ls_remote("refs/heads/nope") is None


def test_blob_tree_commit_roundtrip(repo: Git):
    a = repo.hash_object(b"A")
    b = repo.hash_object(b"B")
    tree = repo.build_tree({"ws/x.json": a, "ws/y.json": b, "top.txt": a})
    assert repo.ls_tree_recursive(tree) == {"ws/x.json": a, "ws/y.json": b, "top.txt": a}
    names = sorted(e.name for e in repo.ls_tree(tree))
    assert names == ["top.txt", "ws"]
    c1 = repo.commit_tree(tree, [], "ws.created x")
    c2 = repo.commit_tree(tree, [c1], "ws.created y")
    assert repo.cat_file(f"{c2}:ws/x.json") == b"A"
    log = repo.log(c2, since=None)
    assert [e.subject for e in log] == ["ws.created y", "ws.created x"]
    assert [e.oid for e in repo.log(c2, since=c1)] == [c2]


def test_push_lease_create_then_reject(repo: Git):
    base = repo.fetch("refs/heads/main")
    assert repo.push(base, "refs/heads/gitswarm/meta", expected=None) is True
    # 두 번째 "없어야 한다" lease 는 거절된다
    assert repo.push(base, "refs/heads/gitswarm/meta", expected=None) is False
    # 맞는 expected 로는 된다
    assert repo.push(base, "refs/heads/gitswarm/meta", expected=base) is True
    assert repo.rev_parse(tracking_ref("refs/heads/gitswarm/meta")) == base


def test_delete_remote_is_idempotent(repo: Git):
    base = repo.fetch("refs/heads/main")
    repo.push(base, "refs/heads/gitswarm/ws/a", expected=None)
    repo.delete_remote("refs/heads/gitswarm/ws/a")
    repo.delete_remote("refs/heads/gitswarm/ws/a")
    assert repo.ls_remote("refs/heads/gitswarm/ws/a") is None


def test_worktree_add_remove(repo: Git, tmp_path: Path):
    base = repo.fetch("refs/heads/main")
    repo.update_ref("refs/heads/gitswarm/ws/w", base)
    wt = tmp_path / "wt"
    repo.worktree_add(wt, "gitswarm/ws/w")
    assert (wt / "README.md").read_text() == "seed\n"
    repo.worktree_remove(wt)
    assert not wt.exists()
    repo.delete_ref("refs/heads/gitswarm/ws/w")
    assert repo.exists("refs/heads/gitswarm/ws/w") is False


def test_git_failure_is_remote_error(tmp_path: Path):
    g = Git.init_bare(tmp_path / "x.git")
    g.set_origin((tmp_path / "missing.git").as_uri())
    with pytest.raises(RemoteError):
        g.fetch("refs/heads/main")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_git_driver.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.driver`

- [ ] **Step 3: 드라이버 구현**

```python
# src/gitswarm/driver/__init__.py
```

```python
# src/gitswarm/driver/git.py
"""git plumbing 의 유일한 자리. 위층은 oid·ref·bytes 만 다룬다."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gitswarm.constants import COMMIT_AUTHOR, COMMIT_EMAIL, tracking_ref
from gitswarm.errors import RemoteError

RC_LS_REMOTE_MISSING = 2
REJECTED_MARKERS = ("[rejected]", "stale info", "failed to push some refs")
MISSING_REMOTE_REF_MARKERS = ("couldn't find remote ref", "remote ref does not exist")
BLOB_MODE = "100644"
TREE_MODE = "040000"
NUL = "\x00"


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str  # blob | tree
    oid: str
    name: str


@dataclass(frozen=True)
class LogEntry:
    oid: str
    subject: str
    committed_at: str


@dataclass(frozen=True)
class Git:
    repo: Path

    # ── 프로세스 ──────────────────────────────────────────────
    def _run(self, *args: str, data: bytes | None = None, ok_rc: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": COMMIT_AUTHOR,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_NAME": COMMIT_AUTHOR,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
        }
        p = subprocess.run(["git", "-C", str(self.repo), *args], input=data, env=env, capture_output=True)
        if p.returncode not in ok_rc:
            raise RemoteError(p.stderr.decode(errors="replace").strip() or f"git {args[0]} rc={p.returncode}")
        return p

    def _out(self, *args: str, data: bytes | None = None) -> str:
        return self._run(*args, data=data).stdout.decode().strip()

    # ── 레포·원격 ─────────────────────────────────────────────
    @classmethod
    def init_bare(cls, path: Path) -> Git:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True, capture_output=True)
        return cls(path)

    def set_origin(self, url: str) -> None:
        self._run("remote", "add", "origin", url)

    def origin_url(self) -> str:
        return self._out("remote", "get-url", "origin")

    def ls_remote(self, ref: str) -> str | None:
        p = self._run("ls-remote", "--exit-code", "origin", ref, ok_rc=(0, RC_LS_REMOTE_MISSING))
        if p.returncode == RC_LS_REMOTE_MISSING:
            return None
        return p.stdout.decode().split()[0]

    def fetch(self, ref: str) -> str | None:
        """origin/<ref> 를 tracking ref 로 강제 갱신. 원격에 없으면 tracking 도 지우고 None."""
        if self.ls_remote(ref) is None:
            self._run("update-ref", "-d", tracking_ref(ref))
            return None
        self._run("fetch", "-q", "origin", f"+{ref}:{tracking_ref(ref)}")
        return self.rev_parse(tracking_ref(ref))

    def push(self, oid: str, ref: str, expected: str | None) -> bool:
        """lease push. expected=None 은 "원격에 그 ref 가 없어야 한다"."""
        lease = f"--force-with-lease={ref}:{expected or ''}"
        p = self._run("push", "-q", "origin", lease, f"{oid}:{ref}", ok_rc=(0, 1))
        if p.returncode == 0:
            return True
        err = p.stderr.decode(errors="replace")
        if any(m in err for m in REJECTED_MARKERS):
            return False
        raise RemoteError(err.strip())

    def delete_remote(self, ref: str) -> None:
        p = self._run("push", "-q", "origin", "--delete", ref, ok_rc=(0, 1))
        if p.returncode == 0:
            self._run("update-ref", "-d", tracking_ref(ref))
            return
        err = p.stderr.decode(errors="replace")
        if any(m in err for m in MISSING_REMOTE_REF_MARKERS):
            return
        raise RemoteError(err.strip())

    # ── 객체 ──────────────────────────────────────────────────
    def rev_parse(self, rev: str) -> str | None:
        p = self._run("rev-parse", "--verify", "-q", f"{rev}^{{object}}", ok_rc=(0, 1))
        if p.returncode != 0:
            return None
        return p.stdout.decode().strip()

    def exists(self, rev: str) -> bool:
        return self._run("cat-file", "-e", rev, ok_rc=(0, 1, 128)).returncode == 0

    def hash_object(self, data: bytes) -> str:
        return self._out("hash-object", "-w", "--stdin", data=data)

    def cat_file(self, rev: str) -> bytes:
        return self._run("cat-file", "blob", rev).stdout

    def ls_tree(self, tree_ish: str) -> list[TreeEntry]:
        out = self._run("ls-tree", "-z", tree_ish).stdout.decode()
        entries = []
        for line in filter(None, out.split(NUL)):
            meta, name = line.split("\t", 1)
            mode, kind, oid = meta.split()
            entries.append(TreeEntry(mode, kind, oid, name))
        return entries

    def ls_tree_recursive(self, tree_ish: str) -> dict[str, str]:
        out = self._run("ls-tree", "-r", "-z", tree_ish).stdout.decode()
        files: dict[str, str] = {}
        for line in filter(None, out.split(NUL)):
            meta, path = line.split("\t", 1)
            files[path] = meta.split()[2]
        return files

    def build_tree(self, files: dict[str, str]) -> str:
        """path→blob oid 평면 사전에서 중첩 트리를 짓는다. 빈 사전도 빈 트리가 된다."""
        children: dict[str, dict[str, str]] = {}
        lines: list[str] = []

        # 최상위 블롭과 하위 디렉터리로 가른다
        for path, oid in files.items():
            head, sep, rest = path.partition("/")
            if not sep:
                lines.append(f"{BLOB_MODE} blob {oid}\t{head}")
                continue
            children.setdefault(head, {})[rest] = oid

        for name, sub in children.items():
            lines.append(f"{TREE_MODE} tree {self.build_tree(sub)}\t{name}")

        data = "".join(f"{line}\n" for line in lines).encode()
        return self._out("mktree", data=data)

    def commit_tree(self, tree: str, parents: list[str], message: str) -> str:
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", p]
        args += ["-m", message]
        return self._out(*args)

    def log(self, ref: str, since: str | None) -> list[LogEntry]:
        rng = f"{since}..{ref}" if since else ref
        out = self._run("log", f"--format=%H{NUL}%s{NUL}%cI", rng).stdout.decode()
        entries = []
        for line in filter(None, out.splitlines()):
            oid, subject, at = line.split(NUL)
            entries.append(LogEntry(oid, subject, at))
        return entries

    # ── ref·worktree ─────────────────────────────────────────
    def update_ref(self, ref: str, oid: str) -> None:
        self._run("update-ref", ref, oid)

    def delete_ref(self, ref: str) -> None:
        self._run("update-ref", "-d", ref)

    def worktree_add(self, path: Path, branch: str) -> None:
        self._run("worktree", "add", "-q", str(path), branch)

    def worktree_remove(self, path: Path) -> None:
        self._run("worktree", "remove", "--force", str(path))
        self._run("worktree", "prune")
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_git_driver.py -q`
Expected: 7 passed

- [ ] **Step 5: 커밋**

```bash
git add src/gitswarm/driver tests/test_git_driver.py
git commit -m "Add git plumbing driver"
```

---

### Task 3: Hive — 원격 하나당 로컬 bare 레포

**Files:**
- Create: `src/gitswarm/store/__init__.py`, `src/gitswarm/store/hive.py`, `tests/test_hive.py`

**Interfaces:**
- Produces `store.hive.Hive(path: Path, url: str, git: Git)`:
  - `Hive.init(url, home: Path) -> Hive` (멱등. 원격 `HEAD` ls-remote 실패면 RemoteError, 디렉터리 안 남김). origin 에는 **받은 URL 그대로**(끝 `/` 만 제거) 쓴다 — `.git` 을 떼면 `file://` 경로가 깨진다. `normalize_url` 은 hive id 산출에만 쓴다.
  - `Hive.open(url, home) -> Hive` (없으면 NotFound)
  - `hive.worktree_dir(ws_id) -> Path` (`<hive>/wt/<id>`)
  - `normalize_url(url) -> str`, `hive_id(url) -> str`(sha256 16자), `resolve_home() -> Path`(`$GITSWARM_HOME` 또는 `~/.gitswarm`)

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_hive.py
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
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_hive.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.store`

- [ ] **Step 3: 구현**

```python
# src/gitswarm/store/__init__.py
```

```python
# src/gitswarm/store/hive.py
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
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_hive.py -q`
Expected: 5 passed

- [ ] **Step 5: 커밋**

```bash
git add src/gitswarm/store tests/test_hive.py
git commit -m "Add hive: per-remote local bare mirror"
```

---

### Task 4: MetaStore — git-native 상태와 CAS

**Files:**
- Create: `src/gitswarm/store/meta.py`, `tests/test_meta_store.py`

**Interfaces:**
- Consumes `Git`(Task 2), `constants.META_REF·META_CAS_RETRIES·tracking_ref`.
- Produces `store.meta.MetaStore(git: Git)`:
  - `Change(path: str, content: bytes, subject: str)`
  - `tip() -> str | None` (fetch 뒤 tracking oid)
  - `read(path) -> bytes | None` · `list(prefix) -> list[str]` (둘 다 fetch 먼저)
  - `apply(change) -> str` (새 meta 커밋 oid. 상한 초과 Conflict)
  - `log(since) -> list[LogEntry]` 최신순, `read_at(oid, path) -> bytes | None`

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_meta_store.py
import multiprocessing as mp
from pathlib import Path

import pytest

from gitswarm.errors import Conflict
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore


@pytest.fixture
def store(remote_url: str, home: Path) -> MetaStore:
    return MetaStore(Hive.init(remote_url, home).git)


def test_empty_store(store: MetaStore):
    assert store.tip() is None
    assert store.read("ws/x.json") is None
    assert store.list("ws/") == []
    assert store.log(since=None) == []


def test_apply_creates_orphan_then_appends(store: MetaStore):
    c1 = store.apply(Change("ws/a.json", b"{}", "ws.created a"))
    c2 = store.apply(Change("ws/b.json", b"{}", "ws.created b"))
    assert store.tip() == c2
    assert store.list("ws/") == ["ws/a.json", "ws/b.json"]
    assert [e.subject for e in store.log(since=None)] == ["ws.created b", "ws.created a"]
    assert [e.oid for e in store.log(since=c1)] == [c2]
    c3 = store.apply(Change("ws/a.json", b'{"state":"dropped"}', "ws.dropped a"))
    assert store.read("ws/a.json") == b'{"state":"dropped"}'
    assert store.read_at(c1, "ws/a.json") == b"{}"
    assert store.read_at(c3, "ws/b.json") == b"{}"


def _writer(remote_url: str, home: str, name: str) -> None:
    hive = Hive.open(remote_url, Path(home))
    MetaStore(hive.git).apply(Change(f"ws/{name}.json", name.encode(), f"ws.created {name}"))


def test_concurrent_writers_both_land(remote_url: str, home: Path):
    Hive.init(remote_url, home)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(remote_url, str(home), n)) for n in ("p", "q", "r")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    store = MetaStore(Hive.open(remote_url, home).git)
    assert store.list("ws/") == ["ws/p.json", "ws/q.json", "ws/r.json"]


def test_cas_exhaustion_is_conflict(store: MetaStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(type(store.git), "push", lambda self, oid, ref, expected: False)
    with pytest.raises(Conflict):
        store.apply(Change("ws/z.json", b"{}", "ws.created z"))
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_meta_store.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.store.meta`

- [ ] **Step 3: 구현**

```python
# src/gitswarm/store/meta.py
"""meta 브랜치 = 상태(ws/<id>.json 트리) + 이벤트 로그(커밋 이력).

쓰기는 원격 push 의 lease 하나로 CAS 한다. 로컬 브랜치는 두지 않는다.

    fetch ─▶ old ─▶ tree' ─▶ commit(new, parent=old) ─▶ push new:meta lease=old
                                                           │ 거절
                                                           ▼ 다시 fetch (≤ META_CAS_RETRIES)
"""

from __future__ import annotations

from dataclasses import dataclass

from gitswarm.constants import META_CAS_RETRIES, META_REF, tracking_ref
from gitswarm.driver.git import Git, LogEntry
from gitswarm.errors import Conflict


@dataclass(frozen=True)
class Change:
    path: str
    content: bytes
    subject: str


@dataclass(frozen=True)
class MetaStore:
    git: Git

    def tip(self) -> str | None:
        return self.git.fetch(META_REF)

    def read(self, path: str) -> bytes | None:
        tip = self.tip()
        if tip is None:
            return None
        return self.read_at(tip, path)

    def read_at(self, oid: str, path: str) -> bytes | None:
        rev = f"{oid}:{path}"
        if not self.git.exists(rev):
            return None
        return self.git.cat_file(rev)

    def list(self, prefix: str) -> list[str]:
        tip = self.tip()
        if tip is None:
            return []
        files = self.git.ls_tree_recursive(tip)
        return sorted(p for p in files if p.startswith(prefix))

    def log(self, since: str | None) -> list[LogEntry]:
        tip = self.tip()
        if tip is None:
            return []
        return self.git.log(tracking_ref(META_REF), since)

    def apply(self, change: Change) -> str:
        blob = self.git.hash_object(change.content)

        for _ in range(META_CAS_RETRIES):
            old = self.tip()
            files = self.git.ls_tree_recursive(old) if old else {}
            files[change.path] = blob
            tree = self.git.build_tree(files)
            new = self.git.commit_tree(tree, [old] if old else [], change.subject)
            if self.git.push(new, META_REF, expected=old):
                return new

        raise Conflict(f"meta CAS failed after {META_CAS_RETRIES} attempts: {change.subject}")
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_meta_store.py -q`
Expected: 4 passed (경합 시험은 spawn 3 프로세스, 수 초)

- [ ] **Step 5: 커밋**

```bash
git add src/gitswarm/store/meta.py tests/test_meta_store.py
git commit -m "Add MetaStore with lease-based CAS"
```

---

### Task 5: 어댑터 계약·plain 어댑터·설정

**Files:**
- Create: `src/gitswarm/adapters/__init__.py`, `src/gitswarm/adapters/remote.py`, `src/gitswarm/adapters/plain.py`, `src/gitswarm/adapters/select.py`, `src/gitswarm/config.py`, `tests/test_adapters.py`

**Interfaces:**
- Produces `adapters.remote`: `Capability(StrEnum) TOKEN·EVENTS·PR`, `Scope(StrEnum) READ·WRITE`, `Token(id: str, secret: str, scope: Scope)`, `RemoteAdapter(Protocol)`: `capabilities() -> frozenset[Capability]`, `issue_token(repo: str, ws_id: str, scope: Scope) -> Token`, `revoke_token(token_id: str) -> None`.
- `adapters.plain.PlainAdapter()` — capabilities 빈 집합, 나머지 `Unsupported`.
- `config.Config(remotes: dict[str, RemoteSpec], sinks: list[SinkSpec])`, `RemoteSpec(adapter: str, api: str, user: str, credential_file: str)`, `SinkSpec(kind: str, target: str)`, `load_config(home) -> Config` (파일 없으면 빈 Config).
- `adapters.select.adapter_for(url, config) -> RemoteAdapter` — host 가 `config.remotes` 에 있고 `adapter == "forgejo"` 면 Task 11 의 ForgejoAdapter, 아니면 PlainAdapter. `repo_name(url) -> str`(`org/repo`), `host_of(url) -> str`.

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_adapters.py
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.adapters.remote import Capability, Scope
from gitswarm.adapters.select import adapter_for, host_of, repo_name
from gitswarm.config import Config, RemoteSpec, SinkSpec, load_config
from gitswarm.errors import Unsupported


def test_plain_adapter_declares_nothing():
    a = PlainAdapter()
    assert a.capabilities() == frozenset()
    with pytest.raises(Unsupported):
        a.issue_token("o/r", "01J", Scope.WRITE)
    with pytest.raises(Unsupported):
        a.revoke_token("1")
    assert Capability.TOKEN not in a.capabilities()


@pytest.mark.parametrize(
    ("url", "host", "name"),
    [
        ("ssh://git@git.example.com/org/repo.git", "git.example.com", "org/repo"),
        ("https://git.example.com/org/repo", "git.example.com", "org/repo"),
        ("git@git.example.com:org/repo.git", "git.example.com", "org/repo"),
        ("file:///tmp/x/remote.git", "", "x/remote"),
    ],
)
def test_host_and_repo_name(url, host, name):
    assert host_of(url) == host
    assert repo_name(url) == name


def test_load_config_missing_and_present(home: Path):
    assert load_config(home) == Config(remotes={}, sinks=[])
    (home / "config.toml").write_text(
        '[remote."git.example.com"]\n'
        'adapter = "forgejo"\napi = "https://git.example.com"\nuser = "bot"\n'
        'credential_file = "/tmp/cred"\n\n'
        '[[sink]]\nkind = "jsonl"\ntarget = "/tmp/events.jsonl"\n'
    )
    cfg = load_config(home)
    assert cfg.remotes["git.example.com"] == RemoteSpec("forgejo", "https://git.example.com", "bot", "/tmp/cred")
    assert cfg.sinks == [SinkSpec("jsonl", "/tmp/events.jsonl")]


def test_adapter_for_defaults_to_plain(home: Path):
    cfg = load_config(home)
    assert isinstance(adapter_for("ssh://git@h/o/r.git", cfg), PlainAdapter)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_adapters.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.adapters`

- [ ] **Step 3: 구현**

```python
# src/gitswarm/adapters/__init__.py
```

```python
# src/gitswarm/adapters/remote.py
"""원격 호스팅이 git 프로토콜 밖에서 주는 기능의 계약. 없으면 Unsupported 를 '명시'한다."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Capability(StrEnum):
    TOKEN = "token"
    EVENTS = "events"
    PR = "pr"


class Scope(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Token:
    id: str
    secret: str
    scope: Scope


class RemoteAdapter(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token: ...

    def revoke_token(self, token_id: str) -> None: ...
```

```python
# src/gitswarm/adapters/plain.py
"""임의 git URL. 호스팅 API 가 없으므로 전부 Unsupported."""

from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.errors import Unsupported


class PlainAdapter:
    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token:
        raise Unsupported("plain remote cannot issue tokens")

    def revoke_token(self, token_id: str) -> None:
        raise Unsupported("plain remote cannot revoke tokens")
```

```python
# src/gitswarm/config.py
"""~/.gitswarm/config.toml — 어댑터와 이벤트 sink 선언. 없으면 전부 기본."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "config.toml"


@dataclass(frozen=True)
class RemoteSpec:
    adapter: str
    api: str = ""
    user: str = ""
    credential_file: str = ""


@dataclass(frozen=True)
class SinkSpec:
    kind: str  # webhook | jsonl
    target: str  # url | path


@dataclass(frozen=True)
class Config:
    remotes: dict[str, RemoteSpec] = field(default_factory=dict)
    sinks: list[SinkSpec] = field(default_factory=list)


def load_config(home: Path) -> Config:
    path = home / CONFIG_FILE
    if not path.exists():
        return Config()

    raw = tomllib.loads(path.read_text())
    remotes = {host: RemoteSpec(**spec) for host, spec in raw.get("remote", {}).items()}
    sinks = [SinkSpec(**s) for s in raw.get("sink", [])]
    return Config(remotes=remotes, sinks=sinks)
```

```python
# src/gitswarm/adapters/select.py
"""URL 의 host 로 어댑터를 고른다. 미선언 = plain."""

from __future__ import annotations

from urllib.parse import urlparse

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.adapters.remote import RemoteAdapter
from gitswarm.config import Config

FORGEJO = "forgejo"
SCP_SEP = ":"


def _split(url: str) -> tuple[str, str]:
    """(host, path). scp 꼴 git@host:org/repo 도 받는다."""
    if "://" in url:
        u = urlparse(url)
        return u.hostname or "", u.path
    user_host, sep, path = url.partition(SCP_SEP)
    if not sep:
        return "", url
    return user_host.rpartition("@")[2], path


def host_of(url: str) -> str:
    return _split(url)[0]


def repo_name(url: str) -> str:
    path = _split(url)[1].strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return "/".join(path.split("/")[-2:])


def adapter_for(url: str, config: Config) -> RemoteAdapter:
    spec = config.remotes.get(host_of(url))
    if spec is None or spec.adapter != FORGEJO:
        return PlainAdapter()

    from gitswarm.adapters.forgejo import ForgejoAdapter  # Task 11

    return ForgejoAdapter.from_spec(spec)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_adapters.py -q`
Expected: 7 passed

- [ ] **Step 5: 커밋**

```bash
git add src/gitswarm/adapters src/gitswarm/config.py tests/test_adapters.py
git commit -m "Add remote adapter contract, plain adapter, config"
```

---

### Task 6: WorkspaceService — 모델·create·get·list·drop

**Files:**
- Create: `src/gitswarm/service/__init__.py`, `src/gitswarm/service/workspace.py`, `tests/test_workspace_lifecycle.py`

**Interfaces:**
- Consumes `Hive`, `MetaStore·Change`, `RemoteAdapter·Capability·Scope`, `constants`.
- Produces:
  - `WsState(StrEnum) OPEN·PUBLISHED·DROPPED`, `Checkout(StrEnum) NONE·WORKTREE`
  - `Workspace` dataclass(id, state, base_ref, base_oid, branch, agent: dict, parent, created_at, ttl_s, labels) + `to_json() -> bytes`, `from_json(bytes)`, `to_dict()`.
  - `CreateResult(id, branch, base_oid, path: str | None, token: str | None)`
  - `WorkspaceService(hive, store, adapter, sinks: list[Sink]=[], clock=utcnow)`:
    - `create(base_ref, agent: dict, ttl_s, from_ws, checkout: Checkout, labels) -> CreateResult`
    - `get(id) -> Workspace` · `list(state: WsState | None) -> list[Workspace]` · `drop(id) -> Workspace`
  - 서비스 생성 도우미 `open_service(remote_url, home) -> WorkspaceService` (Hive.open + config + adapter + sinks).
- Sink 는 Task 10 에서 정의하지만 service 는 지금부터 `self._emit(kind, ws)` 를 호출한다 — 이 태스크에서는 `events.py` 에 `Event`·`Sink` Protocol 최소 정의를 함께 만든다.

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_workspace_lifecycle.py
import multiprocessing as mp
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.constants import ULID_LEN, tracking_ref, ws_ref
from gitswarm.errors import InvalidState, NotFound
from gitswarm.service.workspace import Checkout, WorkspaceService, WsState, open_service
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore


@pytest.fixture
def svc(remote_url: str, home: Path) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())


def test_create_records_meta_and_pushes_branch(svc: WorkspaceService):
    r = svc.create("main", {"name": "impl"}, ttl_s=10, from_ws=None, checkout=Checkout.NONE, labels={})
    assert len(r.id) == ULID_LEN
    assert r.branch == ws_ref(r.id)
    assert r.path is None and r.token is None
    assert svc.hive.git.ls_remote(r.branch) == r.base_oid
    ws = svc.get(r.id)
    assert ws.state is WsState.OPEN and ws.base_ref == "refs/heads/main"
    assert ws.agent == {"name": "impl"} and ws.ttl_s == 10


def test_create_with_checkout_gives_worktree(svc: WorkspaceService):
    r = svc.create("refs/heads/main", {}, ttl_s=0, from_ws=None, checkout=Checkout.WORKTREE, labels={})
    assert r.path and (Path(r.path) / "README.md").read_text() == "seed\n"


def test_create_from_ws_records_parent(svc: WorkspaceService):
    a = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    b = svc.create("main", {}, ttl_s=0, from_ws=a.id, checkout=Checkout.NONE, labels={"k": "v"})
    ws = svc.get(b.id)
    assert ws.parent == a.id and ws.base_oid == a.base_oid and ws.labels == {"k": "v"}


def test_create_unknown_base_is_not_found(svc: WorkspaceService):
    with pytest.raises(NotFound):
        svc.create("nope", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    assert svc.list(None) == []


def test_list_filters_by_state(svc: WorkspaceService):
    a = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    b = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    svc.drop(a.id)
    assert [w.id for w in svc.list(WsState.OPEN)] == [b.id]
    assert {w.id for w in svc.list(None)} == {a.id, b.id}


def test_drop_twice_is_idempotent(svc: WorkspaceService):
    r = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.WORKTREE, labels={})
    svc.hive.git.delete_remote(r.branch)  # 다른 쪽이 먼저 지운 상황
    ws = svc.drop(r.id)
    assert ws.state is WsState.DROPPED
    assert svc.drop(r.id).state is WsState.DROPPED
    assert not Path(r.path).exists()
    assert svc.hive.git.exists(tracking_ref(r.branch)) is False


def test_get_unknown_is_not_found(svc: WorkspaceService):
    with pytest.raises(NotFound):
        svc.get("01J00000000000000000000000")


def _creator(remote_url: str, home: str) -> None:
    open_service(remote_url, Path(home)).create("main", {}, 0, None, Checkout.NONE, {})


def test_concurrent_create_both_recorded(remote_url: str, home: Path):
    Hive.init(remote_url, home)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_creator, args=(remote_url, str(home))) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    assert len(open_service(remote_url, home).list(WsState.OPEN)) == 3


def test_invalid_transition(svc: WorkspaceService):
    r = svc.create("main", {}, ttl_s=0, from_ws=None, checkout=Checkout.NONE, labels={})
    svc.drop(r.id)
    with pytest.raises(InvalidState):
        svc.publish(r.id)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_workspace_lifecycle.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.service`

- [ ] **Step 3: events 최소 정의**

```python
# src/gitswarm/events.py
"""이벤트 = meta 커밋 1개. sink 는 발행 시점 1회 전송(재시도 없음, 되감기는 tail --since)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class Event:
    kind: str
    id: str
    oid: str
    at: str
    payload: dict

    def to_dict(self) -> dict:
        return asdict(self)


class Sink(Protocol):
    def emit(self, event: Event) -> None: ...
```

- [ ] **Step 4: 서비스 구현**

```python
# src/gitswarm/service/__init__.py
```

```python
# src/gitswarm/service/workspace.py
"""Workspace 생명주기. 상태는 MetaStore(원격 git)에만 있다.

    create ──▶ open ──publish──▶ published ──drop──▶ dropped
                 └──────────────drop────────────────▶ dropped
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable

from ulid import ULID

from gitswarm.adapters.remote import Capability, RemoteAdapter, Scope
from gitswarm.adapters.select import adapter_for, repo_name
from gitswarm.config import load_config
from gitswarm.constants import HEADS, TTL_FOREVER, ULID_LEN, meta_path, tracking_ref, ws_branch, ws_ref
from gitswarm.errors import InvalidState, NotFound
from gitswarm.events import Event, Sink
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore

EV_CREATED = "ws.created"
EV_PUBLISHED = "ws.published"
EV_DROPPED = "ws.dropped"
EV_EXPIRED = "ws.expired"


class WsState(StrEnum):
    OPEN = "open"
    PUBLISHED = "published"
    DROPPED = "dropped"


class Checkout(StrEnum):
    NONE = "none"
    WORKTREE = "worktree"


# 허용 전이. 같은 상태로의 재전이는 허용(멱등).
TRANSITIONS: dict[WsState, frozenset[WsState]] = {
    WsState.OPEN: frozenset({WsState.OPEN, WsState.PUBLISHED, WsState.DROPPED}),
    WsState.PUBLISHED: frozenset({WsState.PUBLISHED, WsState.DROPPED}),
    WsState.DROPPED: frozenset({WsState.DROPPED}),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Workspace:
    id: str
    state: WsState
    base_ref: str
    base_oid: str
    branch: str
    agent: dict = field(default_factory=dict)
    parent: str | None = None
    created_at: str = ""
    ttl_s: int = TTL_FOREVER
    labels: dict = field(default_factory=dict)
    token_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    def to_json(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode()

    @classmethod
    def from_json(cls, data: bytes) -> Workspace:
        d = json.loads(data)
        d["state"] = WsState(d["state"])
        return cls(**d)

    def with_state(self, state: WsState) -> Workspace:
        if state not in TRANSITIONS[self.state]:
            raise InvalidState(f"{self.id}: {self.state.value} → {state.value} not allowed")
        return Workspace(**{**asdict(self), "state": state})


@dataclass(frozen=True)
class CreateResult:
    id: str
    branch: str
    base_oid: str
    path: str | None
    token: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _full_ref(ref: str) -> str:
    return ref if ref.startswith("refs/") else HEADS + ref


class WorkspaceService:
    def __init__(
        self,
        hive: Hive,
        store: MetaStore,
        adapter: RemoteAdapter,
        sinks: list[Sink] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.hive = hive
        self.store = store
        self.adapter = adapter
        self.sinks = sinks or []
        self.clock = clock

    # ── 조회 ──────────────────────────────────────────────────
    def get(self, ws_id: str) -> Workspace:
        if len(ws_id) != ULID_LEN:
            raise NotFound(f"invalid workspace id: {ws_id}")
        data = self.store.read(meta_path(ws_id))
        if data is None:
            raise NotFound(f"workspace {ws_id} not found")
        return Workspace.from_json(data)

    def list(self, state: WsState | None) -> list[Workspace]:
        tip = self.store.tip()
        if tip is None:
            return []
        out = []
        for path in self.store.list("ws/"):
            ws = Workspace.from_json(self.store.read_at(tip, path) or b"{}")
            if state is None or ws.state is state:
                out.append(ws)
        return out

    # ── 생성 ──────────────────────────────────────────────────
    def create(
        self,
        base_ref: str,
        agent: dict,
        ttl_s: int,
        from_ws: str | None,
        checkout: Checkout,
        labels: dict,
    ) -> CreateResult:
        parent = self.get(from_ws) if from_ws else None
        src_ref = parent.branch if parent else _full_ref(base_ref)
        base_oid = self.hive.git.fetch(src_ref)
        if base_oid is None:
            raise NotFound(f"base ref {src_ref} not on remote")

        ws_id = str(ULID())
        branch = ws_ref(ws_id)
        # 원격 브랜치가 먼저다 — 실패하면 meta 에 아무것도 남지 않는다
        if not self.hive.git.push(base_oid, branch, expected=None):
            raise InvalidState(f"branch {branch} already exists on remote")
        self.hive.git.update_ref(branch, base_oid)

        token = self._issue_token(ws_id)
        ws = Workspace(
            id=ws_id, state=WsState.OPEN, base_ref=_full_ref(base_ref) if not parent else parent.branch,
            base_oid=base_oid, branch=branch, agent=agent, parent=parent.id if parent else None,
            created_at=_iso(self.clock()), ttl_s=ttl_s, labels=labels,
            token_id=token[0] if token else None,
        )
        self._write(ws, EV_CREATED)

        path = None
        if checkout is Checkout.WORKTREE:
            wt = self.hive.worktree_dir(ws_id)
            self.hive.git.worktree_add(wt, ws_branch(ws_id))
            path = str(wt)
        return CreateResult(ws_id, branch, base_oid, path, token[1] if token else None)

    def _issue_token(self, ws_id: str) -> tuple[str, str] | None:
        if Capability.TOKEN not in self.adapter.capabilities():
            return None
        t = self.adapter.issue_token(repo_name(self.hive.url), ws_id, Scope.WRITE)
        return (t.id, t.secret)

    # ── 폐기 ──────────────────────────────────────────────────
    def drop(self, ws_id: str) -> Workspace:
        ws = self.get(ws_id)
        if ws.state is WsState.DROPPED:
            return ws

        wt = self.hive.worktree_dir(ws_id)
        if wt.exists():
            self.hive.git.worktree_remove(wt)
        self.hive.git.delete_remote(ws.branch)
        if self.hive.git.exists(ws.branch):
            self.hive.git.delete_ref(ws.branch)
        if ws.token_id and Capability.TOKEN in self.adapter.capabilities():
            self.adapter.revoke_token(ws.token_id)

        dropped = ws.with_state(WsState.DROPPED)
        self._write(dropped, EV_DROPPED)
        return dropped

    # ── 공통 ──────────────────────────────────────────────────
    def _write(self, ws: Workspace, kind: str) -> str:
        oid = self.store.apply(Change(meta_path(ws.id), ws.to_json(), f"{kind} {ws.id}"))
        ev = Event(kind=kind, id=ws.id, oid=oid, at=_iso(self.clock()), payload=ws.to_dict())
        for sink in self.sinks:
            sink.emit(ev)
        return oid


def open_service(remote_url: str, home: Path) -> WorkspaceService:
    """CLI·MCP 가 쓰는 조립 지점. Hive 가 없으면 NotFound."""
    from gitswarm.events import sinks_from_config  # Task 10

    hive = Hive.open(remote_url, home)
    config = load_config(home)
    return WorkspaceService(
        hive, MetaStore(hive.git), adapter_for(hive.url, config), sinks_from_config(config)
    )
```

이 태스크에서는 `publish` 가 아직 없어 `test_invalid_transition` 이 `AttributeError` 로 붉다. `events.sinks_from_config` 도 Task 10 이다 — 지금은 `events.py` 에 아래 임시 구현을 넣고 Task 10 이 교체한다:

```python
def sinks_from_config(config) -> list[Sink]:
    return []
```

그리고 `publish` 는 이 태스크에서 전이 검사만 하는 최소 판을 넣는다(Task 7 이 push 를 채운다):

```python
    def publish(self, ws_id: str) -> str:
        ws = self.get(ws_id).with_state(WsState.PUBLISHED)
        return self._write(ws, EV_PUBLISHED)
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_workspace_lifecycle.py -q`
Expected: 9 passed

- [ ] **Step 6: 커밋**

```bash
git add src/gitswarm/service src/gitswarm/events.py tests/test_workspace_lifecycle.py
git commit -m "Add workspace create, get, list, drop"
```

---

### Task 7: read · tree · publish · gc

**Files:**
- Modify: `src/gitswarm/service/workspace.py`
- Create: `tests/test_workspace_read_publish.py`

**Interfaces:**
- Produces on `WorkspaceService`:
  - `read(id, path) -> bytes` (원격 브랜치의 발행된 상태. `..`·절대경로 NotFound)
  - `tree(id, path="") -> list[dict(name, kind, oid)]`
  - `publish(id) -> str` (worktree HEAD 를 원격 브랜치로 push. lease 의 기대값 = **마지막으로 알던 원격**(`refs/remotes/origin/gitswarm/ws/<id>`, create·직전 publish·worktree 의 `git pull --rebase` 가 갱신) — 미리 fetch 하지 않는다. 미리 fetch 하면 남이 올린 커밋을 그대로 덮어쓴다. worktree 없으면 InvalidState, 거절 Conflict, state=published)
  - `gc() -> list[str]` (만료 open → drop, 이벤트 `ws.expired`)

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_workspace_read_publish.py
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.errors import Conflict, InvalidState, NotFound
from gitswarm.service.workspace import Checkout, WorkspaceService, WsState
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore
from tests.conftest import git


@pytest.fixture
def svc(remote_url: str, home: Path) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter())


def _commit_file(wt: Path, name: str, text: str) -> str:
    (wt / name).write_text(text)
    git("add", "--", name, cwd=wt)
    git("commit", "-q", "-m", f"add {name}", cwd=wt)
    return git("rev-parse", "HEAD", cwd=wt)


def test_read_and_tree_before_and_after_publish(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    wt = Path(r.path)
    assert svc.read(r.id, "README.md") == b"seed\n"
    head = _commit_file(wt, "new.txt", "hi\n")
    # 발행 전에는 원격에 없다
    with pytest.raises(NotFound):
        svc.read(r.id, "new.txt")
    assert svc.publish(r.id) == head
    assert svc.read(r.id, "new.txt") == b"hi\n"
    assert svc.get(r.id).state is WsState.PUBLISHED
    names = sorted(e["name"] for e in svc.tree(r.id))
    assert names == ["README.md", "new.txt"]
    assert svc.tree(r.id)[0]["kind"] in {"blob", "tree"}


def test_read_unicode_and_space_path(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    wt = Path(r.path)
    (wt / "문서 모음").mkdir()
    _commit_file(wt, "문서 모음/한글 파일.md", "ㄱ\n")
    svc.publish(r.id)
    assert svc.read(r.id, "문서 모음/한글 파일.md") == "ㄱ\n".encode()
    assert [e["name"] for e in svc.tree(r.id, "문서 모음")] == ["한글 파일.md"]


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", ""])
def test_read_rejects_parent_traversal(svc: WorkspaceService, bad: str):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    with pytest.raises(NotFound):
        svc.read(r.id, bad)


def test_publish_without_worktree_is_invalid(svc: WorkspaceService):
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    with pytest.raises(InvalidState):
        svc.publish(r.id)


def test_publish_lease_conflict(svc: WorkspaceService, tmp_path: Path):
    r = svc.create("main", {}, 0, None, Checkout.WORKTREE, {})
    # 다른 호스트가 같은 브랜치에 먼저 push 한 상황
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", r.branch.removeprefix("refs/heads/"), svc.hive.url, str(other)], check=True)
    _commit_file(other, "theirs.txt", "x\n")
    git("push", "-q", "origin", "HEAD", cwd=other)
    _commit_file(Path(r.path), "mine.txt", "y\n")
    with pytest.raises(Conflict):
        svc.publish(r.id)


def test_gc_drops_expired_only(remote_url: str, home: Path):
    hive = Hive.init(remote_url, home)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), clock=lambda: now)
    old = svc.create("main", {}, 60, None, Checkout.NONE, {})
    forever = svc.create("main", {}, 0, None, Checkout.NONE, {})
    svc.clock = lambda: now + timedelta(seconds=61)
    assert svc.gc() == [old.id]
    assert svc.get(old.id).state is WsState.DROPPED
    assert svc.get(forever.id).state is WsState.OPEN
    assert svc.gc() == []
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_workspace_read_publish.py -q`
Expected: FAIL — `AttributeError: 'WorkspaceService' object has no attribute 'read'` 등

- [ ] **Step 3: 구현(service/workspace.py 에 추가·교체)**

`import` 에 더한다: `from datetime import timedelta`, `from gitswarm.errors import Conflict`, `from gitswarm.driver.git import Git`.

```python
    # ── 읽기 ──────────────────────────────────────────────────
    def _published_rev(self, ws: Workspace) -> str:
        oid = self.hive.git.fetch(ws.branch)
        if oid is None:
            raise NotFound(f"branch {ws.branch} not on remote")
        return oid

    @staticmethod
    def _safe_path(path: str) -> str:
        parts = path.split("/")
        if not path or path.startswith("/") or ".." in parts:
            raise NotFound(f"invalid path: {path!r}")
        return path

    def read(self, ws_id: str, path: str) -> bytes:
        ws = self.get(ws_id)
        rev = f"{self._published_rev(ws)}:{self._safe_path(path)}"
        if not self.hive.git.exists(rev):
            raise NotFound(f"{path} not in {ws_id}")
        return self.hive.git.cat_file(rev)

    def tree(self, ws_id: str, path: str = "") -> list[dict]:
        ws = self.get(ws_id)
        rev = self._published_rev(ws)
        if path:
            rev = f"{rev}:{self._safe_path(path)}"
            if not self.hive.git.exists(rev):
                raise NotFound(f"{path} not in {ws_id}")
        return [{"name": e.name, "kind": e.kind, "oid": e.oid} for e in self.hive.git.ls_tree(rev)]

    # ── 발행 ──────────────────────────────────────────────────
    def publish(self, ws_id: str) -> str:
        ws = self.get(ws_id).with_state(WsState.PUBLISHED)
        wt = self.hive.worktree_dir(ws_id)
        if not wt.exists():
            raise InvalidState(f"{ws_id} has no local worktree; push the branch with git instead")

        head = Git(wt).rev_parse("HEAD")
        if head is None:
            raise InvalidState(f"{ws_id} worktree has no HEAD")
        # 기대값 = 마지막으로 알던 원격. 지금 fetch 하면 남의 커밋을 덮어쓰게 되므로 하지 않는다.
        last_known = self.hive.git.rev_parse(tracking_ref(ws.branch))
        if not self.hive.git.push(head, ws.branch, expected=last_known):
            raise Conflict(f"{ws.branch} moved on remote; run `git pull --rebase` in the worktree")

        self._write(ws, EV_PUBLISHED)
        return head

    # ── 회수 ──────────────────────────────────────────────────
    def gc(self) -> list[str]:
        now = self.clock()
        expired = []
        for ws in self.list(WsState.OPEN):
            if ws.ttl_s == TTL_FOREVER:
                continue
            born = datetime.fromisoformat(ws.created_at.replace("Z", "+00:00"))
            if born + timedelta(seconds=ws.ttl_s) >= now:
                continue
            self._drop_as(ws, EV_EXPIRED)
            expired.append(ws.id)
        return expired
```

`drop` 을 `_drop_as(ws, kind)` 로 쪼개 `drop` 은 `EV_DROPPED`, `gc` 는 `EV_EXPIRED` 를 쓰게 한다:

```python
    def drop(self, ws_id: str) -> Workspace:
        ws = self.get(ws_id)
        if ws.state is WsState.DROPPED:
            return ws
        return self._drop_as(ws, EV_DROPPED)

    def _drop_as(self, ws: Workspace, kind: str) -> Workspace:
        wt = self.hive.worktree_dir(ws.id)
        if wt.exists():
            self.hive.git.worktree_remove(wt)
        self.hive.git.delete_remote(ws.branch)
        if self.hive.git.exists(ws.branch):
            self.hive.git.delete_ref(ws.branch)
        if ws.token_id and Capability.TOKEN in self.adapter.capabilities():
            self.adapter.revoke_token(ws.token_id)

        dropped = ws.with_state(WsState.DROPPED)
        self._write(dropped, kind)
        return dropped
```

`Git(wt).rev_parse("HEAD")` — 드라이버는 `-C <repo>` 로 돌므로 worktree 경로를 repo 로 주면 그 worktree 의 HEAD 를 읽는다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest -q`
Expected: 전부 통과 (Task 6 의 `test_invalid_transition` 포함)

- [ ] **Step 5: 커밋**

```bash
git add src/gitswarm/service/workspace.py tests/test_workspace_read_publish.py
git commit -m "Add workspace read, tree, publish, gc"
```

---

### Task 8: CLI 표면

**Files:**
- Create: `src/gitswarm/surfaces/__init__.py`, `src/gitswarm/surfaces/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes `open_service`, `Hive.init`, `errors.EXIT_CODES`.
- Produces typer 앱 `app`, 진입 `main()`. 모든 출력은 JSON 한 줄(stdout). 성공 `{"ok": true, ...}`, 실패 `{"ok": false, "error": {...}}` + 종료코드.
- 명령: `hive init URL` · `ws create|get|list|read|tree|publish|drop|gc` · `events tail`(Task 10) · `mcp`(Task 9). 공통 옵션 `--remote URL`(env `GITSWARM_REMOTE`).

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_cli.py
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitswarm.surfaces.cli import app

runner = CliRunner()


def run(*args: str, remote: str) -> tuple[int, dict]:
    res = runner.invoke(app, [*args], env={"GITSWARM_REMOTE": remote})
    line = res.stdout.strip().splitlines()[-1]
    return res.exit_code, json.loads(line)


@pytest.fixture
def inited(remote_url: str, home: Path) -> str:
    code, out = run("hive", "init", remote_url, remote=remote_url)
    assert code == 0 and out["ok"] is True
    return remote_url


def test_lifecycle_via_cli(inited: str):
    code, out = run("ws", "create", "--base", "main", "--agent", "impl", "--ttl", "5", "--checkout", remote=inited)
    assert code == 0 and out["ok"] is True
    ws_id = out["id"]
    assert (Path(out["path"]) / "README.md").exists()

    code, out = run("ws", "get", ws_id, remote=inited)
    assert code == 0 and out["state"] == "open" and out["agent"] == {"name": "impl"}

    code, out = run("ws", "read", ws_id, "README.md", remote=inited)
    assert code == 0 and out["content"] == "seed\n"

    code, out = run("ws", "tree", ws_id, remote=inited)
    assert code == 0 and [e["name"] for e in out["entries"]] == ["README.md"]

    code, out = run("ws", "publish", ws_id, remote=inited)
    assert code == 0 and len(out["oid"]) == 40

    code, out = run("ws", "list", "--state", "published", remote=inited)
    assert code == 0 and [w["id"] for w in out["workspaces"]] == [ws_id]

    code, out = run("ws", "drop", ws_id, remote=inited)
    assert code == 0 and out["state"] == "dropped"

    code, out = run("ws", "gc", remote=inited)
    assert code == 0 and out["expired"] == []


def test_error_exit_codes(inited: str):
    code, out = run("ws", "get", "01J00000000000000000000000", remote=inited)
    assert code == 2 and out == {"ok": False, "error": {"kind": "NotFound", "detail": "workspace 01J00000000000000000000000 not found"}}


def test_missing_hive_is_not_found(remote_url: str, home: Path):
    code, out = run("ws", "list", remote=remote_url)
    assert code == 2 and out["error"]["kind"] == "NotFound"


def test_read_binary_is_base64(inited: str):
    code, out = run("ws", "create", "--base", "main", remote=inited)
    ws_id = out["id"]
    # README 는 텍스트라 content 로 온다; 이진이면 content_b64. 여기서는 키 계약만 고정
    code, out = run("ws", "read", ws_id, "README.md", remote=inited)
    assert "content" in out and "content_b64" not in out
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.surfaces`

- [ ] **Step 3: 구현**

```python
# src/gitswarm/surfaces/__init__.py
```

```python
# src/gitswarm/surfaces/cli.py
"""CLI. 출력은 항상 JSON 한 줄 — 에이전트·CI 가 파싱한다. service 만 부른다."""

from __future__ import annotations

import base64
import json
import os
import sys
from functools import wraps
from typing import Annotated

import typer

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import EXIT_CODES, GitswarmError
from gitswarm.service.workspace import Checkout, WsState, open_service
from gitswarm.store.hive import Hive, resolve_home

REMOTE_ENV = "GITSWARM_REMOTE"
EXIT_USAGE = 1

app = typer.Typer(no_args_is_help=True, add_completion=False)
hive_app = typer.Typer(no_args_is_help=True)
ws_app = typer.Typer(no_args_is_help=True)
events_app = typer.Typer(no_args_is_help=True)
app.add_typer(hive_app, name="hive")
app.add_typer(ws_app, name="ws")
app.add_typer(events_app, name="events")

RemoteOpt = Annotated[str | None, typer.Option("--remote", envvar=REMOTE_ENV, help="git remote URL")]


def _emit(payload: dict) -> None:
    typer.echo(json.dumps({"ok": True, **payload}, ensure_ascii=False))


def _remote(remote: str | None) -> str:
    if remote:
        return remote
    typer.echo(json.dumps({"ok": False, "error": {"kind": "Usage", "detail": f"--remote or ${REMOTE_ENV} required"}}))
    raise typer.Exit(EXIT_USAGE)


def guarded(fn):
    """GitswarmError → JSON + 종료코드. 그 밖의 예외는 그대로(버그는 숨기지 않는다)."""

    @wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GitswarmError as e:
            typer.echo(json.dumps(e.to_payload(), ensure_ascii=False))
            raise typer.Exit(EXIT_CODES[e.kind]) from None

    return inner


def _svc(remote: str | None):
    return open_service(_remote(remote), resolve_home())


# ── hive ─────────────────────────────────────────────────────
@hive_app.command("init")
@guarded
def hive_init(url: str) -> None:
    hive = Hive.init(url, resolve_home())
    _emit({"url": hive.url, "path": str(hive.path)})


# ── ws ───────────────────────────────────────────────────────
@ws_app.command("create")
@guarded
def ws_create(
    remote: RemoteOpt = None,
    base: Annotated[str, typer.Option("--base")] = "main",
    agent: Annotated[str, typer.Option("--agent")] = "",
    run: Annotated[str, typer.Option("--run")] = "",
    ttl: Annotated[int, typer.Option("--ttl")] = DEFAULT_TTL_S,
    from_ws: Annotated[str | None, typer.Option("--from-ws")] = None,
    checkout: Annotated[bool, typer.Option("--checkout")] = False,
    label: Annotated[list[str] | None, typer.Option("--label", help="k=v")] = None,
) -> None:
    agent_info = {k: v for k, v in (("name", agent), ("run", run)) if v}
    labels = dict(kv.split("=", 1) for kv in (label or []))
    mode = Checkout.WORKTREE if checkout else Checkout.NONE
    r = _svc(remote).create(base, agent_info, ttl, from_ws, mode, labels)
    _emit(r.to_dict())


@ws_app.command("get")
@guarded
def ws_get(ws_id: str, remote: RemoteOpt = None) -> None:
    _emit(_svc(remote).get(ws_id).to_dict())


@ws_app.command("list")
@guarded
def ws_list(remote: RemoteOpt = None, state: Annotated[str | None, typer.Option("--state")] = None) -> None:
    flt = WsState(state) if state else None
    _emit({"workspaces": [w.to_dict() for w in _svc(remote).list(flt)]})


@ws_app.command("read")
@guarded
def ws_read(ws_id: str, path: str, remote: RemoteOpt = None) -> None:
    data = _svc(remote).read(ws_id, path)
    try:
        _emit({"path": path, "content": data.decode("utf-8")})
    except UnicodeDecodeError:
        _emit({"path": path, "content_b64": base64.b64encode(data).decode()})


@ws_app.command("tree")
@guarded
def ws_tree(ws_id: str, path: str = "", remote: RemoteOpt = None) -> None:
    _emit({"path": path, "entries": _svc(remote).tree(ws_id, path)})


@ws_app.command("publish")
@guarded
def ws_publish(ws_id: str, remote: RemoteOpt = None) -> None:
    _emit({"id": ws_id, "oid": _svc(remote).publish(ws_id)})


@ws_app.command("drop")
@guarded
def ws_drop(ws_id: str, remote: RemoteOpt = None) -> None:
    _emit(_svc(remote).drop(ws_id).to_dict())


@ws_app.command("gc")
@guarded
def ws_gc(remote: RemoteOpt = None) -> None:
    _emit({"expired": _svc(remote).gc()})


# ── mcp ──────────────────────────────────────────────────────
@app.command("mcp")
def mcp_serve() -> None:
    """stdio MCP 서버를 띄운다(Task 9)."""
    from gitswarm.surfaces.mcp import serve

    serve()


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
```

`events tail` 명령은 Task 10 에서 `events_app` 에 붙인다. 미사용 import(`os`)는 넣지 않는다 — ruff 가 잡는다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_cli.py -q && uv run gitswarm --help`
Expected: 4 passed, 도움말에 `hive ws events mcp`

- [ ] **Step 5: 커밋**

```bash
git add src/gitswarm/surfaces tests/test_cli.py
git commit -m "Add CLI surface with JSON output"
```

---

### Task 9: MCP 표면

**Files:**
- Create: `src/gitswarm/surfaces/mcp.py`, `tests/test_mcp.py`

**Interfaces:**
- Produces `mcp = FastMCP("gitswarm")`, `serve()`(stdio). 도구: `hive_init(url)`, `workspace_create(remote, base_ref="main", agent_name="", agent_run="", ttl_s=DEFAULT_TTL_S, from_ws=None, checkout=False, labels={})`, `workspace_get(remote, ws_id)`, `workspace_list(remote, state=None)`, `workspace_read_file(remote, ws_id, path)`, `workspace_tree(remote, ws_id, path="")`, `workspace_publish(remote, ws_id)`, `workspace_drop(remote, ws_id)`, `workspace_gc(remote)`, `events_tail(remote, since=None)`(Task 10 에서 붙임). 반환은 CLI 와 같은 dict. 오류는 `{ok:false, error:{kind, detail}}` 로 **반환**(예외 아님).

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_mcp.py
import asyncio
from pathlib import Path

from fastmcp import Client

from gitswarm.surfaces.mcp import mcp


def call(name: str, **args):
    async def go():
        async with Client(mcp) as c:
            res = await c.call_tool(name, args)
            return res.data

    return asyncio.run(go())


def test_tools_listed():
    async def go():
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}

    names = asyncio.run(go())
    assert {"hive_init", "workspace_create", "workspace_get", "workspace_list", "workspace_read_file",
            "workspace_tree", "workspace_publish", "workspace_drop", "workspace_gc"} <= names


def test_lifecycle_via_mcp(remote_url: str, home: Path):
    assert call("hive_init", url=remote_url)["ok"] is True
    r = call("workspace_create", remote=remote_url, base_ref="main", agent_name="impl", checkout=True)
    assert r["ok"] is True and (Path(r["path"]) / "README.md").exists()
    got = call("workspace_get", remote=remote_url, ws_id=r["id"])
    assert got["state"] == "open"
    rd = call("workspace_read_file", remote=remote_url, ws_id=r["id"], path="README.md")
    assert rd["content"] == "seed\n"
    assert call("workspace_publish", remote=remote_url, ws_id=r["id"])["ok"] is True
    assert call("workspace_drop", remote=remote_url, ws_id=r["id"])["state"] == "dropped"


def test_errors_are_payloads_not_exceptions(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    out = call("workspace_get", remote=remote_url, ws_id="01J00000000000000000000000")
    assert out["ok"] is False and out["error"]["kind"] == "NotFound"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_mcp.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.surfaces.mcp`

- [ ] **Step 3: 구현**

```python
# src/gitswarm/surfaces/mcp.py
"""MCP 표면. 도구 하나 = CLI 명령 하나. 오류는 페이로드로 돌려준다(에이전트가 분기한다)."""

from __future__ import annotations

import base64
from functools import wraps

from fastmcp import FastMCP

from gitswarm.constants import DEFAULT_TTL_S
from gitswarm.errors import GitswarmError
from gitswarm.service.workspace import Checkout, WsState, open_service
from gitswarm.store.hive import Hive, resolve_home

mcp = FastMCP("gitswarm")


def payload(fn):
    @wraps(fn)
    def inner(*args, **kwargs) -> dict:
        try:
            return {"ok": True, **fn(*args, **kwargs)}
        except GitswarmError as e:
            return e.to_payload()

    return inner


def _svc(remote: str):
    return open_service(remote, resolve_home())


@mcp.tool
@payload
def hive_init(url: str) -> dict:
    """원격 하나에 대한 로컬 hive(bare 미러)를 만든다. 멱등."""
    hive = Hive.init(url, resolve_home())
    return {"url": hive.url, "path": str(hive.path)}


@mcp.tool
@payload
def workspace_create(
    remote: str,
    base_ref: str = "main",
    agent_name: str = "",
    agent_run: str = "",
    ttl_s: int = DEFAULT_TTL_S,
    from_ws: str | None = None,
    checkout: bool = False,
    labels: dict[str, str] | None = None,
) -> dict:
    """base 에서 격리 workspace 브랜치를 만든다. checkout=True 면 로컬 worktree 경로도 준다."""
    agent = {k: v for k, v in (("name", agent_name), ("run", agent_run)) if v}
    mode = Checkout.WORKTREE if checkout else Checkout.NONE
    return _svc(remote).create(base_ref, agent, ttl_s, from_ws, mode, labels or {}).to_dict()


@mcp.tool
@payload
def workspace_get(remote: str, ws_id: str) -> dict:
    """workspace 메타를 읽는다."""
    return _svc(remote).get(ws_id).to_dict()


@mcp.tool
@payload
def workspace_list(remote: str, state: str | None = None) -> dict:
    """workspace 목록. state = open|published|dropped."""
    flt = WsState(state) if state else None
    return {"workspaces": [w.to_dict() for w in _svc(remote).list(flt)]}


@mcp.tool
@payload
def workspace_read_file(remote: str, ws_id: str, path: str) -> dict:
    """체크아웃 없이 발행된 파일을 읽는다. 텍스트는 content, 이진은 content_b64."""
    data = _svc(remote).read(ws_id, path)
    try:
        return {"path": path, "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": path, "content_b64": base64.b64encode(data).decode()}


@mcp.tool
@payload
def workspace_tree(remote: str, ws_id: str, path: str = "") -> dict:
    """디렉터리 한 단계를 나열한다."""
    return {"path": path, "entries": _svc(remote).tree(ws_id, path)}


@mcp.tool
@payload
def workspace_publish(remote: str, ws_id: str) -> dict:
    """로컬 worktree 의 커밋을 원격 workspace 브랜치로 push 한다."""
    return {"id": ws_id, "oid": _svc(remote).publish(ws_id)}


@mcp.tool
@payload
def workspace_drop(remote: str, ws_id: str) -> dict:
    """브랜치·worktree·토큰을 거두고 dropped 로 표시한다. 멱등."""
    return _svc(remote).drop(ws_id).to_dict()


@mcp.tool
@payload
def workspace_gc(remote: str) -> dict:
    """ttl 이 지난 open workspace 를 drop 한다."""
    return {"expired": _svc(remote).gc()}


def serve() -> None:
    mcp.run()
```

fastmcp 4 의 `Client(mcp)` 인프로세스 전송과 `res.data` 역직렬화가 다르면 `res.structured_content` 를 쓴다 — 시험 보조 `call()` 한 곳만 바꾼다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_mcp.py -q`
Expected: 3 passed

- [ ] **Step 5: 커밋**

```bash
git add src/gitswarm/surfaces/mcp.py tests/test_mcp.py
git commit -m "Add MCP surface mirroring the CLI"
```

---

### Task 10: 이벤트 — tail 과 sink

**Files:**
- Modify: `src/gitswarm/events.py`, `src/gitswarm/surfaces/cli.py`, `src/gitswarm/surfaces/mcp.py`, `src/gitswarm/service/workspace.py`
- Create: `tests/test_events.py`

**Interfaces:**
- Produces `events.JsonlSink(path)`, `events.WebhookSink(url)`(httpx POST, timeout `WEBHOOK_TIMEOUT_S = 5`, 실패는 stderr 한 줄·예외 없음), `events.sinks_from_config(config) -> list[Sink]`(Task 6 임시판 교체), `events.parse_subject(subject) -> tuple[kind, id]`.
- `WorkspaceService.events(since: str | None) -> list[Event]`(meta 로그 → Event, payload = 그 커밋의 ws json).
- CLI `gitswarm events tail [--since OID]` → `{"ok":true,"events":[...]}`; MCP `events_tail(remote, since=None)`.

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_events.py
import json
from pathlib import Path

import httpx
import respx

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.config import Config, SinkSpec
from gitswarm.events import JsonlSink, WebhookSink, parse_subject, sinks_from_config
from gitswarm.service.workspace import Checkout, WorkspaceService
from gitswarm.store.hive import Hive
from gitswarm.store.meta import MetaStore


def test_parse_subject():
    assert parse_subject("ws.created 01JABC") == ("ws.created", "01JABC")


def test_sinks_from_config(tmp_path: Path):
    cfg = Config(sinks=[SinkSpec("jsonl", str(tmp_path / "e.jsonl")), SinkSpec("webhook", "https://h/x")])
    sinks = sinks_from_config(cfg)
    assert isinstance(sinks[0], JsonlSink) and isinstance(sinks[1], WebhookSink)


def test_jsonl_sink_and_tail(remote_url: str, home: Path, tmp_path: Path):
    hive = Hive.init(remote_url, home)
    path = tmp_path / "events.jsonl"
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), sinks=[JsonlSink(path)])
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    svc.drop(r.id)

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["kind"] for e in lines] == ["ws.created", "ws.dropped"]
    assert lines[0]["payload"]["id"] == r.id

    evs = svc.events(since=None)
    assert [e.kind for e in evs] == ["ws.dropped", "ws.created"]
    assert evs[0].payload["state"] == "dropped" and evs[1].payload["state"] == "open"
    assert svc.events(since=evs[1].oid) == [evs[0]]


@respx.mock
def test_webhook_sink_posts_and_survives_failure(remote_url: str, home: Path, capsys):
    route = respx.post("https://hook.example/e").mock(return_value=httpx.Response(500))
    hive = Hive.init(remote_url, home)
    svc = WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), sinks=[WebhookSink("https://hook.example/e")])
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    assert route.called and json.loads(route.calls[0].request.content)["id"] == r.id
    assert "webhook" in capsys.readouterr().err
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_events.py -q`
Expected: FAIL — `ImportError: cannot import name 'JsonlSink'`

- [ ] **Step 3: events.py 완성(임시 `sinks_from_config` 교체)**

```python
# src/gitswarm/events.py  (전체 교체)
"""이벤트 = meta 커밋 1개. sink 는 발행 시점 1회 전송(재시도 없음, 되감기는 tail --since)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import httpx

from gitswarm.config import Config

WEBHOOK_TIMEOUT_S = 5
SINK_JSONL = "jsonl"
SINK_WEBHOOK = "webhook"


@dataclass(frozen=True)
class Event:
    kind: str
    id: str
    oid: str
    at: str
    payload: dict

    def to_dict(self) -> dict:
        return asdict(self)


class Sink(Protocol):
    def emit(self, event: Event) -> None: ...


@dataclass(frozen=True)
class JsonlSink:
    path: Path

    def emit(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class WebhookSink:
    url: str

    def emit(self, event: Event) -> None:
        try:
            httpx.post(self.url, json=event.to_dict(), timeout=WEBHOOK_TIMEOUT_S).raise_for_status()
        except httpx.HTTPError as e:
            print(f"gitswarm: webhook {self.url} failed: {e}", file=sys.stderr)


def sinks_from_config(config: Config) -> list[Sink]:
    sinks: list[Sink] = []
    for spec in config.sinks:
        if spec.kind == SINK_JSONL:
            sinks.append(JsonlSink(Path(spec.target).expanduser()))
        elif spec.kind == SINK_WEBHOOK:
            sinks.append(WebhookSink(spec.target))
    return sinks


def parse_subject(subject: str) -> tuple[str, str]:
    kind, _, ws_id = subject.partition(" ")
    return kind, ws_id
```

- [ ] **Step 4: service·CLI·MCP 에 tail 추가**

`service/workspace.py` 에:

```python
    def events(self, since: str | None) -> list[Event]:
        out = []
        for entry in self.store.log(since):
            kind, ws_id = parse_subject(entry.subject)
            raw = self.store.read_at(entry.oid, meta_path(ws_id)) or b"{}"
            out.append(Event(kind=kind, id=ws_id, oid=entry.oid, at=entry.committed_at, payload=json.loads(raw)))
        return out
```

(`from gitswarm.events import Event, Sink, parse_subject`; `open_service` 안의 지연 import 를 최상단 `sinks_from_config` import 로 바꾼다 — events.py 는 service 를 import 하지 않으므로 순환 없음.)

`cli.py` 에:

```python
@events_app.command("tail")
@guarded
def events_tail(remote: RemoteOpt = None, since: Annotated[str | None, typer.Option("--since")] = None) -> None:
    _emit({"events": [e.to_dict() for e in _svc(remote).events(since)]})
```

`mcp.py` 에:

```python
@mcp.tool
@payload
def events_tail(remote: str, since: str | None = None) -> dict:
    """meta 로그를 이벤트로 돌려준다(최신순). since = 마지막으로 본 oid."""
    return {"events": [e.to_dict() for e in _svc(remote).events(since)]}
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest -q`
Expected: 전부 통과

- [ ] **Step 6: 커밋**

```bash
git add src/gitswarm/events.py src/gitswarm/service/workspace.py src/gitswarm/surfaces tests/test_events.py
git commit -m "Add event sinks and tail"
```

---

### Task 11: Forgejo 어댑터 — 레포 한정 토큰

**Files:**
- Create: `src/gitswarm/adapters/forgejo.py`, `tests/test_forgejo_adapter.py`

**Interfaces:**
- Produces `ForgejoAdapter(api: str, user: str, credential: str, client: httpx.Client | None = None)`, `ForgejoAdapter.from_spec(spec: RemoteSpec)`(credential_file 을 읽는다; 없으면 `Unsupported`). capabilities = `{TOKEN}`. `issue_token(repo, ws_id, scope)` → `POST {api}/api/v1/users/{user}/tokens` basic auth, body `{"name": "gitswarm-<ws_id>", "scopes": ["read:repository"] | ["write:repository"], "repositories": [repo]}` → `Token(id=str(json.id), secret=json.sha1, scope)`. `revoke_token(id)` → `DELETE .../tokens/{id}`; 404 는 성공.

- [ ] **Step 1: 실패 시험 작성**

```python
# tests/test_forgejo_adapter.py
import json
from pathlib import Path

import httpx
import pytest
import respx

from gitswarm.adapters.forgejo import ForgejoAdapter
from gitswarm.adapters.remote import Capability, Scope
from gitswarm.adapters.select import adapter_for
from gitswarm.config import Config, RemoteSpec
from gitswarm.errors import RemoteError, Unsupported

API = "https://git.example.com"


@pytest.fixture
def adapter() -> ForgejoAdapter:
    return ForgejoAdapter(api=API, user="bot", credential="s3cret")


def test_capabilities(adapter: ForgejoAdapter):
    assert adapter.capabilities() == frozenset({Capability.TOKEN})


@respx.mock
def test_issue_token_is_repo_scoped(adapter: ForgejoAdapter):
    route = respx.post(f"{API}/api/v1/users/bot/tokens").mock(
        return_value=httpx.Response(201, json={"id": 7, "sha1": "tok", "name": "gitswarm-01J"})
    )
    t = adapter.issue_token("org/repo", "01J", Scope.WRITE)
    assert (t.id, t.secret, t.scope) == ("7", "tok", Scope.WRITE)
    body = json.loads(route.calls[0].request.content)
    assert body == {"name": "gitswarm-01J", "scopes": ["write:repository"], "repositories": ["org/repo"]}
    assert route.calls[0].request.headers["authorization"].startswith("Basic ")


@respx.mock
def test_revoke_404_is_success_and_500_is_error(adapter: ForgejoAdapter):
    respx.delete(f"{API}/api/v1/users/bot/tokens/7").mock(return_value=httpx.Response(404))
    adapter.revoke_token("7")
    respx.delete(f"{API}/api/v1/users/bot/tokens/8").mock(return_value=httpx.Response(500))
    with pytest.raises(RemoteError):
        adapter.revoke_token("8")


@respx.mock
def test_issue_failure_does_not_leak_credential(adapter: ForgejoAdapter):
    respx.post(f"{API}/api/v1/users/bot/tokens").mock(return_value=httpx.Response(401, text="bad"))
    with pytest.raises(RemoteError) as ei:
        adapter.issue_token("o/r", "01J", Scope.READ)
    assert "s3cret" not in str(ei.value)


def test_from_spec_reads_credential_file(tmp_path: Path):
    cred = tmp_path / "cred"
    cred.write_text("abc\n")
    a = ForgejoAdapter.from_spec(RemoteSpec("forgejo", API, "bot", str(cred)))
    assert a.credential == "abc"
    with pytest.raises(Unsupported):
        ForgejoAdapter.from_spec(RemoteSpec("forgejo", API, "bot", str(tmp_path / "missing")))


def test_adapter_for_picks_forgejo(tmp_path: Path):
    cred = tmp_path / "cred"
    cred.write_text("abc")
    cfg = Config(remotes={"git.example.com": RemoteSpec("forgejo", API, "bot", str(cred))})
    assert isinstance(adapter_for("ssh://git@git.example.com/o/r.git", cfg), ForgejoAdapter)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_forgejo_adapter.py -q`
Expected: FAIL — `ModuleNotFoundError: gitswarm.adapters.forgejo`

- [ ] **Step 3: 구현**

```python
# src/gitswarm/adapters/forgejo.py
"""Forgejo: workspace 범위 PAT. 자격은 파일에서 읽고 어디에도 다시 쓰지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import httpx

from gitswarm.adapters.remote import Capability, Scope, Token
from gitswarm.config import RemoteSpec
from gitswarm.errors import RemoteError, Unsupported

TOKEN_NAME_PREFIX = "gitswarm-"
SCOPE_NAMES: dict[Scope, str] = {Scope.READ: "read:repository", Scope.WRITE: "write:repository"}
TIMEOUT_S = 10


@dataclass(frozen=True)
class ForgejoAdapter:
    api: str
    user: str
    credential: str = field(repr=False)
    client: httpx.Client | None = field(default=None, repr=False)

    @classmethod
    def from_spec(cls, spec: RemoteSpec) -> ForgejoAdapter:
        path = Path(spec.credential_file).expanduser()
        if not spec.credential_file or not path.exists():
            raise Unsupported(f"forgejo credential file missing for {spec.api}")
        return cls(api=spec.api.rstrip("/"), user=spec.user, credential=path.read_text().strip())

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TOKEN})

    def _http(self) -> httpx.Client:
        return self.client or httpx.Client(auth=(self.user, self.credential), timeout=TIMEOUT_S)

    def _tokens_url(self) -> str:
        return f"{self.api}/api/v1/users/{self.user}/tokens"

    def issue_token(self, repo: str, ws_id: str, scope: Scope) -> Token:
        body = {"name": TOKEN_NAME_PREFIX + ws_id, "scopes": [SCOPE_NAMES[scope]], "repositories": [repo]}
        with self._http() as http:
            r = http.post(self._tokens_url(), json=body, auth=(self.user, self.credential))
        if r.status_code != HTTPStatus.CREATED:
            raise RemoteError(f"forgejo token issue failed: HTTP {r.status_code}")
        j = r.json()
        return Token(id=str(j["id"]), secret=j["sha1"], scope=scope)

    def revoke_token(self, token_id: str) -> None:
        with self._http() as http:
            r = http.delete(f"{self._tokens_url()}/{token_id}", auth=(self.user, self.credential))
        if r.status_code in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_FOUND):
            return
        raise RemoteError(f"forgejo token revoke failed: HTTP {r.status_code}")
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest -q`
Expected: 전부 통과 (`test_adapters.py` 의 plain 기본 선택도 그대로)

- [ ] **Step 5: 커밋**

```bash
git add src/gitswarm/adapters/forgejo.py tests/test_forgejo_adapter.py
git commit -m "Add Forgejo adapter with repo-scoped tokens"
```

---

### Task 12: 실무 투입 — CI·README·설치·실원격 smoke

**Files:**
- Create: `.forgejo/workflows/build.yml`, `scripts/smoke.py`
- Modify: `README.md`

**Interfaces:** 없음(문서·게이트). 이 태스크가 "실무에서 쓸 수 있다"를 증명하는 자리다 — 설치 한 줄, Claude Code MCP 등록 한 블록, 진짜 원격에 대한 생명주기 smoke 가 초록.

- [ ] **Step 1: CI 작성**

keiai-sans `build.yml` 을 복사하되 Dockerfile.ci 를 아래로 바꾼다(git 이 든 비-slim 이미지, 폰트 단계 제거):

```dockerfile
FROM harbor.keiailab.com/ghcr-proxy/astral-sh/uv:python3.12-bookworm
WORKDIR /w
COPY . .
RUN git --version
RUN uv sync --dev
RUN uv run ruff check src scripts tests
RUN uv run ruff format --check src scripts tests
RUN uv run pytest tests -q
RUN uv build && uv run --isolated --with dist/*.whl gitswarm --help
```

워크플로 머리 주석은 "keiai-sans build.yml(2026-08-06 host 러너 이관) 계승 — RFC-0127 원격 buildkitd 로 파이썬 툴체인 실행" 한 줄로 줄인다.

- [ ] **Step 2: smoke 스크립트(PEP 723, 실원격 대상·수동)**

```python
# scripts/smoke.py
# /// script
# requires-python = ">=3.11"
# dependencies = ["gitswarm"]
# ///
"""실제 원격에 대한 생명주기 점검. CI 밖에서 사람이 돌린다(네트워크 필요).

    uv run --with-editable . scripts/smoke.py ssh://git@git.keiailab.com/keiailab-oss/gitswarm.git
"""

import sys
import tempfile
from pathlib import Path

from gitswarm.service.workspace import Checkout, WsState, open_service
from gitswarm.store.hive import Hive


def main(url: str) -> int:
    home = Path(tempfile.mkdtemp(prefix="gitswarm-smoke-"))
    Hive.init(url, home)
    svc = open_service(url, home)

    r = svc.create("main", {"name": "smoke"}, 300, None, Checkout.WORKTREE, {"smoke": "1"})
    print("created", r.id, r.branch)
    assert svc.get(r.id).state is WsState.OPEN
    print("tree", [e["name"] for e in svc.tree(r.id)][:5])
    print("publish", svc.publish(r.id))
    assert svc.get(r.id).state is WsState.PUBLISHED
    print("events", [e.kind for e in svc.events(None)][:3])
    print("drop", svc.drop(r.id).state)
    assert svc.hive.git.ls_remote(r.branch) is None
    print("OK — home", home)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
```

Run(수동): `uv run --with-editable . scripts/smoke.py <실제 원격 URL>` — 원격은 `gitswarm` 레포 자신(Forgejo `keiailab-oss/gitswarm`, Task 12 끝에 만든다). `refs/heads/gitswarm/meta` 와 `ws/*` 브랜치가 생겼다 지워지는 것을 Forgejo 브랜치 목록에서 확인한다.

- [ ] **Step 3: README 작성**

```markdown
# gitswarm

LLM 에이전트 다수를 위한 **git 위 조율 계층**. 임의 git 원격 위에 에이전트마다 격리
workspace 를 만들고, 체크아웃 없이 읽고, 발행하고, 거둔다. 상태는 전부 원격 git 에
있다(`refs/heads/gitswarm/*`) — 데몬·DB 없음.

## 설치

    uv tool install gitswarm        # 또는 uvx gitswarm --help

## 30초

    gitswarm hive init ssh://git@host/org/repo.git
    export GITSWARM_REMOTE=ssh://git@host/org/repo.git
    gitswarm ws create --base main --agent impl --checkout
    #  → {"ok": true, "id": "01J…", "branch": "refs/heads/gitswarm/ws/01J…", "path": "…/wt/01J…", …}
    gitswarm ws read 01J… README.md
    gitswarm ws publish 01J…
    gitswarm ws drop 01J…
    gitswarm events tail

모든 출력은 JSON 한 줄. 오류는 `{"ok": false, "error": {"kind", "detail"}}` + 종료코드
(NotFound 2 · Conflict 3 · InvalidState 4 · Unsupported 5 · RemoteError 6).

## Claude Code 에 MCP 로 붙이기

    claude mcp add gitswarm -- uvx gitswarm mcp

도구: `hive_init` · `workspace_create/get/list/read_file/tree/publish/drop/gc` · `events_tail`.

## 다른 호스트의 에이전트

worktree 없이 브랜치만 받은 에이전트는 평범한 git 으로 일한다:

    git clone -b gitswarm/ws/01J… ssh://git@host/org/repo.git
    … commit …
    git push origin HEAD

## 설정 `~/.gitswarm/config.toml`

    [remote."git.example.com"]
    adapter = "forgejo"                 # 레포 한정 토큰 발급
    api = "https://git.example.com"
    user = "bot"
    credential_file = "~/.config/gitswarm/forgejo.cred"

    [[sink]]
    kind = "jsonl"                      # 또는 webhook
    target = "~/.gitswarm/events.jsonl"

## 상태 배치

    refs/heads/gitswarm/meta        상태(ws/<id>.json) + 이벤트 로그(커밋 이력)
    refs/heads/gitswarm/ws/<id>     workspace 브랜치

쓰기는 `push --force-with-lease` 하나로 CAS. 설계: `docs/superpowers/specs/`.

## 로드맵

A Workspace(이 판) → B Intent(커밋 ↔ 지시·근거 기록) → C Landing(동시 결과 기계 병합).

MIT · KeiaiLab
```

- [ ] **Step 4: 전체 게이트**

Run: `uv run ruff check src scripts tests && uv run ruff format --check src scripts tests && uv run pytest -q && uv build`
Expected: 전부 초록, `dist/gitswarm-0.1.0-py3-none-any.whl`

- [ ] **Step 5: 커밋**

```bash
git add .forgejo README.md scripts
git commit -m "Add CI gate, README, and real-remote smoke script"
```

- [ ] **Step 6: Forgejo 레포 생성과 첫 push·smoke**

관리자 PAT(`~/.config/keiailab/forgejo-admin.token`, 전역 CLAUDE.md §4-2)로 `POST /api/v1/orgs/keiailab-oss/repos {"name":"gitswarm","default_branch":"stable","private":false}` 를 만들고 `git remote add origin ssh://git@git.keiailab.com/keiailab-oss/gitswarm.git` 뒤 land-direct 로 착지한다(`/Users/phil/dev/.claude/land-direct.sh` — 훅이 원시 push 를 막는다). 그 뒤 Step 2 의 smoke 를 이 원격으로 돌려 초록을 확인하고 결과를 PR/커밋 본문이 아니라 세션 보고에 적는다.
