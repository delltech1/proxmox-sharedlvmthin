#!/usr/bin/python3
"""Pure policy model for the BASTRIX APT transaction guard."""

import hashlib
import json
import os
import re
import stat


MAX_PROTOCOL_BYTES = 16 * 1024 * 1024
MAX_ACTIONS = 4096
VERSION_RE = re.compile(r"^[A-Za-z0-9.+:~_-]+$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?$")
ARCH_RE = re.compile(r"^(?:-|[a-z0-9][a-z0-9-]*)$")
MULTIARCH = {"-", "same", "foreign", "allowed", "none", "no"}


class Refusal(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def manual_authorization_matches(authorization, plan_digest, payloads, context):
    """Match every durable one-shot authorization boundary exactly."""
    return bool(
        isinstance(authorization, dict)
        and authorization.get("schema") == 2
        and re.fullmatch(r"[0-9a-f]{32}", authorization.get("authorization_id", ""))
        and authorization.get("plan_digest") == plan_digest
        and authorization.get("payloads") == payloads
        and authorization.get("context") == context
    )


def strict_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refusal(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=pairs)


def parse_protocol_v3(raw):
    if len(raw) > MAX_PROTOCOL_BYTES:
        raise Refusal("APT hook protocol exceeds size limit")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise Refusal("APT hook protocol is not UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "VERSION 3":
        raise Refusal("APT hook protocol v3 is required")
    try:
        separator = lines.index("", 1)
    except ValueError as exc:
        raise Refusal("APT hook protocol has no configuration terminator") from exc
    # APT's v3 stream may legitimately repeat configuration keys inherited
    # from multiple scopes. The guard does not use configuration values for
    # authorization; retain every value instead of applying unsafe last-one-
    # wins semantics or rejecting a real APT transaction.
    config = {}
    for line in lines[1:separator]:
        if "=" not in line:
            raise Refusal("malformed APT configuration line")
        key, value = line.split("=", 1)
        if not key:
            raise Refusal("empty APT configuration key")
        config.setdefault(key, []).append(value)
    actions = []
    for line in lines[separator + 1:]:
        if not line:
            continue
        fields = line.split(" ", 8)
        if len(fields) != 9:
            raise Refusal("malformed APT package action")
        package, old_version, old_arch, old_multi, direction, new_version, new_arch, new_multi, action = fields
        if not PACKAGE_RE.fullmatch(package):
            raise Refusal("unsafe APT package name")
        if old_version != "-" and not VERSION_RE.fullmatch(old_version):
            raise Refusal("unsafe old package version")
        if new_version != "-" and not VERSION_RE.fullmatch(new_version):
            raise Refusal("unsafe new package version")
        if not ARCH_RE.fullmatch(old_arch) or not ARCH_RE.fullmatch(new_arch):
            raise Refusal("unsafe package architecture")
        if old_multi not in MULTIARCH or new_multi not in MULTIARCH or direction not in {"<", ">", "="}:
            raise Refusal("invalid APT package action metadata")
        if action not in {"**CONFIGURE**", "**REMOVE**"} and not action.startswith("/"):
            raise Refusal("APT package payload path must be absolute")
        actions.append({
            "package": package, "old_version": old_version, "old_arch": old_arch,
            "old_multiarch": old_multi, "direction": direction,
            "new_version": new_version, "new_arch": new_arch,
            "new_multiarch": new_multi, "action": action,
        })
        if len(actions) > MAX_ACTIONS:
            raise Refusal("APT hook action count exceeds limit")
    return {"protocol": 3, "actions": actions}


def base_package(name):
    return name.split(":", 1)[0]


def watched(action, manifest):
    name = base_package(action["package"])
    if name in manifest.get("watched_packages", []):
        return True
    return any(re.fullmatch(pattern, name) for pattern in manifest.get("watched_package_patterns", []))


def relevant_actions(plan, manifest):
    return [action for action in plan["actions"] if watched(action, manifest)]


def unsupported_identity_changes(actions):
    """Return package identities that cannot be proved by a version-only tuple.

    Compatibility tuples currently qualify one native architecture per package.
    An APT architecture or Multi-Arch transition must therefore never inherit a
    tuple merely because its version string happens to match.  Install/remove
    rows use '-' on one side and are handled by tuple/result-state checks.
    """
    refused = []
    for action in actions:
        old_arch = action["old_arch"]
        new_arch = action["new_arch"]
        old_multi = action["old_multiarch"]
        new_multi = action["new_multiarch"]
        if old_arch != "-" and new_arch != "-" and old_arch != new_arch:
            refused.append({"package": action["package"], "old_arch": old_arch,
                            "new_arch": new_arch, "reason": "architecture-change"})
        elif old_multi != "-" and new_multi != "-" and old_multi != new_multi:
            refused.append({"package": action["package"], "old_multiarch": old_multi,
                            "new_multiarch": new_multi, "reason": "multiarch-change"})
    return refused


def resulting_versions(installed, actions):
    result = dict(installed)
    for action in actions:
        name = base_package(action["package"])
        if action["action"] == "**REMOVE**" or action["new_version"] == "-":
            result[name] = None
        else:
            result[name] = action["new_version"]
    return result


def exact_tuple(manifest, versions, *, profile, api, running_kernel, plugin_version, scope):
    for item in manifest.get("tuples", []):
        if item.get("status") not in {"QUALIFIED", "EXACT_LAB_TESTED"}:
            continue
        if item.get("required_tests"):
            continue
        if profile not in item.get("profiles", []) or item.get("api") != api:
            continue
        if item.get("running_kernel") != running_kernel or scope not in item.get("scopes", []):
            continue
        if plugin_version not in item.get("plugin_versions", []):
            continue
        expected = item.get("packages", {})
        if expected and all(versions.get(name) == value for name, value in expected.items()):
            return item
    return None


def observed_tuple(manifest, versions, *, profile, api, running_kernel, plugin_version):
    """Identify an exact catalogued runtime without implying qualification."""
    matches = []
    for item in manifest.get("tuples", []):
        if profile not in item.get("profiles", []) or item.get("api") != api:
            continue
        if item.get("running_kernel") != running_kernel:
            continue
        if plugin_version not in item.get("plugin_versions", []):
            continue
        expected = item.get("packages", {})
        if expected and all(versions.get(name) == value for name, value in expected.items()):
            matches.append(item)
    if len(matches) > 1:
        raise Refusal("runtime matches multiple compatibility tuples")
    return matches[0] if matches else None


def known_incompatible(manifest, versions, compare_versions):
    storage = versions.get("libpve-storage-perl")
    qemu = versions.get("qemu-server")
    if not storage or not qemu:
        return None
    for rule in manifest.get("known_incompatible", []):
        if compare_versions(storage, "ge", rule["storage_min"]) and compare_versions(qemu, "lt", rule["qemu_max_exclusive"]):
            return rule
    return None


def evaluate(plan, manifest, policy, context, compare_versions):
    related = relevant_actions(plan, manifest)
    if not related:
        return {"verdict": "ALLOW_UNRELATED", "relevant": [], "post_gate_required": False}
    mode = policy.get("mode")
    if mode == "UNSELECTED":
        return {"verdict": "REFUSE_POLICY_UNSELECTED", "relevant": related, "post_gate_required": False}
    if mode in {"WARN", "MANUAL_OVERRIDE"}:
        return {"verdict": "REQUIRE_EXACT_MANUAL_AUTHORIZATION", "relevant": related,
                "post_gate_required": False}
    if mode not in {"QUALIFIED_AUTO", "QUALIFIED_ONLY", "FREEZE"}:
        raise Refusal("unknown update policy mode")
    if mode == "FREEZE":
        # A configure-only action can follow a raw foreign unpack and execute
        # maintainer scripts. FREEZE therefore refuses every relevant action;
        # interrupted watched packages require an explicit recovery plan.
        return {"verdict": "REFUSE_FREEZE", "relevant": related, "post_gate_required": False}
    identity_changes = unsupported_identity_changes(related)
    if identity_changes:
        return {"verdict": "REFUSE_UNQUALIFIED_PACKAGE_IDENTITY_CHANGE",
                "identity_changes": identity_changes, "relevant": related,
                "post_gate_required": False}
    target_versions = resulting_versions(context["installed"], plan["actions"])
    incompatible = known_incompatible(manifest, target_versions, compare_versions)
    if incompatible:
        return {"verdict": "REFUSE_KNOWN_INCOMPATIBLE", "reason": incompatible["reason"],
                "relevant": related, "post_gate_required": False}
    common = dict(profile=context["profile"], api=context["api"],
                  running_kernel=context["running_kernel"], plugin_version=context["plugin_version"],
                  scope=policy.get("required_scope", "san-dataplane"))
    current = exact_tuple(manifest, context["installed"], **common)
    target = exact_tuple(manifest, target_versions, **common)
    if not current or not target:
        return {"verdict": "REFUSE_UNQUALIFIED", "current_tuple": current and current["id"],
                "target_tuple": target and target["id"], "relevant": related, "post_gate_required": False}
    represented = set(target.get("packages", {}))
    # A configure-only replay is still security- and compatibility-relevant:
    # it can execute maintainer scripts after a previous interrupted/foreign
    # unpack.  Do not let a watched package bypass tuple coverage merely
    # because this particular APT transaction carries no archive payload.
    unrepresented = sorted({base_package(row["package"]) for row in related
                            if base_package(row["package"]) not in represented})
    if current["id"] == target["id"]:
        if unrepresented:
            return {"verdict": "REFUSE_UNQUALIFIED_PACKAGE_CHANGE", "packages": unrepresented,
                    "current_tuple": current["id"], "target_tuple": target["id"],
                    "relevant": related, "post_gate_required": False}
        return {"verdict": "ALLOW_QUALIFIED", "current_tuple": current["id"],
                "target_tuple": target["id"], "relevant": related, "post_gate_required": True}
    edge = next((edge for edge in manifest.get("rolling_edges", [])
                 if edge.get("from") == current["id"] and edge.get("to") == target["id"]
                 and edge.get("status") in {"QUALIFIED", "EXACT_LAB_TESTED"}), None)
    if not edge:
        return {"verdict": "REFUSE_UNQUALIFIED_EDGE", "current_tuple": current["id"],
                "target_tuple": target["id"], "relevant": related, "post_gate_required": False}
    # Name-only allowances cannot prove an old/new version, architecture or
    # operation.  Refuse legacy entries rather than interpreting them as an
    # unbounded permission.  A future manifest schema may add exact transition
    # records, but schema 1 intentionally has no such escape hatch.
    allowed_extra = edge.get("allowed_package_changes", [])
    if allowed_extra:
        return {"verdict": "REFUSE_UNSAFE_EDGE_ALLOWANCE",
                "packages": sorted(set(allowed_extra)),
                "current_tuple": current["id"], "target_tuple": target["id"],
                "relevant": related, "post_gate_required": False}
    if unrepresented:
        return {"verdict": "REFUSE_UNQUALIFIED_PACKAGE_CHANGE", "packages": unrepresented,
                "current_tuple": current["id"], "target_tuple": target["id"],
                "relevant": related, "post_gate_required": False}
    return {"verdict": "ALLOW_QUALIFIED_EDGE", "current_tuple": current["id"],
            "target_tuple": target["id"], "edge": edge.get("id"),
            "relevant": related, "post_gate_required": True}


def hash_payloads(plan):
    result = []
    for action in plan["actions"]:
        path = action["action"]
        if not path.startswith("/"):
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as exc:
            raise Refusal(f"cannot safely open APT payload: {path}") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Refusal(f"APT payload is not a single-link regular file: {path}")
            hasher = hashlib.sha256()
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                hasher.update(block)
            after = os.fstat(descriptor)
            identity_before = (before.st_dev, before.st_ino, before.st_size,
                               before.st_mtime_ns, before.st_ctime_ns, before.st_nlink)
            identity_after = (after.st_dev, after.st_ino, after.st_size,
                              after.st_mtime_ns, after.st_ctime_ns, after.st_nlink)
            if identity_before != identity_after:
                raise Refusal(f"APT payload changed while it was hashed: {path}")
            result.append({"path": path, "sha256": hasher.hexdigest(), "size": before.st_size,
                           "device": before.st_dev, "inode": before.st_ino})
        finally:
            os.close(descriptor)
    return result
