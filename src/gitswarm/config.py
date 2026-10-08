"""~/.gitswarm/config.toml — 어댑터와 이벤트 sink 선언. 없으면 전부 기본."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from gitswarm.errors import InvalidState

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

    # 손으로 쓰는 파일이다 — 오타·모양 오류는 traceback 이 아니라 JSON 계약 안의 InvalidState
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        remotes = {host: RemoteSpec(**spec) for host, spec in raw.get("remote", {}).items()}
        sinks = [SinkSpec(**s) for s in raw.get("sink", [])]
    # ValueError = TOMLDecodeError(문법) + UnicodeDecodeError(UTF-8 아닌 바이트)
    except (ValueError, TypeError, AttributeError, OSError) as e:
        raise InvalidState(f"{CONFIG_FILE}: {type(e).__name__}: {e}") from None
    return Config(remotes=remotes, sinks=sinks)
