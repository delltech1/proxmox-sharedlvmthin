#!/usr/bin/python3
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Inert model for qualifying recovery-only LVM synchronous label I/O.

This file intentionally has no subprocess backend.  It specifies the admission
and comparison rules which a later disposable-host harness must satisfy before
``global/use_aio=0`` may be considered for recovery inventory probes.
"""

import argparse
import copy
import json
import re
import sys

sys.dont_write_bytecode = True

SCHEMA = 1
ACK = "I_UNDERSTAND_THIS_IS_A_READ_ONLY_LVM_AB_QUALIFICATION"
BASE_ARGV = ("/sbin/vgs", "--readonly")
SYNC_CONFIG = "global { use_aio=0 }"
VG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")
BOOT_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
LVM_UUID_RE = re.compile(r"^[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}$")
MANIFEST_FIELDS = frozenset({
    "boot_id", "kernel", "lvm_package", "executable_sha256",
    "config_sha256", "device_identity",
})


class Refusal(RuntimeError):
    pass


class Controller:
    """Pure orchestration model with injectable journal/executor/identity."""

    def __init__(self, journal):
        self.journal = journal
        self.state = "NEW"

    def _persist(self, event):
        try:
            self.journal.append(copy.deepcopy(event))
        except Exception:
            self.state = "UNKNOWN"
            raise

    def _refuse(self, classification, reason):
        result = {"classification": classification, "reason": reason}
        self._persist({"kind": "PAIR_RESULT", "result": result})
        self.state = "UNKNOWN"
        return result

    def run_pair(self, device, vg, expected_uuid, executor, identity):
        if self.state != "NEW":
            raise Refusal("pair controller is single-use")
        # Lock out callback reentrancy before calling any injected code.
        self.state = "STARTING"
        try:
            a_argv = tuple(exact_argv(device, vg, sync=False))
            b_argv = tuple(exact_argv(device, vg, sync=True))
            expected_uuid = validate_lvm_uuid(expected_uuid)
            before = validate_manifest(copy.deepcopy(identity()))
            self._persist({"kind": "PAIR_INTENT", "a_argv": a_argv,
                           "b_argv": b_argv, "identity": before})
            self.state = "A_DISPATCHED"
            a_receipt = copy.deepcopy(executor("A", a_argv))
        except Exception:
            self.state = "UNKNOWN"
            raise
        self._persist({"kind": "A_RECEIPT", "receipt": a_receipt})
        admitted, reason = admit_b(a_receipt, a_argv, before, expected_uuid)
        if not admitted:
            return self._refuse("A_AMBIGUOUS_B_FORBIDDEN", reason)
        try:
            after_a = validate_manifest(copy.deepcopy(identity()))
        except Exception:
            self.state = "UNKNOWN"
            raise
        if before != after_a:
            return self._refuse("IDENTITY_CHANGED_B_FORBIDDEN",
                                "host/device/tool identity changed after A")
        self._persist({"kind": "B_INTENT", "identity": after_a})
        self.state = "B_DISPATCHED"
        try:
            b_receipt = copy.deepcopy(executor("B", b_argv))
        except Exception:
            self.state = "UNKNOWN"
            raise
        self._persist({"kind": "B_RECEIPT", "receipt": b_receipt})
        try:
            after_b = validate_manifest(copy.deepcopy(identity()))
        except Exception:
            self.state = "UNKNOWN"
            raise
        result = compare(a_receipt, b_receipt, device, vg, expected_uuid,
                         before, after_b)
        self._persist({"kind": "PAIR_RESULT", "result": result})
        self.state = (
            "EVIDENCE_COMPLETE"
            if result.get("classification") == "PAIR_MATCH_REPETITION_REQUIRED"
            else "UNKNOWN"
        )
        return result


def exact_argv(device, vg, sync=False):
    """Build one fixed, read-only query for one prevalidated VG and device."""
    if not isinstance(device, str) or not device.startswith("/dev/"):
        raise Refusal("device must be an absolute /dev path")
    if any(char.isspace() for char in device) or "," in device:
        raise Refusal("device path must not contain whitespace")
    if not isinstance(vg, str) or not VG_RE.fullmatch(vg):
        raise Refusal("VG is not a canonical LVM name")
    argv = list(BASE_ARGV)
    argv.extend(("--devices", device, "--noheadings", "--separator", "|",
                 "-o", "vg_uuid"))
    if sync:
        argv.extend(("--config", SYNC_CONFIG))
    argv.append(vg)
    return argv


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise Refusal("identity manifest schema is incomplete or open")
    if not isinstance(manifest["boot_id"], str) or not BOOT_RE.fullmatch(manifest["boot_id"]):
        raise Refusal("identity manifest boot ID is invalid")
    for field in ("executable_sha256", "config_sha256"):
        if not isinstance(manifest[field], str) or not HASH_RE.fullmatch(manifest[field]):
            raise Refusal(f"identity manifest {field} is invalid")
    for field in ("kernel", "lvm_package", "device_identity"):
        if not isinstance(manifest[field], str) or not manifest[field] or "\x00" in manifest[field]:
            raise Refusal(f"identity manifest {field} is invalid")
    return copy.deepcopy(manifest)


def validate_lvm_uuid(value):
    if not isinstance(value, str) or not LVM_UUID_RE.fullmatch(value):
        raise Refusal("value is not a canonical LVM UUID")
    return value


def terminal_proof(receipt, manifest=None):
    """Return a refusal reason unless one exact child is conclusively gone."""
    required = {
        "pid", "starttime", "boot_id", "argv", "terminal", "reaped",
        "returncode", "descendants", "unread_stdout", "unread_stderr",
        "output_truncated", "identity_stable", "dstate_observed",
        "deadline_exceeded", "evidence_persisted", "stderr", "stdout",
        "pidfd_pinned", "owned_child",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        return "child receipt schema is incomplete or open"
    if type(receipt["pid"]) is not int or receipt["pid"] <= 1:
        return "child PID is invalid"
    if type(receipt["starttime"]) is not int or receipt["starttime"] <= 0:
        return "child start time is invalid"
    if not isinstance(receipt["boot_id"], str) or not BOOT_RE.fullmatch(receipt["boot_id"]):
        return "child boot ID is invalid"
    if manifest is not None and receipt["boot_id"] != manifest["boot_id"]:
        return "child boot ID differs from identity manifest"
    if not isinstance(receipt["argv"], (tuple, list)) or not all(
            isinstance(item, str) and item for item in receipt["argv"]):
        return "child argv is invalid"
    for field in ("reaped", "unread_stdout", "unread_stderr", "output_truncated",
                  "identity_stable", "dstate_observed", "deadline_exceeded",
                  "evidence_persisted", "pidfd_pinned", "owned_child"):
        if type(receipt[field]) is not bool:
            return f"child receipt {field} is not boolean"
    if receipt["terminal"] != "EXITED" or not receipt["reaped"]:
        return "child is not terminal and reaped"
    if not receipt["pidfd_pinned"] or not receipt["owned_child"]:
        return "child ownership was not positively proven"
    if not receipt["identity_stable"]:
        return "child identity changed or was not proven"
    if receipt["dstate_observed"]:
        return "D-state was observed"
    if receipt["deadline_exceeded"]:
        return "execution deadline was exceeded"
    if receipt["descendants"]:
        return "descendants remain or existed ambiguously"
    if receipt["unread_stdout"] or receipt["unread_stderr"]:
        return "a captured pipe is not at EOF"
    if receipt["output_truncated"]:
        return "captured output was truncated"
    if receipt["stderr"]:
        return "probe emitted unexpected stderr"
    if not receipt["evidence_persisted"]:
        return "probe evidence was not durably persisted"
    if type(receipt["returncode"]) is not int or receipt["returncode"] != 0:
        return "probe did not exit successfully"
    if not isinstance(receipt["descendants"], list) or any(
            type(pid) is not int or pid <= 1 for pid in receipt["descendants"]):
        return "descendant evidence is invalid"
    if not isinstance(receipt["stdout"], str) or not isinstance(receipt["stderr"], str):
        return "captured output is not text"
    return None


def parse_one_uuid(output):
    """Accept exactly one non-empty UUID and no additional output record."""
    if not isinstance(output, str) or "\x00" in output:
        raise Refusal("invalid output encoding")
    rows = [raw.strip() for raw in output.splitlines() if raw.strip()]
    if len(rows) != 1 or "|" in rows[0] or any(char.isspace() for char in rows[0]):
        raise Refusal("expected exactly one VG UUID")
    return validate_lvm_uuid(rows[0])


def admit_b(a_receipt, expected_argv, manifest=None, expected_uuid=None):
    reason = terminal_proof(a_receipt, manifest)
    if reason:
        return False, reason
    if a_receipt["argv"] != expected_argv:
        return False, "A argv differs from the persisted plan"
    try:
        a_uuid = parse_one_uuid(a_receipt["stdout"])
    except Refusal as exc:
        return False, str(exc)
    if expected_uuid is not None and a_uuid != expected_uuid:
        return False, "A UUID differs from configured identity"
    return True, None


def compare(a_receipt, b_receipt, device, vg, expected_uuid,
            manifest_before, manifest_after):
    """Classify one pair.  A clean pair is evidence, never qualification."""
    try:
        expected_uuid = validate_lvm_uuid(expected_uuid)
        manifest_before = validate_manifest(copy.deepcopy(manifest_before))
        manifest_after = validate_manifest(copy.deepcopy(manifest_after))
        expected_a = tuple(exact_argv(device, vg, sync=False))
        expected_b = tuple(exact_argv(device, vg, sync=True))
    except Refusal as exc:
        return {"classification": "INPUT_AMBIGUOUS", "reason": str(exc)}
    admitted, reason = admit_b(
        a_receipt, expected_a, manifest_before, expected_uuid
    )
    if not admitted:
        return {"classification": "A_AMBIGUOUS_B_FORBIDDEN", "reason": reason}
    reason = terminal_proof(b_receipt, manifest_after)
    if reason:
        return {"classification": "B_AMBIGUOUS", "reason": reason}
    if b_receipt["argv"] != expected_b:
        return {"classification": "B_AMBIGUOUS", "reason": "B argv differs from plan"}
    if manifest_before != manifest_after:
        return {"classification": "IDENTITY_CHANGED", "reason": "host/device/tool identity changed between A and B"}
    try:
        a_uuid = parse_one_uuid(a_receipt.get("stdout"))
        b_uuid = parse_one_uuid(b_receipt.get("stdout"))
    except Refusal as exc:
        return {"classification": "OUTPUT_AMBIGUOUS", "reason": str(exc)}
    if a_uuid != expected_uuid or b_uuid != expected_uuid:
        return {"classification": "SEMANTIC_MISMATCH"}
    return {
        "classification": "PAIR_MATCH_REPETITION_REQUIRED",
        "qualified": False,
        "note": "one clean pair does not prove freedom from D-state or SAN stalls",
    }


def plan(device, vg, expected_uuid):
    expected_uuid = validate_lvm_uuid(expected_uuid)
    return {
        "schema": SCHEMA,
        "classification": "PLAN_ONLY_NO_SUBPROCESS",
        "persistent_config_change": False,
        "storage_mutation": False,
        "candidate_first": False,
        "expected_uuid": expected_uuid,
        "a_argv": exact_argv(device, vg, sync=False),
        "b_argv": exact_argv(device, vg, sync=True),
        "b_admission": "only after exact A child is terminal, reaped and unambiguous",
        "result_limit": "a matching pair requires repetition and failure injection",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--vg", required=True)
    parser.add_argument("--expect-vg-uuid", required=True)
    parser.add_argument("--execute-token")
    args = parser.parse_args(argv)
    try:
        result = plan(args.device, args.vg, args.expect_vg_uuid)
    except Refusal as exc:
        print(json.dumps({"classification": "REFUSED", "reason": str(exc)}, sort_keys=True))
        return 2
    if args.execute_token is not None:
        result["classification"] = "REFUSED_EXECUTE_BACKEND_NOT_QUALIFIED"
        result["token_valid"] = args.execute_token == ACK
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
