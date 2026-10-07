"""git plumbing 의 유일한 자리. 위층은 oid·ref·bytes 만 다룬다."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from gitswarm.backoff import backoff_s
from gitswarm.constants import (
    COMMIT_AUTHOR,
    COMMIT_EMAIL,
    FETCH_LOCK_RETRIES,
    HEADS,
    peek_ref,
    tracking_ref,
)
from gitswarm.errors import RemoteError

RC_LS_REMOTE_MISSING = 2
RC_NO_SUCH_REMOTE = 2
RC_FATAL = 128
VERSION_WORD = 2
# Four wordings measured on git 2.55: sequential stale lease; server-side race on update; server-side race on create.
# Any "cannot lock ref" server message is lock contention with a sibling writer; the CAS loop re-fetches and retries.
# Hook declines ("pre-receive hook declined") must NOT match.
REJECTED_MARKERS = (
    "[rejected]",
    "stale info",
    "incorrect old value provided",
    "reference already exists",
    "cannot lock ref",
)
# 로컬 ref 디렉터리를 형제 프로세스가 동시에 만들거나 지울 때(fetch 가 tracking·peek ref 를 쓸 때).
LOCAL_LOCK_MARKER = "cannot lock ref"
MISSING_REMOTE_REF_MARKERS = ("couldn't find remote ref", "remote ref does not exist")
# 원격이 이미 그 oid 를 들고 있으면 git 은 lease 를 보지 않고 이 문구와 rc 0 으로 끝낸다(-q 면 숨긴다).
UP_TO_DATE_MARKER = "Everything up-to-date"
# ls-remote --symref 의 HEAD 줄: "ref: refs/heads/main\tHEAD"
SYMREF_PREFIX = f"ref: {HEADS}"
REMOTE_HEAD = "HEAD"


def is_lease_rejection(stderr: str) -> bool:
    """push stderr 가 lease 거절(재시도 대상)인지. 훅 거절·권한 오류는 아니다."""
    return any(marker in stderr for marker in REJECTED_MARKERS)


# SSH 다중화: 명령 하나의 git 호출들이(그리고 ControlPersist 동안 뒤 명령들도) 핸드셰이크 하나를
# 나눠 쓴다. 호출마다 새 SSH 를 열면 RTT 0.2 s 원격에서 명령당 수 초가 든다.
NETWORK_COMMANDS = frozenset({"fetch", "push", "ls-remote"})
CALLER_SSH_ENVS = ("GIT_SSH_COMMAND", "GIT_SSH")
SSH_CONTROL_DIR = ".ssh-control"
# %C(40자 해시) 대신 고정 이름 — %C 면 기본 홈에서도 한도를 넘는다.
# hive 하나 = 원격 하나일 때만 안전하다(set_origin 은 있는 hive 의 원격을 바꾸지 않는다).
SSH_CONTROL_SOCKET = "mux"
SSH_CONTROL_PERSIST_S = 60
SOCKET_PATH_MAX = 104  # sun_path 바이트 한도(macOS 104 · Linux 108 중 작은 쪽)
MASTER_TEMP_SUFFIX = 17  # ssh 는 마스터 소켓을 "<path>.<16자>" 로 만든 뒤 옮긴다

BLOB_MODE = "100644"
TREE_MODE = "040000"
NUL = "\x00"
NULL_OID = "0000000000000000000000000000000000000000"


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
    def _run(
        self,
        *args: str,
        data: bytes | None = None,
        ok_rc: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": COMMIT_AUTHOR,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_NAME": COMMIT_AUTHOR,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
            # 판정이 git 문구(거절·없음·up-to-date)에 기댄다 — 번역되면 안 된다
            "LC_ALL": "C",
        }
        if args[0] in NETWORK_COMMANDS:
            env |= self._ssh_env()
        p = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            input=data,
            env=env,
            capture_output=True,
        )
        if p.returncode not in ok_rc:
            raise RemoteError(
                p.stderr.decode(errors="replace").strip() or f"git {args[0]} rc={p.returncode}"
            )
        return p

    def _ssh_env(self) -> dict[str, str]:
        """hive 별 ControlMaster 소켓을 거는 GIT_SSH_COMMAND. 호출자가 ssh 를 정했으면 그것이 이긴다."""
        if any(var in os.environ for var in CALLER_SSH_ENVS) or self._config_ssh_command:
            return {}

        if self.mux_problem():
            return {}

        control = self.repo / SSH_CONTROL_DIR
        socket = control / SSH_CONTROL_SOCKET

        # 셸 따옴표(git 이 셸로 돈다) 안에 ssh 따옴표 — ssh 는 -o 값을 공백에서 다시 쪼갠다
        control.mkdir(mode=0o700, exist_ok=True)
        path = str(socket).replace("%", "%%")  # ssh 는 % 를 토큰으로 편다
        control_path = shlex.quote('ControlPath="' + path + '"')
        cmd = (
            f"ssh -o ControlMaster=auto -o {control_path} -o ControlPersist={SSH_CONTROL_PERSIST_S}"
        )
        return {"GIT_SSH_COMMAND": cmd}

    def mux_problem(self) -> str | None:
        """이 레포의 ControlMaster 소켓을 쓸 수 없는 이유. 쓸 수 있으면 None."""
        socket = self.repo / SSH_CONTROL_DIR / SSH_CONTROL_SOCKET
        size = len(os.fsencode(socket)) + MASTER_TEMP_SUFFIX

        # 경로가 한도를 넘으면 ssh 가 "ControlPath too long" 으로 죽는다 — 다중화 없이 간다.
        # `"` 는 ssh 의 -o 값 따옴표 안에 넣을 수 없다 — 역시 다중화 없이.
        if size >= SOCKET_PATH_MAX:
            return f"control socket path too long ({size} >= {SOCKET_PATH_MAX} bytes): {socket}"
        if '"' in str(socket):
            return f"control socket path contains a double quote: {socket}"
        return None

    @cached_property
    def _config_ssh_command(self) -> str:
        """git config 의 core.sshCommand. env 의 GIT_SSH_COMMAND 가 그것을 덮으므로 있으면 비켜선다."""
        return self._run("config", "--get", "core.sshCommand", ok_rc=(0, 1)).stdout.decode().strip()

    def _out(self, *args: str, data: bytes | None = None) -> str:
        return self._run(*args, data=data).stdout.decode().strip()

    # ── 레포·원격 ─────────────────────────────────────────────
    @staticmethod
    def version() -> str:
        """`git --version` 의 판본 낱말(예: "2.55.0"). git 을 못 돌리면 RemoteError."""
        try:
            p = subprocess.run(["git", "--version"], capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            raise RemoteError(f"git not runnable: {type(e).__name__}") from None
        # "git version 2.39.3 (Apple Git-146)" — 셋째 낱말
        return p.stdout.decode().split()[VERSION_WORD]

    @classmethod
    def init_bare(cls, path: Path) -> Git:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "-q", str(path)],
            check=True,
            capture_output=True,
        )
        return cls(path)

    def set_origin(self, url: str) -> None:
        self._run("remote", "add", "--", "origin", url)

    def origin_url(self) -> str:
        return self._out("remote", "get-url", "origin")

    def remote_url(self, name: str) -> str | None:
        """이름 있는 remote 의 URL. 레포 밖이거나(128) 그 remote 가 없으면(2) None."""
        p = self._run("remote", "get-url", name, ok_rc=(0, RC_NO_SUCH_REMOTE, RC_FATAL))
        if p.returncode != 0:
            return None
        return p.stdout.decode().strip()

    def ls_remote(self, ref: str) -> str | None:
        p = self._run(
            "ls-remote",
            "--exit-code",
            "--",
            "origin",
            ref,
            ok_rc=(0, RC_LS_REMOTE_MISSING),
        )
        if p.returncode == RC_LS_REMOTE_MISSING:
            return None
        return p.stdout.decode().split()[0]

    def default_branch(self, remote: str = "origin") -> str | None:
        """원격 HEAD 가 가리키는 브랜치 이름. 빈 레포·끊긴 HEAD 처럼 symref 가 없으면 None."""
        out = self._out("ls-remote", "--symref", "--", remote, REMOTE_HEAD)
        for line in out.splitlines():
            target, _, name = line.partition("\t")
            if name == REMOTE_HEAD and target.startswith(SYMREF_PREFIX):
                return target.removeprefix(SYMREF_PREFIX)
        return None

    def _lock_retry(self, op: Callable[[], str | None]) -> str | None:
        """같은 hive 의 형제 프로세스와 로컬 ref 잠금이 겹치면 잠시 뒤 다시 한다."""
        for attempt in range(FETCH_LOCK_RETRIES - 1):
            try:
                return op()
            except RemoteError as e:
                if LOCAL_LOCK_MARKER not in e.detail:
                    raise
            time.sleep(backoff_s(attempt))
        return op()

    def fetch(self, ref: str) -> str | None:
        """origin/<ref> 를 tracking ref 로 강제 갱신. 원격에 없으면 tracking 도 지우고 None."""
        return self._lock_retry(lambda: self._fetch_once(ref))

    def _fetch_once(self, ref: str) -> str | None:
        return self._fetch_into("origin", ref, tracking_ref(ref))

    def peek(self, ref: str) -> str | None:
        """원격 ref 를 peek ref 로 받아 oid 를 돌려준다. tracking ref 는 건드리지 않는다."""
        # URL 로 받는다 — 이름 있는 remote 로 받으면 git 이 tracking ref 도 덩달아 갱신한다.
        return self._lock_retry(lambda: self._fetch_into(self.origin_url(), ref, peek_ref(ref)))

    def _fetch_into(self, source: str, ref: str, local: str) -> str | None:
        """원격 ref 를 local 로 강제 갱신. 원격에 없으면 local 도 지우고 None.

        사전 ls-remote 없이 fetch 한 번 — 없음은 fetch 의 오류 문구로 판정한다.
        """
        p = self._run("fetch", "-q", "--", source, f"+{ref}:{local}", ok_rc=(0, RC_FATAL))
        if p.returncode == 0:
            return self.rev_parse(local)

        err = p.stderr.decode(errors="replace")
        if not any(m in err for m in MISSING_REMOTE_REF_MARKERS):
            raise RemoteError(err.strip())
        self._run("update-ref", "-d", local)
        return None

    def push(self, oid: str, ref: str, expected: str | None) -> bool:
        """lease push. expected=None 은 "원격에 그 ref 가 없어야 한다"."""
        expect_val = NULL_OID if expected is None else expected
        lease = f"--force-with-lease={ref}:{expect_val}"
        # -q 없이 — 판정에 UP_TO_DATE_MARKER 가 필요하다
        p = self._run("push", lease, "--", "origin", f"{oid}:{ref}", ok_rc=(0, 1))
        err = p.stderr.decode(errors="replace")

        # 원격이 이미 oid 면 git 은 lease 를 건너뛴다 — 기대값이 그 oid 였을 때만 성공이다
        if p.returncode == 0:
            # 한 줄 전체로 맞춘다 — 서버 훅이 같은 문구를 "remote: …" 로 찍을 수 있다
            return UP_TO_DATE_MARKER not in err.splitlines() or expected == oid
        if is_lease_rejection(err):
            return False
        raise RemoteError(err.strip())

    def delete_remote(self, ref: str, expected: str | None) -> bool:
        """원격 ref 삭제. 없으면 성공(멱등).

        expected = 마지막으로 본 oid → lease 삭제, 원격이 옮겨졌으면 지우지 않고 False.
        expected=None = 본 적 없음 → 무조건 삭제.
        """
        args = ["push", "-q"]
        if expected is not None:
            args.append(f"--force-with-lease={ref}:{expected}")
        # `--` 뒤는 위치 인자뿐 — 값이 옵션으로 읽히지 않는다
        p = self._run(*args, "--", "origin", f":{ref}", ok_rc=(0, 1))
        if p.returncode == 0:
            self._run("update-ref", "-d", tracking_ref(ref))
            return True

        # 이미 없으면 삭제는 끝난 것이다(lease 거절 문구로 와도 같다)
        err = p.stderr.decode(errors="replace")
        if any(m in err for m in MISSING_REMOTE_REF_MARKERS) or self.ls_remote(ref) is None:
            self._run("update-ref", "-d", tracking_ref(ref))
            return True
        if is_lease_rejection(err):
            return False
        raise RemoteError(err.strip())

    # ── 객체 ──────────────────────────────────────────────────
    def rev_parse(self, rev: str) -> str | None:
        p = self._run("rev-parse", "--verify", "-q", f"{rev}^{{object}}", ok_rc=(0, 1))
        if p.returncode != 0:
            return None
        return p.stdout.decode().strip()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        p = self._run("merge-base", "--is-ancestor", ancestor, descendant, ok_rc=(0, 1))
        return p.returncode == 0

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
        out = self._run("log", "--format=%H%x00%s%x00%cI", rng).stdout.decode()
        entries = []
        for line in filter(None, out.split("\n")):
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
