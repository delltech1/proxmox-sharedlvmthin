"""Pure receipt consistency check; never an authorization or host probe.

Callers must separately establish the authenticity and freshness of each
input, package and FREEZE integrity, executor fencing and cluster safety.
This prototype is deliberately not connected to any command or dispatch path.
"""
import re

from sharedlvmthin_update_policy import digest


class PredecessorMismatch(ValueError):
    pass


def inspect_predecessor(pending, release, completed, freeze, *, hostname, boot_id):
    """Check relationships among an old CLOSED settlement and a new boot.

    Returns digest references only. Inputs are never modified. Success means
    receipt consistency, not that any package or runtime action is allowed.
    """
    def need(condition, reason):
        if not condition:
            raise PredecessorMismatch(reason)

    def matches(value, pattern):
        return isinstance(value, str) and re.fullmatch(pattern, value) is not None

    uuid = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    hex32, hex64 = r"[0-9a-f]{32}", r"[0-9a-f]{64}"
    need(all(isinstance(x, dict) for x in (pending, release, completed, freeze)),
         "missing or malformed predecessor records")
    prepared = pending.get("prepared_transition")
    need(isinstance(prepared, dict) and prepared.get("schema") == 2
         and prepared.get("state") == "PREPARED" and "kind" not in prepared,
         "invalid prepared transition")
    need(pending.get("schema") == 1 and pending.get("state") == "PACKAGE_SETTLED_RUNTIME_CLOSED"
         and pending.get("authority") == "NONE" and pending.get("reason") == "UNLISTED_RUNTIME_TUPLE",
         "runtime predecessor is not closed")
    need(isinstance(hostname, str) and bool(hostname) and prepared.get("hostname") == hostname,
         "node mismatch")
    old_boot = prepared.get("boot_id")
    need(matches(old_boot, uuid) and matches(boot_id, uuid) and old_boot != boot_id
         and pending.get("boot_id") == old_boot, "boot identity mismatch")
    need(matches(prepared.get("txid"), hex32) and pending.get("txid") == prepared["txid"],
         "transaction mismatch")
    for key in ("candidate_sha256", "candidate_artifact_sha256", "candidate_manifest_sha256",
                "parent_ledger_sha256"):
        need(matches(prepared.get(key), hex64), "invalid transition digest")
    for key in ("runtime_build_id", "runtime_identity_sha256", "transition_sha256"):
        need(matches(pending.get(key), hex64), "invalid runtime digest")
    need(pending.get("transition_sha256") == digest(prepared), "prepared digest mismatch")
    need(prepared.get("target_package") in {"pve-sharedlvmthin", "pve-sharedlvmthin-thick"}
         and isinstance(prepared.get("target_version"), str) and bool(prepared["target_version"])
         and pending.get("plugin_package") == prepared["target_package"]
         and pending.get("plugin_version") == prepared["target_version"]
         and pending.get("plan_digest") == prepared["candidate_sha256"]
         and pending.get("artifact_sha256") == prepared["candidate_artifact_sha256"]
         and pending.get("runtime_manifest_sha256") == prepared["candidate_manifest_sha256"]
         and isinstance(pending.get("kernel_release"), str) and bool(pending["kernel_release"]),
         "installed candidate binding mismatch")
    extensions = {"replacement_proof", "successor_ledger", "rotation_ledger_sha256",
                  "successor_generation", "successor_ledger_sha256"}
    need(completed.get("state") == "COMPLETE" and not (set(completed) - set(prepared) - extensions)
         and all(completed.get(k) == v for k, v in prepared.items() if k != "state"),
         "completed transition mismatch")
    need(freeze.get("phase") == "ACTIVE" and matches(freeze.get("generation"), hex32)
         and completed.get("successor_generation") == freeze["generation"]
         and matches(completed.get("successor_ledger_sha256"), hex64)
         and completed.get("successor_ledger_sha256") == digest(freeze),
         "FREEZE successor mismatch")
    release_keys = ("schema", "runtime_build_id", "artifact_sha256", "plan_digest", "boot_id",
                    "kernel_release", "plugin_package", "plugin_version", "runtime_manifest_sha256",
                    "runtime_identity_sha256", "transition_sha256")
    need(all(k in pending for k in release_keys), "incomplete closed release")
    expected_release = {k: pending[k] for k in release_keys}
    expected_release.update(qualified="UNQUALIFIED", qualification_txid=pending["txid"],
                            verdict="PACKAGE_ONLY_SETTLEMENT", reason=pending["reason"])
    need(release == expected_release, "closed release mismatch")
    return {"result": "CONSISTENT_ONLY", "authority": "NONE",
            "qualification_sha256": digest(pending), "release_sha256": digest(release),
            "package_settlement_sha256": digest(completed), "freeze_ledger_sha256": digest(freeze)}
