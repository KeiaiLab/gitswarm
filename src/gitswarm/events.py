# src/gitswarm/events.py
"""이벤트 = meta 커밋 1개. sink 는 발행 시점 1회 전송(재시도 없음, 되감기는 tail --since).

`at`: sink 로 보낼 때는 발행 호스트의 벽시계, `events tail` 은 meta 커밋의 커미터 시각.
"""

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
        # InvalidURL 은 HTTPError 도 ValueError 도 아니다(httpx 0.28), idna 오류는 ValueError
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
            # 한 줄로: URL 은 repr, 오류는 종류만(httpx 메시지는 여러 줄이다)
            print(f"gitswarm: webhook {self.url!r} failed: {type(e).__name__}", file=sys.stderr)


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
