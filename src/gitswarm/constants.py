"""레포 전체가 공유하는 이름·상한. 매직 값은 여기에만 둔다."""

REF_PREFIX = "gitswarm/"
HEADS = "refs/heads/"
TRACKING = "refs/remotes/origin/"

META_REF = f"{HEADS}{REF_PREFIX}meta"
WS_DIR = "ws"  # meta 트리 안의 디렉터리: ws/<id>.json

META_CAS_RETRIES = 5
DEFAULT_TTL_S = 7200
TTL_FOREVER = 0
ULID_LEN = 26

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


def meta_path(ws_id: str) -> str:
    return f"{WS_DIR}/{ws_id}.json"
