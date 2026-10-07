"""meta 브랜치 = 상태(ws/<id>.json 트리) + 이벤트 로그(커밋 이력).

쓰기는 원격 push 의 lease 하나로 CAS 한다. 로컬 브랜치는 두지 않는다.
변경은 내용이 아니라 변환이다 — 재시도마다 새 tip 의 레코드를 다시 읽어 다시 판정한다.

    fetch ─▶ old ─▶ prev=read(old) ─▶ transform(prev) ─▶ commit(new, parent=old)
                                                           │
                              push new:meta lease=old ◀────┘
                                   │ 거절
                                   ▼ 다시 fetch (≤ META_CAS_RETRIES)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gitswarm.constants import META_CAS_RETRIES, META_REF, tracking_ref
from gitswarm.driver.git import Git, LogEntry
from gitswarm.errors import Conflict


@dataclass(frozen=True)
class Change:
    """path 의 현재 내용(없으면 None)을 받아 새 내용을 돌려주는 변환. 예외는 apply 가 그대로 올린다."""

    path: str
    transform: Callable[[bytes | None], bytes]
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
        for _ in range(META_CAS_RETRIES):
            old = self.tip()

            # 새 tip 기준으로 다시 읽고 다시 변환한다 — 남의 변경 위에 옛 판정을 덮지 않는다
            prev = self.read_at(old, change.path) if old else None
            blob = self.git.hash_object(change.transform(prev))

            files = self.git.ls_tree_recursive(old) if old else {}
            files[change.path] = blob
            tree = self.git.build_tree(files)
            new = self.git.commit_tree(tree, [old] if old else [], change.subject)
            if self.git.push(new, META_REF, expected=old):
                return new

        raise Conflict(f"meta CAS failed after {META_CAS_RETRIES} attempts: {change.subject}")
