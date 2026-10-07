"""레포 전체가 공유하는 이름·상한. 매직 값은 여기에만 둔다."""

import re

REF_PREFIX = "gitswarm/"
HEADS = "refs/heads/"
TRACKING = "refs/remotes/origin/"
PEEK = "refs/gitswarm/peek/"
LEASE = "refs/gitswarm/lease/"

META_REF = f"{HEADS}{REF_PREFIX}meta"
WS_DIR = "ws"  # meta 트리 안의 디렉터리: ws/<id>.json

# 동시 쓰기 주체 N 이 같은 박자면 N 라운드가 든다. 8 은 작성자 4 에서 3 회 중 1 회 소진됐다.
META_CAS_RETRIES = 16
CAS_BACKOFF_BASE_S = 0.05  # 재시도 간격 = min(MAX, BASE·2^n) × U(0.5, 1.5)
CAS_BACKOFF_MAX_S = 2.0
FETCH_LOCK_RETRIES = 5  # 같은 hive 를 쓰는 형제 프로세스와의 로컬 ref 잠금 경합
DEFAULT_TTL_S = 7200
TTL_FOREVER = 0
MAX_TTL_S = 365 * 24 * 3600  # 1 년. 날짜 연산이 넘치지 않는 상한
TOKEN_ID_RE = re.compile(r"[0-9]+")  # 원격 PAT id: URL 경로에 들어가므로 숫자만

COMMIT_AUTHOR = "gitswarm"
COMMIT_EMAIL = "gitswarm@localhost"


def ws_ref(ws_id: str) -> str:
    return f"{HEADS}{REF_PREFIX}ws/{ws_id}"


def ws_branch(ws_id: str) -> str:
    """worktree add 가 받는 짧은 브랜치 이름."""
    return f"{REF_PREFIX}ws/{ws_id}"


def tracking_ref(ref: str) -> str:
    """refs/heads/X → refs/remotes/origin/X."""
    if not ref.startswith(HEADS):
        raise ValueError(f"not a branch ref: {ref}")
    return TRACKING + ref[len(HEADS) :]


def peek_ref(ref: str) -> str:
    """refs/heads/X → refs/gitswarm/peek/X. 읽기 전용 조회 자리(tracking 과 분리)."""
    if not ref.startswith(HEADS):
        raise ValueError(f"not a branch ref: {ref}")
    return PEEK + ref[len(HEADS) :]


def lease_ref(ref: str) -> str:
    """refs/heads/X → refs/gitswarm/lease/X. publish·drop 의 lease 기준(아무 fetch 도 옮기지 못한다)."""
    if not ref.startswith(HEADS):
        raise ValueError(f"not a branch ref: {ref}")
    return LEASE + ref[len(HEADS) :]


def meta_path(ws_id: str) -> str:
    return f"{WS_DIR}/{ws_id}.json"
