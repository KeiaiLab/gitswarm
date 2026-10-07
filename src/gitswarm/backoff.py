"""경합 재시도 간격. 같은 박자로 깨어나는 쓰기 주체들을 지수 + 지터로 흩는다."""

import random

from gitswarm.constants import CAS_BACKOFF_BASE_S, CAS_BACKOFF_MAX_S

JITTER_LOW = 0.5
JITTER_HIGH = 1.5


def backoff_s(attempt: int) -> float:
    """attempt 0 → 0.025~0.075 s, 1 → 0.05~0.15 s, … 상한 2 s 의 0.5~1.5 배."""
    step = min(CAS_BACKOFF_MAX_S, CAS_BACKOFF_BASE_S * 2**attempt)
    return step * random.uniform(JITTER_LOW, JITTER_HIGH)
