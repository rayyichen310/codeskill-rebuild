"""Fail-closed permit handling for one isolated native OpenClaw compaction.

OpenClaw's public ``before_compaction`` hook identifies the native session,
but its automatic summary request does not expose a public transport-purpose
field.  A CODESKILL trial therefore gives one sidecar endpoint to exactly one
native session.  The hook writes a durable permit for that exact session; the
sidecar can forward only the native summary attempts covered by that permit.

This module deliberately knows nothing about request text, roles, token counts,
or model payload shape.  A permit is retired only after the independent SQLite
detector reports a real native compaction transition.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from time import time
from typing import Any, Callable

from .types import canonical_json, read_json, sha256_text, utc_now, write_json


class NativeSummaryPermitError(RuntimeError):
    """A native-summary permit is absent, malformed, stale, or ambiguous."""


@dataclass(frozen=True)
class NativeSummaryPermit:
    """Auditable metadata for a single public-hook native-summary permit."""

    filename: str
    permit_sha256: str
    session_id: str
    trial_id: str
    issued_at_unix_ms: int
    bypass_ordinal: int

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": "r012_public_hook_native_summary_permit",
            "filename": self.filename,
            "permit_sha256": self.permit_sha256,
            "session_id": self.session_id,
            "trial_id": self.trial_id,
            "issued_at_unix_ms": self.issued_at_unix_ms,
            "bypass_ordinal": self.bypass_ordinal,
            "identity_basis": "isolated_sidecar_expected_session_id",
            "payload_or_prompt_heuristic": "not_used",
        }


@dataclass(frozen=True)
class NormalCallBoundary:
    """A public provider-wrapper signal that this proxy request is normal."""

    filename: str
    boundary_sha256: str
    session_id: str
    trial_id: str
    issued_at_unix_ms: int
    permit_resolution: str

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": "r012_public_plugin_normal_call_boundary",
            "filename": self.filename,
            "boundary_sha256": self.boundary_sha256,
            "session_id": self.session_id,
            "trial_id": self.trial_id,
            "issued_at_unix_ms": self.issued_at_unix_ms,
            "permit_resolution": self.permit_resolution,
            "identity_basis": "public_provider_wrapper_options_session_id",
            "payload_or_prompt_heuristic": "not_used",
        }


class NativeSummaryPermitGate:
    """Read public-hook permits for one isolated sidecar/session pairing.

    The caller invokes :meth:`authorize` after its SQLite detector.  A genuine
    transition retires a live permit before ordinary overlay processing.  In
    the absence of a transition, a permit authorizes a bounded raw native
    summary forwarding attempt.  A public provider wrapper writes a positive
    normal-call boundary before every ordinary solver request.  Such a
    boundary revokes an unresolved permit and the request proceeds through the
    normal overlay; it can never use the native-summary bypass.  Multiple live
    permits, a foreign session, a stale file, recovery after a sidecar restart,
    or too many attempts stop the proxy before upstream I/O.
    """

    _PERMIT_KIND = "codeskill_native_summary_permit"
    _NORMAL_BOUNDARY_KIND = "codeskill_normal_call_boundary"
    _STATE_KIND = "r012_native_summary_permit_gate_state"

    def __init__(
        self,
        *,
        permit_dir: Path,
        expected_session_id: str,
        trial_id: str,
        max_age_seconds: int = 300,
        max_bypass_attempts: int = 4,
        clock: Callable[[], float] = time,
    ) -> None:
        if not isinstance(expected_session_id, str) or not expected_session_id:
            raise ValueError("expected_session_id is required")
        if not isinstance(trial_id, str) or not trial_id:
            raise ValueError("trial_id is required")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if max_bypass_attempts <= 0:
            raise ValueError("max_bypass_attempts must be positive")
        self.permit_dir = Path(permit_dir)
        self.expected_session_id = expected_session_id
        self.trial_id = trial_id
        self.max_age_seconds = max_age_seconds
        self.max_bypass_attempts = max_bypass_attempts
        self.clock = clock
        self.state_path = self.permit_dir / "sidecar-permit-state.json"
        # Raw native-summary forwarding is intentionally process-local.  A
        # restarted proxy can safely retire a proven transition or handle a
        # public normal boundary, but may never replay an unresolved permit.
        self._active_permits_created_here: set[str] = set()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": self._STATE_KIND,
            "trial_id": self.trial_id,
            "expected_session_id": self.expected_session_id,
            "permits": {},
            "normal_boundary_count": 0,
            "created_at_utc": utc_now(),
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        state = read_json(self.state_path)
        if (
            state.get("kind") != self._STATE_KIND
            or state.get("trial_id") != self.trial_id
            or state.get("expected_session_id") != self.expected_session_id
            or not isinstance(state.get("permits"), dict)
        ):
            raise NativeSummaryPermitError("sidecar permit state does not match this isolated trial/session")
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        state["updated_at_utc"] = utc_now()
        write_json(self.state_path, state)

    @staticmethod
    def _is_confirmed_transition(value: dict[str, Any] | None) -> bool:
        return (
            isinstance(value, dict)
            and value.get("kind") == "native_compaction"
            and value.get("confirmed") is True
            and isinstance(value.get("compaction_id"), str)
            and bool(value["compaction_id"])
            and isinstance(value.get("native_session_event_ref"), str)
            and bool(value["native_session_event_ref"])
        )

    def _read_new_permits(self, state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if not self.permit_dir.exists():
            return []
        candidates: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(self.permit_dir.glob("native-summary-*.json")):
            if path.name in state["permits"]:
                continue
            try:
                value = read_json(path)
            except Exception as error:
                raise NativeSummaryPermitError(f"cannot read public-hook permit {path.name}: {type(error).__name__}: {error}") from error
            if not isinstance(value, dict):
                raise NativeSummaryPermitError(f"public-hook permit {path.name} is not an object")
            candidates.append((path.name, value))
        return candidates

    def _validate_permit(self, filename: str, value: dict[str, Any]) -> tuple[str, int]:
        if value.get("kind") != self._PERMIT_KIND or value.get("schema_version") != 1:
            raise NativeSummaryPermitError(f"public-hook permit {filename} has an unknown schema")
        if value.get("trial_id") != self.trial_id:
            raise NativeSummaryPermitError(f"public-hook permit {filename} belongs to a different trial")
        if value.get("session_id") != self.expected_session_id:
            raise NativeSummaryPermitError(f"public-hook permit {filename} belongs to a different native session")
        nonce = value.get("nonce")
        issued = value.get("issued_at_unix_ms")
        if not isinstance(nonce, str) or len(nonce) < 32:
            raise NativeSummaryPermitError(f"public-hook permit {filename} has no usable nonce")
        if isinstance(issued, bool) or not isinstance(issued, int) or issued <= 0:
            raise NativeSummaryPermitError(f"public-hook permit {filename} has no usable issue time")
        age_ms = int(self.clock() * 1000) - issued
        if age_ms < -5_000 or age_ms > self.max_age_seconds * 1000:
            raise NativeSummaryPermitError(f"public-hook permit {filename} is outside its allowed lifetime")
        return sha256_text(canonical_json(value)), issued

    def _activate_new_permit(self, state: dict[str, Any]) -> None:
        candidates = self._read_new_permits(state)
        if not candidates:
            return
        if len(candidates) != 1:
            raise NativeSummaryPermitError("more than one unobserved public-hook permit exists for this isolated sidecar")
        filename, value = candidates[0]
        permit_sha256, issued = self._validate_permit(filename, value)
        active = [
            name
            for name, record in state["permits"].items()
            if isinstance(record, dict) and record.get("status") == "active"
        ]
        if active:
            raise NativeSummaryPermitError("a new public-hook permit arrived before the prior permit was resolved")
        state["permits"][filename] = {
            "status": "active",
            "permit_sha256": permit_sha256,
            "issued_at_unix_ms": issued,
            "bypass_count": 0,
            "activated_at_utc": utc_now(),
        }
        self._active_permits_created_here.add(filename)

    def _read_normal_boundaries(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.permit_dir.exists():
            return []
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.permit_dir.glob("normal-call-*.json")):
            try:
                value = read_json(path)
            except Exception as error:
                raise NativeSummaryPermitError(f"cannot read public normal-call boundary {path.name}: {type(error).__name__}: {error}") from error
            if not isinstance(value, dict):
                raise NativeSummaryPermitError(f"public normal-call boundary {path.name} is not an object")
            candidates.append((path, value))
        return candidates

    def _consume_normal_boundary(self, state: dict[str, Any]) -> NormalCallBoundary | None:
        candidates = self._read_normal_boundaries()
        if not candidates:
            return None
        if len(candidates) != 1:
            raise NativeSummaryPermitError("more than one unconsumed public normal-call boundary exists for this isolated sidecar")
        path, value = candidates[0]
        if value.get("kind") != self._NORMAL_BOUNDARY_KIND or value.get("schema_version") != 1:
            raise NativeSummaryPermitError(f"public normal-call boundary {path.name} has an unknown schema")
        if value.get("trial_id") != self.trial_id:
            raise NativeSummaryPermitError(f"public normal-call boundary {path.name} belongs to a different trial")
        if value.get("session_id") != self.expected_session_id:
            raise NativeSummaryPermitError(f"public normal-call boundary {path.name} belongs to a different native session")
        nonce = value.get("nonce")
        issued = value.get("issued_at_unix_ms")
        if not isinstance(nonce, str) or len(nonce) < 32:
            raise NativeSummaryPermitError(f"public normal-call boundary {path.name} has no usable nonce")
        if isinstance(issued, bool) or not isinstance(issued, int) or issued <= 0:
            raise NativeSummaryPermitError(f"public normal-call boundary {path.name} has no usable issue time")
        age_ms = int(self.clock() * 1000) - issued
        if age_ms < -5_000 or age_ms > self.max_age_seconds * 1000:
            raise NativeSummaryPermitError(f"public normal-call boundary {path.name} is outside its allowed lifetime")
        next_ordinal = int(state.get("normal_boundary_count", 0)) + 1
        state["normal_boundary_count"] = next_ordinal
        state["last_normal_boundary"] = {
            "filename": path.name,
            "boundary_sha256": sha256_text(canonical_json(value)),
            "session_id": self.expected_session_id,
            "trial_id": self.trial_id,
            "issued_at_unix_ms": issued,
            "consumed_ordinal": next_ordinal,
            "consumed_at_utc": utc_now(),
        }
        self._save_state(state)
        try:
            path.unlink()
        except OSError as error:
            raise NativeSummaryPermitError(f"cannot consume public normal-call boundary {path.name}: {type(error).__name__}: {error}") from error
        return NormalCallBoundary(
            filename=path.name,
            boundary_sha256=str(state["last_normal_boundary"]["boundary_sha256"]),
            session_id=self.expected_session_id,
            trial_id=self.trial_id,
            issued_at_unix_ms=issued,
            permit_resolution="no_active_native_summary_permit",
        )

    def _ensure_active_permit_is_fresh(self, state: dict[str, Any], filename: str, record: dict[str, Any]) -> None:
        """Refuse reuse of a permit that aged after its first bypass.

        The public hook is allowed only a small handoff window.  Persisting a
        permit state must not turn that window into an indefinite bypass after
        the sidecar is restarted or a native summary stalls.
        """
        issued = record.get("issued_at_unix_ms")
        if isinstance(issued, bool) or not isinstance(issued, int) or issued <= 0:
            record["status"] = "rejected_invalid_active_record"
            record["rejected_at_utc"] = utc_now()
            self._save_state(state)
            raise NativeSummaryPermitError(f"active public-hook permit {filename} has no usable issue time")
        age_ms = int(self.clock() * 1000) - issued
        if age_ms < -5_000 or age_ms > self.max_age_seconds * 1000:
            record["status"] = "rejected_expired_before_native_sqlite_transition"
            record["rejected_at_utc"] = utc_now()
            self._save_state(state)
            raise NativeSummaryPermitError(f"active public-hook permit {filename} is outside its allowed lifetime")

    def authorize(self, *, compaction_evidence: dict[str, Any] | None) -> NativeSummaryPermit | NormalCallBoundary | None:
        """Authorize a raw summary, or return a positive normal-call boundary."""
        state = self._load_state()
        self._activate_new_permit(state)
        normal_boundary = self._consume_normal_boundary(state)
        active = [
            (name, record)
            for name, record in state["permits"].items()
            if isinstance(record, dict) and record.get("status") == "active"
        ]
        if len(active) > 1:
            raise NativeSummaryPermitError("multiple active public-hook permits exist for this isolated sidecar")
        if not active:
            if normal_boundary is not None:
                return normal_boundary
            self._save_state(state)
            return None
        filename, record = active[0]
        self._ensure_active_permit_is_fresh(state, filename, record)
        if self._is_confirmed_transition(compaction_evidence):
            record["status"] = "retired_after_native_sqlite_transition"
            record["retired_at_utc"] = utc_now()
            record["native_compaction_id"] = compaction_evidence["compaction_id"]
            record["native_session_event_ref"] = compaction_evidence["native_session_event_ref"]
            self._save_state(state)
            self._active_permits_created_here.discard(filename)
            if normal_boundary is not None:
                return replace(normal_boundary, permit_resolution="retired_after_native_sqlite_transition")
            return None
        if normal_boundary is not None:
            resolved_boundary = replace(normal_boundary, permit_resolution="revoked_before_native_sqlite_transition")
            record["status"] = "revoked_by_public_normal_call_boundary_before_native_sqlite_transition"
            record["revoked_at_utc"] = utc_now()
            record["normal_boundary"] = resolved_boundary.evidence()
            self._save_state(state)
            self._active_permits_created_here.discard(filename)
            return resolved_boundary
        if filename not in self._active_permits_created_here:
            record["status"] = "rejected_recovered_unresolved_permit_without_public_normal_boundary"
            record["rejected_at_utc"] = utc_now()
            self._save_state(state)
            raise NativeSummaryPermitError("unresolved public-hook permit was recovered after sidecar restart without a public normal-call boundary")
        bypass_count = record.get("bypass_count")
        if isinstance(bypass_count, bool) or not isinstance(bypass_count, int) or bypass_count < 0:
            raise NativeSummaryPermitError("sidecar permit state has an invalid bypass count")
        if bypass_count >= self.max_bypass_attempts:
            raise NativeSummaryPermitError("native summary did not produce SQLite compaction evidence within its bounded retry allowance")
        record["bypass_count"] = bypass_count + 1
        record["last_bypass_at_utc"] = utc_now()
        self._save_state(state)
        return NativeSummaryPermit(
            filename=filename,
            permit_sha256=str(record["permit_sha256"]),
            session_id=self.expected_session_id,
            trial_id=self.trial_id,
            issued_at_unix_ms=int(record["issued_at_unix_ms"]),
            bypass_ordinal=int(record["bypass_count"]),
        )
