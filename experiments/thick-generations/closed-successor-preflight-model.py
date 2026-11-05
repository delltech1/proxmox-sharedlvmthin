"""Pure closed-successor preflight model: NOT an admission mechanism.

Observations are synthetic caller inputs, not proofs, locks or OS probes.
Nothing here installs packages, changes files, opens runtime or grants a right
to dispatch. A model result cannot be consumed by the production gate.
"""
import re

from closed_predecessor_prototype import inspect_predecessor


def evaluate(pending, release, completed, freeze, candidate, observed, *, hostname, boot_id):
    """Evaluate receipt relationships and explicit unproved prerequisites."""
    predecessor = inspect_predecessor(pending, release, completed, freeze,
                                      hostname=hostname, boot_id=boot_id)
    expected_keys = {"source_package", "source_version", "target_package", "target_version",
                     "deb_sha256", "artifact_sha256", "manifest_sha256", "tuple_status"}
    if not isinstance(candidate, dict) or set(candidate) != expected_keys:
        raise ValueError("candidate identity is incomplete or ambiguous")
    if candidate["source_package"] != pending["plugin_package"] \
            or candidate["source_version"] != pending["plugin_version"] \
            or candidate["target_package"] not in {"pve-sharedlvmthin", "pve-sharedlvmthin-thick"} \
            or not isinstance(candidate["target_version"], str) \
            or not re.fullmatch(r"[A-Za-z0-9.+:~_-]{1,128}", candidate["target_version"]):
        raise ValueError("candidate is not the exact successor source")
    for key in ("deb_sha256", "artifact_sha256", "manifest_sha256"):
        if not isinstance(candidate[key], str) or not re.fullmatch(r"[0-9a-f]{64}", candidate[key]):
            raise ValueError("candidate digest is malformed")
    if candidate["tuple_status"] != "UNLISTED":
        raise ValueError("closed-only model requires an exact UNLISTED classification")
    checks = ("freeze_and_package_intact", "dpkg_exclusive", "executor_fence_and_inventory",
              "no_competing_dispatch", "storage_and_workers_safe", "maintscript_effects_deferred")
    if not isinstance(observed, dict) or set(observed) != set(checks) | {"active_transition", "boot_id"}:
        raise ValueError("observations are incomplete or ambiguous")
    if observed["boot_id"] != boot_id:
        raise ValueError("boot changed during observation")
    if observed["active_transition"] != "ABSENT":
        raise ValueError("existing or unknown package transition")
    if any(type(observed[key]) is not bool for key in checks):
        raise ValueError("prerequisite observation is not boolean")
    missing = [key for key in checks if not observed[key]]
    return {"result": "MODEL_BLOCKED" if missing else "MODEL_CONSISTENT_ONLY",
            "authority": "NONE", "dispatch_allowed": False, "runtime_open": False,
            "predecessor": predecessor, "missing": missing}
