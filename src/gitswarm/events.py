# src/gitswarm/events.py
"""이벤트 = meta 커밋 1개. sink 는 발행 시점 1회 전송(재시도 없음, 되감기는 tail --since)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import httpx

from gitswarm.config import Config
from gitswarm.errors import InvalidState

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        else:
            raise InvalidState(f"unknown sink kind {spec.kind!r}; expected jsonl|webhook")
    return sinks


def parse_subject(subject: str) -> tuple[str, str]:
    kind, _, ws_id = subject.partition(" ")
    return kind, ws_id
