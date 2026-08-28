"""Fail-closed setup for the pinned SABER resource-limit correctness patch."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import tempfile
from pathlib import Path
from typing import Any


EXPECTED_SABER_COMMIT = "a9bdce1343fd1c331aafda3119cbf0d48f215382"
EXPECTED_PATCH_SHA256 = (
    "93d847dfa0c3f0976f412ae74652df139c5415351e395b9a9031e08db8d8fe6f"
)
EXPECTED_UPSTREAM_SOLVER_SHA256 = (
    "8af5f648d01496b2b1844dcd5841078d82238736586cbf6e5befef15646cf024"
)
EXPECTED_PATCHED_SOLVER_SHA256 = (
    "53fce535f3e58d07526348a133b5180c903e5e2b5bdb150dae5edfbb9edb8894"
)

_OLD_HANDLER = b'            except LimitExceededError:\n'
_NEW_HANDLER = (
    b'            except LimitExceededError as exc:\n'
    b'                if exc.type != "tool_call":\n'
    b'                    raise\n'
)


class SaberPatchError(RuntimeError):
    """Raised when pinned SABER cannot be verified and patched exactly."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_patch_artifact(path: Path) -> str:
    try:
        digest = _sha256(path.read_bytes())
    except OSError as exc:
        raise SaberPatchError(f"cannot read SABER patch artifact: {path}") from exc
    if digest != EXPECTED_PATCH_SHA256:
        raise SaberPatchError(
            f"unexpected SABER patch SHA-256: {digest}; "
            f"expected {EXPECTED_PATCH_SHA256}")
    return digest


def _ensure_solver_bytes(solver_path: Path, *, apply_if_needed: bool) -> str:
    try:
        source = solver_path.read_bytes()
    except OSError as exc:
        raise SaberPatchError(f"cannot read pinned SABER solver: {solver_path}") from exc
    digest = _sha256(source)
    if digest == EXPECTED_PATCHED_SOLVER_SHA256:
        return "already_patched"
    if digest != EXPECTED_UPSTREAM_SOLVER_SHA256:
        raise SaberPatchError(
            f"unknown SABER solver SHA-256: {digest}; refusing to patch")
    if not apply_if_needed:
        raise SaberPatchError("pinned SABER solver is unpatched")
    if source.count(_OLD_HANDLER) != 1 or _NEW_HANDLER in source:
        raise SaberPatchError("upstream SABER handler does not match expected context")
    patched = source.replace(_OLD_HANDLER, _NEW_HANDLER, 1)
    if _sha256(patched) != EXPECTED_PATCHED_SOLVER_SHA256:
        raise SaberPatchError("computed SABER patch result has unexpected SHA-256")

    # The dependency lives in the task's pinned venv. Replace atomically so an
    # interrupted setup cannot leave a partially written solver.
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=solver_path.parent, prefix=".solver_factory.",
            suffix=".tmp", delete=False,
        ) as temporary:
            temporary.write(patched)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, solver_path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
    if _sha256(solver_path.read_bytes()) != EXPECTED_PATCHED_SOLVER_SHA256:
        raise SaberPatchError("SABER solver verification failed after patch")
    return "applied"


def ensure_saber_resource_limit_patch(
    repo_root: Path, *, apply_if_needed: bool = True,
) -> dict[str, Any]:
    """Verify the exact SABER pin and make its minimal patch fail closed."""
    patch_path = (
        repo_root / "benchmarks" / "patches" /
        "saber-resource-limit-correctness.patch"
    )
    patch_digest = _verify_patch_artifact(patch_path)
    try:
        distribution = importlib.metadata.distribution("saber")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SaberPatchError("pinned SABER distribution is not installed") from exc
    direct_url_text = distribution.read_text("direct_url.json")
    try:
        direct_url = json.loads(direct_url_text or "{}")
        commit = direct_url["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SaberPatchError("SABER direct_url.json has no auditable commit") from exc
    if commit != EXPECTED_SABER_COMMIT:
        raise SaberPatchError(
            f"unexpected SABER commit: {commit}; expected {EXPECTED_SABER_COMMIT}")
    solver_path = Path(distribution.locate_file(
        "saber/agents/solver_factory.py")).resolve()
    action = _ensure_solver_bytes(
        solver_path, apply_if_needed=apply_if_needed)
    return {
        "identity": "upstream SABER + resource-limit correctness patch",
        "saber_version": distribution.version,
        "saber_commit": commit,
        "patch_sha256": patch_digest,
        "solver_sha256": EXPECTED_PATCHED_SOLVER_SHA256,
        "action": action,
        "solver_path": str(solver_path),
    }
