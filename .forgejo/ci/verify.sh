#!/bin/sh
# .forgejo/ci/verify.sh — gitswarm 리포 검증(host 러너 · Alpine/BusyBox 가 부른다).
# 툴체인 시험은 파일 끝 ci-pod 블록이 ns ci-test Job 파드에서 돈다(keiai-sans 와 같은 꼴).
# 이 리포는 이미지를 굽지 않는다 — 발행물은 PyPI 휠뿐이고 그것도 CI 밖이다.
set -eu

# ── CI 토큰을 클론에서 걷는다(함정 원장 219) ───────────────────────────────────
# ci.yml 이 x-access-token 을 URL 에 실어 클론했다. 아래 파드로 트리를 보내기 전에
# .git/config 에서 지운다 — 시험(pytest)은 PR 코드라 읽을 수 있기 때문이다.
if [ "${GITHUB_ACTIONS:-}" = true ]; then
  git remote set-url origin "https://git.keiailab.com/${GITHUB_REPOSITORY}.git"
fi

# ── 툴체인 시험은 ci-pod 파드에서 ──────────────────────────────────────────────
# 러너 ─▶ ci-pod[uv] : uv sync → ruff check/format → pytest → uv build
# 인덱스는 파드 env(Nexus) 뿐 — 공개망은 CNP 가 막는다. uv.lock 부재(fleet 관례).
# pytest 가 실제 git(file:// 원격 · worktree · spawn 프로세스)을 쓰므로 git 이 든
# 비-slim uv 이미지를 쓴다.
TOOLCHAIN_IMAGE=harbor.keiailab.com/ghcr-proxy/astral-sh/uv:python3.12-bookworm
TOOLCHAIN_CMD='set -eu
: "${PIP_INDEX_URL:?Nexus 인덱스 없음 — 공개 PyPI 로 가지 않는다}"
export UV_DEFAULT_INDEX="$PIP_INDEX_URL" UV_PYTHON_DOWNLOADS=never
git --version
uv sync --dev
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
# vulture: 허용 오탐은 vulture_whitelist.py 에 이름별 이유와 함께 — typer·fastmcp 데코레이터
# 진입점 · console script main.
uv run vulture src vulture_whitelist.py --min-confidence 60
uv run pytest tests -q --cov --cov-report=term-missing
uv build
if tar tzf dist/*.tar.gz | grep -E "\.hypothesis|\.forgejo"; then echo "sdist leaks dev files"; exit 1; fi
uv run --isolated --no-project --with dist/*.whl gitswarm --help >/dev/null'

if command -v ci-pod >/dev/null 2>&1; then
  ci-pod -n gitswarm-toolchain "$TOOLCHAIN_IMAGE" -- "$TOOLCHAIN_CMD"
  echo "toolchain 시험 OK (ci-pod $TOOLCHAIN_IMAGE)"
elif [ "${GITHUB_ACTIONS:-}" = true ]; then
  echo "FATAL: CI 인데 ci-pod 가 없다 — 시험을 건너뛰면 초록이 거짓말한다"
  exit 1
else
  echo "toolchain 시험: ci-pod 없음(로컬) — 건너뜀"
fi
