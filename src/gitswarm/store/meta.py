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
