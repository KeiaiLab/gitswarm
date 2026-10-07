"""git plumbing 의 유일한 자리. 위층은 oid·ref·bytes 만 다룬다."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gitswarm.constants import COMMIT_AUTHOR, COMMIT_EMAIL, tracking_ref
from gitswarm.errors import RemoteError

RC_LS_REMOTE_MISSING = 2
REJECTED_MARKERS = (
    "[rejected]",
    "stale info",
    "incorrect old value provided",
    "reference already exists",
)
MISSING_REMOTE_REF_MARKERS = ("couldn't find remote ref", "remote ref does not exist")
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
        }
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

    def _out(self, *args: str, data: bytes | None = None) -> str:
        return self._run(*args, data=data).stdout.decode().strip()

    # ── 레포·원격 ─────────────────────────────────────────────
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
        self._run("remote", "add", "origin", url)

    def origin_url(self) -> str:
        return self._out("remote", "get-url", "origin")

    def ls_remote(self, ref: str) -> str | None:
        p = self._run(
            "ls-remote",
            "--exit-code",
            "origin",
            ref,
            ok_rc=(0, RC_LS_REMOTE_MISSING),
        )
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
        # Pre-check: if remote already holds oid and that differs from expected,
        # reject immediately. This handles the case where git's "up-to-date" exit
        # would short-circuit the lease check.
        remote_oid = self.ls_remote(ref)
        if remote_oid == oid:
            expect_val = NULL_OID if expected is None else expected
            if remote_oid != expect_val:
                return False
        expect_val = NULL_OID if expected is None else expected
        lease = f"--force-with-lease={ref}:{expect_val}"
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
