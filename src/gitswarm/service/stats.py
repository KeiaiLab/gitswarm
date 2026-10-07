"""이벤트 로그·레코드에서 센 수치. 원격 meta 하나만 읽는다."""

from __future__ import annotations

from collections import Counter

from gitswarm.service.workspace import WorkspaceService, WsState


def summarize(svc: WorkspaceService) -> dict:
    """{by_kind, by_state, open_oldest_age_s, total_events, invalid}. open 이 없으면 age 는 None."""
    events = svc.events(None)
    good, invalid = svc.list_report(None)

    # 가장 오래 열린 workspace — 회수가 밀리는지 보는 지표
    born = [w.born() for w in good if w.state is WsState.OPEN]
    oldest = int((svc.clock() - min(born)).total_seconds()) if born else None

    return {
        "by_kind": dict(Counter(e.kind for e in events)),
        "by_state": dict(Counter(w.state.value for w in good)),
        "open_oldest_age_s": oldest,
        "total_events": len(events),
        "invalid": len(invalid),
    }
