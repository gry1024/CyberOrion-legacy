"""Fail-closed setup tests for the pinned SABER correctness patch."""
from __future__ import annotations

from pathlib import Path

import pytest

from cyberorion.bench import excytin_saber_patch as patcher


REPO = Path(__file__).resolve().parents[1]
SOLVER = (
    REPO / "benchmarks" / "external" / "excytin" / ".venv" /
    "lib" / "python3.12" / "site-packages" / "saber" / "agents" /
    "solver_factory.py"
)


def _upstream_solver_bytes() -> bytes:
    patched = SOLVER.read_bytes()
    assert patcher._sha256(patched) == patcher.EXPECTED_PATCHED_SOLVER_SHA256
    upstream = patched.replace(patcher._NEW_HANDLER, patcher._OLD_HANDLER, 1)
    assert patcher._sha256(upstream) == patcher.EXPECTED_UPSTREAM_SOLVER_SHA256
    return upstream


def test_expected_patch_artifact_identity() -> None:
    path = (REPO / "benchmarks" / "patches" /
            "saber-resource-limit-correctness.patch")
    assert patcher._verify_patch_artifact(path) == patcher.EXPECTED_PATCH_SHA256


def test_exact_upstream_solver_is_patched_and_then_idempotent(
    tmp_path: Path,
) -> None:
    solver = tmp_path / "solver_factory.py"
    solver.write_bytes(_upstream_solver_bytes())
    assert patcher._ensure_solver_bytes(
        solver, apply_if_needed=True) == "applied"
    assert patcher._sha256(
        solver.read_bytes()) == patcher.EXPECTED_PATCHED_SOLVER_SHA256
    assert patcher._ensure_solver_bytes(
        solver, apply_if_needed=True) == "already_patched"


def test_unpatched_solver_fails_when_application_is_disabled(
    tmp_path: Path,
) -> None:
    solver = tmp_path / "solver_factory.py"
    solver.write_bytes(_upstream_solver_bytes())
    with pytest.raises(patcher.SaberPatchError, match="unpatched"):
        patcher._ensure_solver_bytes(solver, apply_if_needed=False)


def test_unknown_solver_fails_closed(tmp_path: Path) -> None:
    solver = tmp_path / "solver_factory.py"
    solver.write_text("unexpected solver", encoding="utf-8")
    with pytest.raises(patcher.SaberPatchError, match="unknown SABER solver"):
        patcher._ensure_solver_bytes(solver, apply_if_needed=True)


def test_modified_patch_artifact_fails_closed(tmp_path: Path) -> None:
    patch = tmp_path / "patch.patch"
    patch.write_text("not the expected patch", encoding="utf-8")
    with pytest.raises(patcher.SaberPatchError, match="unexpected SABER patch"):
        patcher._verify_patch_artifact(patch)
