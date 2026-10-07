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


def sinks_from_config(config) -> list[Sink]:
    return []
