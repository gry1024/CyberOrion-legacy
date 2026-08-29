"""Regression tests for the pinned SABER startup health retry patch."""
from __future__ import annotations

from pathlib import Path

import pytest

from cyberorion.bench import excytin_saber_startup_patch as patcher


REPO = Path(__file__).resolve().parents[1]
SANDBOX = (
    REPO / "benchmarks" / "external" / "excytin" / ".venv" /
    "lib" / "python3.12" / "site-packages" / "saber" / "sandbox.py"
)


def _upstream_sandbox_bytes() -> bytes:
    patched = SANDBOX.read_bytes()
    assert patcher._sha256(patched) == patcher.EXPECTED_PATCHED_SANDBOX_SHA256
    upstream = patched.replace(
        patcher._NEW_RETRY_DECLARATION,
        patcher._OLD_RETRY_DECLARATION,
        1,
    )
    assert patcher._sha256(upstream) == patcher.EXPECTED_UPSTREAM_SANDBOX_SHA256
    return upstream


def test_expected_startup_patch_artifact_identity() -> None:
    path = REPO / "benchmarks" / "patches" / "saber-startup-health-retry.patch"
    assert patcher._verify_patch_artifact(path) == patcher.EXPECTED_PATCH_SHA256


def test_pinned_sandbox_is_exactly_patched() -> None:
    assert patcher._sha256(SANDBOX.read_bytes()) == (
        patcher.EXPECTED_PATCHED_SANDBOX_SHA256)
    assert SANDBOX.read_bytes().count(
        patcher._NEW_RETRY_DECLARATION) == 1


def test_exact_upstream_sandbox_is_patched_and_idempotent(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox.py"
    sandbox.write_bytes(_upstream_sandbox_bytes())
    assert patcher._ensure_sandbox_bytes(
        sandbox, apply_if_needed=True) == "applied"
    assert patcher._sha256(
        sandbox.read_bytes()) == patcher.EXPECTED_PATCHED_SANDBOX_SHA256
    assert patcher._ensure_sandbox_bytes(
        sandbox, apply_if_needed=True) == "already_patched"


def test_unpatched_sandbox_fails_when_application_is_disabled(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox.py"
    sandbox.write_bytes(_upstream_sandbox_bytes())
    with pytest.raises(patcher.SaberStartupPatchError, match="unpatched"):
        patcher._ensure_sandbox_bytes(sandbox, apply_if_needed=False)


def test_unknown_sandbox_fails_closed(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox.py"
    sandbox.write_text("unexpected SABER sandbox", encoding="utf-8")
    with pytest.raises(patcher.SaberStartupPatchError, match="unknown SABER sandbox"):
        patcher._ensure_sandbox_bytes(sandbox, apply_if_needed=True)


def test_modified_patch_artifact_fails_closed(tmp_path: Path) -> None:
    patch = tmp_path / "patch.patch"
    patch.write_text("not the expected patch", encoding="utf-8")
    with pytest.raises(
        patcher.SaberStartupPatchError,
        match="unexpected SABER startup patch",
    ):
        patcher._verify_patch_artifact(patch)
