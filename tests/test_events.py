import json
from pathlib import Path

import httpx
import pytest
import respx

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.config import Config, SinkSpec
from gitswarm.constants import meta_path
from gitswarm.errors import InvalidState, NotFound
from gitswarm.events import Event, JsonlSink, WebhookSink, parse_subject, sinks_from_config
from gitswarm.service.workspace import Checkout, WorkspaceService
from gitswarm.store.hive import Hive
from gitswarm.store.meta import Change, MetaStore


def test_parse_subject():
    assert parse_subject("ws.created 01JABC") == ("ws.created", "01JABC")


def test_sinks_from_config(tmp_path: Path):
    cfg = Config(
        sinks=[SinkSpec("jsonl", str(tmp_path / "e.jsonl")), SinkSpec("webhook", "https://h/x")]
    )
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
    svc = WorkspaceService(
        hive, MetaStore(hive.git), PlainAdapter(), sinks=[WebhookSink("https://hook.example/e")]
    )
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    assert route.called and json.loads(route.calls[0].request.content)["id"] == r.id
    assert "webhook" in capsys.readouterr().err


def _svc(remote_url: str, home: Path, sinks=()) -> WorkspaceService:
    hive = Hive.init(remote_url, home)
    return WorkspaceService(hive, MetaStore(hive.git), PlainAdapter(), sinks=list(sinks))


def test_unknown_sink_kind_raises():
    with pytest.raises(InvalidState):
        sinks_from_config(Config(sinks=[SinkSpec("webhok", "x")]))


def test_events_rejects_bad_since(remote_url: str, home: Path):
    svc = _svc(remote_url, home)
    with pytest.raises(NotFound):
        svc.events(since="--output=/x")


def test_events_corrupt_record_raises(remote_url: str, home: Path):
    svc = _svc(remote_url, home)
    ws_id = "01J00000000000000000000000"
    svc.store.apply(Change(meta_path(ws_id), lambda _: b"not json", f"ws.created {ws_id}"))
    with pytest.raises(InvalidState):
        svc.events(None)
    svc.store.apply(Change(meta_path(ws_id), lambda _: b"[1]", f"ws.created {ws_id}"))
    with pytest.raises(InvalidState):
        svc.events(None)


def test_jsonl_sink_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "new" / "dir" / "e.jsonl"
    JsonlSink(path).emit(Event("k", "i", "o", "t", {}))
    assert path.exists()


class _Boom:
    def emit(self, event: Event) -> None:
        raise RuntimeError("sink down")


def _blocked_jsonl(tmp_path: Path) -> JsonlSink:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    return JsonlSink(blocker / "e.jsonl")


@pytest.mark.parametrize(
    "make_sink",
    [
        lambda tmp: _Boom(),
        _blocked_jsonl,
        lambda tmp: WebhookSink("http://ex\nample/"),  # httpx.InvalidURL
        lambda tmp: WebhookSink("http://xn--/"),  # idna ValueError
    ],
)
def test_sink_failure_does_not_fail_commit(
    remote_url: str, home: Path, tmp_path: Path, capsys, make_sink
):
    svc = _svc(remote_url, home, [make_sink(tmp_path)])
    r = svc.create("main", {}, 0, None, Checkout.NONE, {})
    # 기록은 이미 원격에 있다 — sink 오류가 재시도(=중복 workspace)를 부르지 않는다
    assert svc.get(r.id).id == r.id
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("gitswarm: ")
