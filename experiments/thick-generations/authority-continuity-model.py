#!/usr/bin/python3
"""Pure review model for an off-SAN Thick admission authority contract.

This module authorizes no storage or executor action.  It makes the missing
rollback-resistant authority-head proof explicit and evaluates only proposals
that a future backend may consume.
"""

import hashlib
import json
import re


HEX32 = re.compile(r"^[a-f0-9]{32}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
BOOT_ID = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
VG_UUID = re.compile(r"^[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}$")
NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def blocked(reason, classification="BLOCKED_ON_CONTINUITY_PROOF"):
    return {"allowed": 0, "action": "REFUSE", "dispatch_allowed": 0,
            "classification": classification, "reason": reason}


def _exact_bit(value):
    return type(value) is int and value in (0, 1)


def enrollment_digest(value):
    return hashlib.sha256((json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ) + "\n").encode()).hexdigest()


def validate_enrollment(value):
    fields = {"schema", "kind", "cluster_id", "storage_id", "vg_uuid", "storage_set",
              "authority_id", "authority_node", "authority_boot_id",
              "enrollment_epoch", "backend", "code_digest", "policy_digest"}
    if not isinstance(value, dict) or set(value) != fields \
            or type(value.get("schema")) is not int or value["schema"] != 1 \
            or value.get("kind") != "PINNED_ADMISSION_AUTHORITY" \
            or not isinstance(value.get("cluster_id"), str) \
            or not NODE.fullmatch(value["cluster_id"]) \
            or not STORAGE_ID.fullmatch(value.get("storage_id", "")) \
            or not VG_UUID.fullmatch(value.get("vg_uuid", "")) \
            or not HEX64.fullmatch(value.get("storage_set", "")) \
            or not HEX32.fullmatch(value.get("authority_id", "")) \
            or not NODE.fullmatch(value.get("authority_node", "")) \
            or not BOOT_ID.fullmatch(value.get("authority_boot_id", "")) \
            or not HEX64.fullmatch(value.get("enrollment_epoch", "")) \
            or value.get("backend") != "LOCAL_PERSISTENT_NO_FAILOVER" \
            or not HEX64.fullmatch(value.get("code_digest", "")) \
            or not HEX64.fullmatch(value.get("policy_digest", "")):
        raise ValueError("authority enrollment schema or identity is invalid")
    return value


def validate_observation(value):
    if not isinstance(value, dict) or set(value) != {"schema", "kind", "status",
            "authority_id", "authority_node", "authority_boot_id",
            "enrollment_epoch", "enrollment_sha256", "ledger_revision",
            "ledger_sha256"} \
            or type(value.get("schema")) is not int or value["schema"] != 1 \
            or value.get("kind") != "AUTHORITY_HEAD_OBSERVATION" \
            or value.get("status") not in {"AVAILABLE", "UNAVAILABLE", "MISSING",
                                              "CORRUPT"}:
        raise ValueError("authority observation schema is invalid")
    if value["status"] == "AVAILABLE":
        if not HEX32.fullmatch(value.get("authority_id", "")) \
                or not NODE.fullmatch(value.get("authority_node", "")) \
                or not BOOT_ID.fullmatch(value.get("authority_boot_id", "")) \
                or not HEX64.fullmatch(value.get("enrollment_epoch", "")) \
                or not HEX64.fullmatch(value.get("enrollment_sha256", "")) \
                or type(value.get("ledger_revision")) is not int \
                or value["ledger_revision"] < 1 \
                or not HEX64.fullmatch(value.get("ledger_sha256", "")):
            raise ValueError("available authority observation is malformed")
    elif any(value.get(name) is not None for name in (
            "authority_id", "authority_node", "authority_boot_id",
            "enrollment_epoch", "enrollment_sha256", "ledger_revision",
            "ledger_sha256")):
        raise ValueError("unavailable authority observation carries trusted state")
    return value


def validate_witness(value):
    fields = {"schema", "kind", "trust_domain", "authority_id",
              "authority_node", "authority_boot_id", "enrollment_epoch",
              "enrollment_sha256", "ledger_revision", "ledger_sha256",
              "proof_digest"}
    if not isinstance(value, dict) or set(value) != fields \
            or type(value.get("schema")) is not int or value["schema"] != 1 \
            or value.get("kind") != "INDEPENDENT_AUTHORITY_HEAD_V1" \
            or value.get("trust_domain") != "ROLLBACK_INDEPENDENT" \
            or not HEX32.fullmatch(value.get("authority_id", "")) \
            or not NODE.fullmatch(value.get("authority_node", "")) \
            or not BOOT_ID.fullmatch(value.get("authority_boot_id", "")) \
            or not HEX64.fullmatch(value.get("enrollment_epoch", "")) \
            or not HEX64.fullmatch(value.get("enrollment_sha256", "")) \
            or type(value.get("ledger_revision")) is not int \
            or value["ledger_revision"] < 1 \
            or not HEX64.fullmatch(value.get("ledger_sha256", "")) \
            or not HEX64.fullmatch(value.get("proof_digest", "")):
        raise ValueError("independent authority-head witness is malformed")
    return value


def _identity(value):
    return tuple(value[name] for name in (
        "authority_id", "authority_node", "authority_boot_id", "enrollment_epoch"
    ))


