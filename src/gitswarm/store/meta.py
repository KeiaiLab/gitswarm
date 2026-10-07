"""meta 브랜치 = 상태(ws/<id>.json 트리) + 이벤트 로그(커밋 이력).

쓰기는 원격 push 의 lease 하나로 CAS 한다. 로컬 브랜치는 두지 않는다.
변경은 내용이 아니라 변환이다 — 재시도마다 새 tip 의 레코드를 다시 읽어 다시 판정한다.

    fetch ─▶ old ─▶ prev=read(old) ─▶ transform(prev) ─▶ commit(new, parent=old)
                                                           │
                              push new:meta lease=old ◀────┘
                                   │ 거절
                                   ▼ 지수+지터 대기 뒤 다시 fetch (≤ META_CAS_RETRIES)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from gitswarm.backoff import backoff_s
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
    sleep: Callable[[float], None] = time.sleep  # 시험은 no-op 을 넣는다

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

    def read_many_at(self, tip: str, paths: list[str]) -> dict[str, bytes]:
        """한 tip 의 여러 경로를 git 프로세스 하나로. 없는 경로는 빠진다."""
        got = self.read_many([(tip, path) for path in paths])
        return {path: data for (_, path), data in got.items()}

    def read_many(self, spots: list[tuple[str, str]]) -> dict[tuple[str, str], bytes]:
        """(commit oid, 경로) 여러 개를 git 프로세스 하나로 — 이벤트마다 다른 커밋을 읽을 때."""
        revs = {f"{oid}:{path}": (oid, path) for oid, path in spots}
        return {revs[rev]: data for rev, data in self.git.cat_files(list(revs)).items()}

    def list(self, prefix: str) -> list[str]:
        tip = self.tip()
        if tip is None:
            return []
        return self.list_at(tip, prefix)

    def list_at(self, tip: str, prefix: str) -> list[str]:
        """주어진 tip 의 경로 목록(fetch 없음) — 같은 tip 에서 read_at 하라."""
        files = self.git.ls_tree_recursive(tip)
        return sorted(p for p in files if p.startswith(prefix))

    def log(self, since: str | None) -> list[LogEntry]:
        tip = self.tip()
        if tip is None:
            return []
        return self.git.log(tracking_ref(META_REF), since)

    def apply(self, change: Change) -> str:
        for attempt in range(META_CAS_RETRIES):
            # 같은 박자로 거절된 쓰기 주체들을 흩는다
            if attempt:
                self.sleep(backoff_s(attempt - 1))
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
