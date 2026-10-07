"""Build artifacts (sdist + wheel) contain what a release should, and nothing else."""

import email
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_SDIST = (".hypothesis", ".forgejo", ".dockerignore", "__pycache__", ".coverage")
ENTRY_POINT = "gitswarm = gitswarm.surfaces.cli:main"


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--out-dir", str(out), str(ROOT)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return out


def _wheel(dist: Path) -> zipfile.ZipFile:
    return zipfile.ZipFile(next(dist.glob("*.whl")))


def _sdist_names(dist: Path) -> list[str]:
    with tarfile.open(next(dist.glob("*.tar.gz"))) as tar:
        return tar.getnames()


def _read(wheel: zipfile.ZipFile, suffix: str) -> str:
    name = next(n for n in wheel.namelist() if n.endswith(suffix))
    return wheel.read(name).decode()


def test_wheel_holds_only_package_and_dist_info(dist: Path) -> None:
    names = _wheel(dist).namelist()
    stray = [n for n in names if not n.startswith(("gitswarm/", "gitswarm-"))]

    assert stray == []
    assert any(n.startswith("gitswarm/") for n in names)


def test_sdist_excludes_dev_artifacts(dist: Path) -> None:
    names = _sdist_names(dist)
    leaked = [n for n in names if any(bad in n for bad in FORBIDDEN_SDIST)]

    assert leaked == []


def test_sdist_has_license_and_pyproject(dist: Path) -> None:
    names = _sdist_names(dist)

    assert any(n.endswith("/LICENSE") for n in names)
    assert any(n.endswith("/pyproject.toml") for n in names)


def test_entry_point(dist: Path) -> None:
    assert ENTRY_POINT in _read(_wheel(dist), ".dist-info/entry_points.txt")


def test_metadata(dist: Path) -> None:
    meta = email.message_from_string(_read(_wheel(dist), ".dist-info/METADATA"))
    classifiers = meta.get_all("Classifier") or []

    assert meta["Requires-Python"] == ">=3.11"
    assert "License :: OSI Approved :: MIT License" in classifiers