def evaluate_continuity(enrollment, observation, witness=None):
    try:
        validate_enrollment(enrollment)
        validate_observation(observation)
    except (KeyError, TypeError, ValueError) as error:
        return blocked(str(error), "BLOCKED_MALFORMED_AUTHORITY_EVIDENCE")
    if observation["status"] != "AVAILABLE":
        return blocked("authority history is not positively available")
    if _identity(observation) != _identity(enrollment):
        return blocked("observed authority identity differs from pinned enrollment")
    expected_enrollment_sha = enrollment_digest(enrollment)
    if observation["enrollment_sha256"] != expected_enrollment_sha:
        return blocked("authority observation is not bound to the complete enrollment")
    if witness is None:
        return blocked("rollback-independent authority-head witness is absent")
    try:
        validate_witness(witness)
    except (KeyError, TypeError, ValueError) as error:
        return blocked(str(error), "BLOCKED_MALFORMED_AUTHORITY_EVIDENCE")
    if _identity(witness) != _identity(enrollment):
        return blocked("independent witness identifies another authority history")
    if witness["enrollment_sha256"] != expected_enrollment_sha:
        return blocked("independent witness is not bound to the complete enrollment")
    observed_head = (observation["ledger_revision"], observation["ledger_sha256"])
    witnessed_head = (witness["ledger_revision"], witness["ledger_sha256"])
    if observed_head != witnessed_head:
        reason = "observed authority head differs from rollback-independent witness"
        if observation["ledger_revision"] < witness["ledger_revision"]:
            reason = "authority ledger is older than rollback-independent witness"
        return blocked(reason)
    return {"allowed": 0, "reserve_authorized": 0,
            "action": "MODEL_CONTINUITY_ASSUMPTIONS_MATCH_ONLY",
            "dispatch_allowed": 0, "assumptions_matched": 1,
            "classification": "BLOCKED_ON_CONTINUITY_PROOF",
            "reason": "head bindings match, but witness authenticity, freshness and CAS relationship are unqualified"}


def evaluate_alias_set(enrollments):
    if not isinstance(enrollments, list) or not enrollments:
        return blocked("authority enrollment set is absent",
                       "BLOCKED_MALFORMED_AUTHORITY_EVIDENCE")
    try:
        for item in enrollments:
            validate_enrollment(item)
    except (KeyError, TypeError, ValueError) as error:
        return blocked(str(error), "BLOCKED_MALFORMED_AUTHORITY_EVIDENCE")
    vg = enrollments[0]["vg_uuid"]
    if len({item["storage_id"] for item in enrollments}) != len(enrollments):
        return blocked("alias list contains duplicate storage identities")
    binding = (_identity(enrollments[0]), enrollments[0]["storage_set"],
               enrollments[0]["code_digest"], enrollments[0]["policy_digest"],
               enrollments[0]["cluster_id"], enrollments[0]["backend"])
    if any(item["vg_uuid"] != vg for item in enrollments):
        return blocked("alias comparison spans different VG identities")
    if any((_identity(item), item["storage_set"], item["code_digest"],
            item["policy_digest"], item["cluster_id"], item["backend"]) != binding
           for item in enrollments[1:]):
        return blocked("same-VG aliases do not share one exact authority binding")
    return {"allowed": 0, "reserve_authorized": 0,
            "action": "MODEL_ALIAS_CONSISTENCY_OBSERVED_ONLY",
            "dispatch_allowed": 0, "consistent": 1,
            "classification": "BLOCKED_ON_CONTINUITY_PROOF",
            "reason": "supplied unique aliases agree; participant-set completeness is unproven"}


def evaluate_reenrollment(evidence):
    fields = {"schema", "kind", "cluster_id", "vg_uuid",
              "old_enrollment_sha256", "recovery_transaction",
              "participant_set_sha256", "participant_set_complete",
              "all_potential_executors_fenced", "storage_reconciled",
              "old_authority_disabled", "old_history_retired"}
    if not isinstance(evidence, dict) or set(evidence) != fields \
            or type(evidence.get("schema")) is not int or evidence["schema"] != 1 \
            or evidence.get("kind") != "MANUAL_REENROLLMENT_EVIDENCE" \
            or not NODE.fullmatch(evidence.get("cluster_id", "")) \
            or not VG_UUID.fullmatch(evidence.get("vg_uuid", "")) \
            or not HEX64.fullmatch(evidence.get("old_enrollment_sha256", "")) \
            or not HEX32.fullmatch(evidence.get("recovery_transaction", "")) \
            or not HEX64.fullmatch(evidence.get("participant_set_sha256", "")) \
            or any(not _exact_bit(evidence.get(name)) for name in {
                "participant_set_complete", "all_potential_executors_fenced",
                "storage_reconciled", "old_authority_disabled",
                "old_history_retired"}):
        return blocked("manual re-enrollment evidence is malformed",
                       "BLOCKED_MALFORMED_AUTHORITY_EVIDENCE")
    required = {"participant_set_complete", "all_potential_executors_fenced",
                "storage_reconciled", "old_authority_disabled",
                "old_history_retired"}
    if any(evidence[name] != 1 for name in required):
        return blocked("re-enrollment requires complete fencing and reconciliation")
    return {"allowed": 0, "reenrollment_authorized": 0,
            "action": "MODEL_REENROLLMENT_PRECONDITIONS_ASSUMED",
            "dispatch_allowed": 0, "assumptions_matched": 1,
            "classification": "BLOCKED_ON_CONTINUITY_PROOF",
            "reason": "bound preconditions are asserted but fencing, reconciliation and non-replay proofs are unqualified"}
