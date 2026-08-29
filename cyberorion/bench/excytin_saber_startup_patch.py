"""Pinned SABER permanent-service startup retry patch.

This is intentionally separate from the SABER resource-limit correctness
patch.  It changes only the default number of permanent-service startup
attempts from five to seven.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import tempfile
from pathlib import Path


EXPECTED_SABER_COMMIT = "a9bdce1343fd1c331aafda3119cbf0d48f215382"
EXPECTED_PATCH_SHA256 = (
    "c9d7e6423ea4864448febd56128ebdb73acb4dc21ffe7dddbf2136fd5b6056de"
)
EXPECTED_UPSTREAM_SANDBOX_SHA256 = (
    "23b82ceb9f8cebc8445348e47fe1516c689dbad672affd87a8e5ccf1c454405a"
)
EXPECTED_PATCHED_SANDBOX_SHA256 = (
    "0abfa204d9050b3569256b3c7c8f8df6b3669455ac6bbe3b3129465d8a32e8a8"
)

_OLD_RETRY_DECLARATION = b"    max_retries: int = 5,\n"
_NEW_RETRY_DECLARATION = b"    max_retries: int = 7,\n"


class SaberStartupPatchError(RuntimeError):
    """Raised when the pinned SABER startup patch cannot be verified."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_patch_artifact(path: Path) -> str:
    try:
        digest = _sha256(path.read_bytes())
    except OSError as exc:
        raise SaberStartupPatchError(
            f"cannot read SABER startup patch artifact: {path}") from exc
    if digest != EXPECTED_PATCH_SHA256:
        raise SaberStartupPatchError(
            f"unexpected SABER startup patch SHA-256: {digest}; "
            f"expected {EXPECTED_PATCH_SHA256}")
    return digest


def _ensure_sandbox_bytes(
    sandbox_path: Path, *, apply_if_needed: bool,
) -> str:
    try:
        source = sandbox_path.read_bytes()
    except OSError as exc:
        raise SaberStartupPatchError(
            f"cannot read pinned SABER sandbox: {sandbox_path}") from exc

    digest = _sha256(source)
    if digest == EXPECTED_PATCHED_SANDBOX_SHA256:
        return "already_patched"
    if digest != EXPECTED_UPSTREAM_SANDBOX_SHA256:
        raise SaberStartupPatchError(
            f"unknown SABER sandbox SHA-256: {digest}; refusing to patch")
    if not apply_if_needed:
        raise SaberStartupPatchError("pinned SABER sandbox is unpatched")
    if source.count(_OLD_RETRY_DECLARATION) != 1:
        raise SaberStartupPatchError(
            "upstream SABER startup retry declaration does not match")

    patched = source.replace(_OLD_RETRY_DECLARATION, _NEW_RETRY_DECLARATION, 1)
    if _sha256(patched) != EXPECTED_PATCHED_SANDBOX_SHA256:
        raise SaberStartupPatchError(
            "computed SABER startup patch result has unexpected SHA-256")

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=sandbox_path.parent, prefix=".sandbox.",
            suffix=".tmp", delete=False,
        ) as temporary:
            temporary.write(patched)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, sandbox_path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass

    if _sha256(sandbox_path.read_bytes()) != EXPECTED_PATCHED_SANDBOX_SHA256:
        raise SaberStartupPatchError(
            "SABER startup sandbox verification failed after patch")
    return "applied"


def ensure_saber_startup_health_retry_patch(
    repo_root: Path, *, apply_if_needed: bool = True,
) -> dict[str, str]:
    """Verify the pinned SABER commit and apply the exact startup patch."""
    patch_path = (
        repo_root / "benchmarks" / "patches" /
        "saber-startup-health-retry.patch"
    )
    patch_digest = _verify_patch_artifact(patch_path)
    try:
        distribution = importlib.metadata.distribution("saber")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SaberStartupPatchError(
            "pinned SABER distribution is not installed") from exc

    direct_url_text = distribution.read_text("direct_url.json")
    try:
        direct_url = json.loads(direct_url_text or "{}")
        commit = direct_url["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SaberStartupPatchError(
            "SABER direct_url.json has no auditable commit") from exc
    if commit != EXPECTED_SABER_COMMIT:
        raise SaberStartupPatchError(
            f"unexpected SABER commit: {commit}; "
            f"expected {EXPECTED_SABER_COMMIT}")

    sandbox_path = Path(distribution.locate_file(
        "saber/sandbox.py")).resolve()
    action = _ensure_sandbox_bytes(
        sandbox_path, apply_if_needed=apply_if_needed)
    return {
        "identity": "upstream SABER + startup health retry patch",
        "saber_version": distribution.version,
        "saber_commit": commit,
        "patch_sha256": patch_digest,
        "sandbox_sha256": EXPECTED_PATCHED_SANDBOX_SHA256,
        "action": action,
        "sandbox_path": str(sandbox_path),
    }
