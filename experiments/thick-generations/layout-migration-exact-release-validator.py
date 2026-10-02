#!/usr/bin/env python3
"""Unshipped pure release proof adapter. No CLI, transport or effect authority.

Evidence authenticity is NOT implied by JSON hashes. A future reviewed local
transport must supply fresh, authenticated node observations and durable reads.
Until that boundary and the installed fresh loader exist, only the explicitly
named offline constructor is available. It cannot authorize a live executor.
"""
import builtins
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import types

HERE = Path(__file__).resolve().parent
FILES = (
    "layout-migration-exact-restore-model.py", "layout-migration-all-certified-v2.py",
    "layout-migration-release-certificate-v2.py", "layout-migration-all-ready-v2.py",
    "layout-migration-all-configured-v2.py", "layout-migration-v2-context.py",
    "layout-migration-topology-v2.py", "layout-migration-plan.py", "layout-migration-node-evidence.py",
    "layout-migration-release-certificate.py", "layout-migration-all-ready-plan.py",
    "layout-migration-finalize-plan.py",
    "layout-migration-workload-gate.py",
)
CERTIFIED_FIELDS = ("context", "topology_template", "baseline_raw", "target_raw", "configured",
    "refresh_authorization", "stored_ready", "release_authorization", "ready_records", "certificate_raw",
    "collection", "records")
BYTE_FIELDS = ("baseline_raw", "target_raw", "certificate_raw")


class Refusal(ValueError):
    pass


def need(value, message):
    if not value:
        raise Refusal(message)


def exact(value, fields, label):
    need(type(value) is dict and set(value) == set(fields), label + " fields differ")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw):
    need(type(raw) is bytes and 0 < len(raw) <= 8 * 1024 * 1024, "evidence byte bound")
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate evidence key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(Refusal("nonfinite evidence")))
    need(canonical(value) == raw, "noncanonical evidence")
    return value


class FrozenVerifiers:
    """Execute only the supplied hash-pinned bytes, including nested loaders.

    A private importlib facade is installed in each module's own builtins. It
    does not patch global importlib or reuse sys.modules. No verifier source is
    reopened by a transitive spec_from_file_location call (including ABA).
    The bundle is trusted CODE, never part of the evidence JSON or network API.
    """
    def __init__(self, sources, expected):
        exact(sources, FILES, "verifier source closure")
        exact(expected, FILES, "trusted verifier digest closure")
        self.sources = {}
        for name in FILES:
            raw = sources[name]
            need(type(raw) is bytes and 0 < len(raw) <= 2 * 1024 * 1024
                 and hashlib.sha256(raw).hexdigest() == expected[name], "verifier source digest differs")
            self.sources[name] = raw
        self.sources = types.MappingProxyType(self.sources)
        self.hashes = types.MappingProxyType(dict(expected))
        self.importlib = types.ModuleType("importlib")
        self.importlib.util = types.SimpleNamespace(spec_from_file_location=self.spec,
            module_from_spec=importlib.util.module_from_spec)
        self.loaded = set()

    def importer(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name == "importlib.util" and level == 0:
            return self.importlib.util if fromlist else self.importlib
        return builtins.__import__(name, globals, locals, fromlist, level)

    def spec(self, name, path):
        path = Path(path)
        need(path.parent == HERE and path.name in self.sources, "verifier import outside pinned closure")
        owner = self
        class Loader:
            def create_module(self, spec): return None
            def exec_module(self, module):
                module.__file__ = str(path)
                module.__builtins__ = {**vars(builtins), "__import__": owner.importer}
                owner.loaded.add(path.name)
                need(hashlib.sha256(owner.sources[path.name]).hexdigest() == owner.hashes[path.name], "loaded verifier digest drift")
                exec(compile(owner.sources[path.name], str(path), "exec"), module.__dict__)
        return importlib.util.spec_from_loader(name, Loader(), origin=str(path))

    def load(self, file):
        spec = self.spec("fresh_" + file.replace("-", "_"), HERE / file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class Validator:
    def __init__(self):
        raise Refusal("trusted cross-node transport and installed fresh loader are not integrated")

    @classmethod
    def for_offline_test(cls, sources, expected):
        instance = object.__new__(cls)
        instance.code = FrozenVerifiers(sources, expected)
        instance.last_time = None
        instance.bound = None
        return instance

    def evaluate(self, raw, request, executor, before, *, now):
        """Return an offline executor-shaped grant, NEVER install as live callback.

        Re-evaluation of the exact same request/evidence is allowed for the
        executor's pre-effect bracket. A different request/cohort or regressed
        clock is refused. Durable no-replay belongs to the local executor.
        """
        need(type(now) is int and now > 0 and (self.last_time is None or now >= self.last_time), "validator clock")
        self.last_time = now
        evidence = strict_json(raw)
        A = self.code.load("layout-migration-all-certified-v2.py")
        M = self.code.load("layout-migration-exact-restore-model.py")
        W = self.code.load("layout-migration-workload-gate.py")
        A.builtin(evidence)
        exact(evidence, {"schema", "authority", "plan", "baseline", "baseline_sources", "baseline_captures", "workload", "current", "certified_inputs",
                         "archives", "cohort_id", "issued_at", "expires_at"}, "release envelope")
        need(evidence["schema"] == "slt-exact-local-release-evidence/v1" and evidence["authority"] == "NONE",
             "evidence authority ceiling")
        plan, baseline = evidence["plan"], evidence["baseline"]
        M.validate_plan(plan, now)
        need(type(evidence["issued_at"]) is int and type(evidence["expires_at"]) is int
             and plan["issued_at"] <= evidence["issued_at"] <= now <= evidence["expires_at"] <= plan["expires_at"]
             and 0 < evidence["expires_at"] - evidence["issued_at"] <= 30
             and type(evidence["cohort_id"]) is str and A.HEX32.fullmatch(evidence["cohort_id"]), "fresh cohort deadline")
        names = [r["node"] for r in plan["participants"]]
        exact(baseline, {"authority", "plan_sha256", "records"}, "baseline")
        need(baseline["authority"] == "NONE" and baseline["plan_sha256"] == M.digest(plan), "baseline plan")
        exact(baseline["records"], names, "baseline coverage")
        exact(evidence["baseline_captures"], names, "baseline capture coverage")
        exact(evidence["baseline_sources"], names, "baseline source coverage")
        exact(evidence["current"], names, "current coverage")
        workload = copy.deepcopy(evidence["workload"])
        exact(workload, {"schema", "observed_at", "managed_storages", "consumers", "running_consumers", "storage_cfg_sha256",
                         "resources_sha256", "config_sha256", "mutation_performed", "authorization", "verdict", "limitations", "evidence_sha256"}, "workload")
        claimed = workload.pop("evidence_sha256")
        need(claimed == digest(workload) == plan["workload_sha256"] and workload["schema"] == "slt-workload-prepare/v1"
             and workload["verdict"] == "SNAPSHOT_READY" and workload["authorization"] == "NONE"
             and workload["mutation_performed"] is False and workload["running_consumers"] == []
             and workload["storage_cfg_sha256"] == plan["storage_cfg_sha256"]
             and type(workload["observed_at"]) is int and 0 < workload["observed_at"] <= evidence["issued_at"]
             and M.sha(workload["resources_sha256"]), "workload identity differs")
        cfg = evidence["certified_inputs"].get("baseline_raw")
        need(type(cfg) is str and workload["managed_storages"] == W.storage_ids(cfg.encode()), "workload managed scope differs")
        validator = object.__new__(M.Coordinator); validator.plan = plan
        for node in names:
            captured = evidence["baseline_captures"][node]
            exact(captured, {"schema", "authority", "mutation_performed", "tx", "plan_sha256", "observation",
                             "service_namespace", "artifact_identity", "source_evidence_sha256"}, "baseline capture")
            need(captured["schema"] == "slt-exact-local-baseline/v1" and captured["authority"] == "NONE"
                 and captured["mutation_performed"] is False and captured["tx"] == plan["tx"]
                 and captured["plan_sha256"] == digest(plan) and captured["observation"] == baseline["records"][node]
                 and captured["service_namespace"] == evidence["baseline_sources"][node]
                 and type(captured["source_evidence_sha256"]) is list and len(captured["source_evidence_sha256"]) == 2
                 and all(M.sha(v) for v in captured["source_evidence_sha256"]), "baseline was not recomposed from exact captures")
            self.file_identity(captured["artifact_identity"], stat.S_ISREG)
            need(captured["artifact_identity"][6] == 65, "baseline artifact marker length")
            validator.validate_record(baseline["records"][node], node, before=True)
            current = evidence["current"][node]
            exact(current, {"cohort_id", "observed_at", "observation", "service_namespace", "runtime"}, "current node")
            need(current["cohort_id"] == evidence["cohort_id"] and type(current["observed_at"]) is int
                 and evidence["issued_at"] <= current["observed_at"] <= now, "stale/mixed current cohort")
            validator.validate_record(current["observation"], node)
            self.runtime(current["runtime"], current["observation"], workload, names, node, W)
            desired = baseline["records"][node]["services"]
            actual = current["observation"]["services"]
            for unit in M.UNITS:
                original_source = evidence["baseline_sources"][node]
                current_source = current["service_namespace"]
                self.unit_source(original_source, unit, desired[unit]["masked"], M)
                self.unit_source(current_source, unit, actual[unit]["masked"], M)
                for prefix in ("/usr/lib/systemd/system/", "/run/systemd/system/"):
                    need(original_source[prefix + unit] == current_source[prefix + unit], "service vendor/runtime source drift")
                if unit in M.PRESERVED:
                    adjusted = dict(actual[unit])
                    if unit == "pve-guests.service" and adjusted["masked"]:
                        adjusted["masked"] = desired[unit]["masked"]
                    need(adjusted == desired[unit], "preserved service drift")
                else:
                    need(not actual[unit]["active"] or desired[unit]["active"], "originally inactive service started")
                    need(not desired[unit]["masked"] or actual[unit]["masked"], "preexisting mask removed")
                if desired[unit]["masked"]:
                    need(original_source["/etc/systemd/system/" + unit] == current_source["/etc/systemd/system/" + unit],
                         "preexisting mask inode changed")
        inputs = evidence["certified_inputs"]
        exact(inputs, CERTIFIED_FIELDS, "ALL_CERTIFIED inputs")
        inputs = copy.deepcopy(inputs)
        for field in BYTE_FIELDS:
            need(type(inputs[field]) is str, "raw evidence must be exact UTF-8")
            inputs[field] = inputs[field].encode("utf-8")
        certified = A.evaluate(**inputs, now=now)
        context = inputs["context"]
        need(certified["tx"] == plan["tx"] and certified["nodes"] == names
             and certified["candidate"]["artifact_sha256"] == plan["candidate_sha256"]
             and context["baseline_storage_cfg_sha256"] == plan["storage_cfg_sha256"]
             and context["target_storage_cfg_sha256"] == plan["target_storage_cfg_sha256"], "composed identity/config differs")
        for row in plan["participants"]:
            node = row["node"]
            need(certified["participant_boots"][node] == row["boot_id"]
                 and certified["node_roles"][node] == ("SAN_PARTICIPANT" if row["role"] == "SAN" else "CONTROL_ONLY"),
                 "composed role/boot differs")
            installed = next(r["installed"] for r in inputs["records"] if r["node"] == node)
            need(installed["artifact_sha256"] == row["after_package_sha256"], "certified/current package differs")
        exact(evidence["archives"], certified["release_nodes"], "three SAN archives")
        archive_hashes = {}
        for node, archive in evidence["archives"].items():
            self.archive(archive, certified, node, now, A)
            archive_hashes[node] = digest(archive["receipt"])
            need(archive["receipt"]["finished_at"] <= evidence["current"][node]["observed_at"], "current observation predates archive")
        latest_archive = max(a["receipt"]["finished_at"] for a in evidence["archives"].values())
        need(all(c["observed_at"] >= latest_archive for c in evidence["current"].values()),
             "cohort observation predates complete SAN archive cohort")
        release = {"plan_sha256": digest(plan), "authority": "NONE", "all_certified_sha256": digest(certified),
                   "archive_sha256": archive_hashes}
        exact(request, {"tx", "plan_sha256", "node", "boot_id", "unit", "operation", "baseline_sha256", "release_sha256"}, "request")
        need(request["node"] in names and request["tx"] == plan["tx"] and request["plan_sha256"] == digest(plan)
             and request["baseline_sha256"] == digest(baseline) and request["release_sha256"] == digest(release)
             and request["boot_id"] == certified["participant_boots"][request["node"]]
             and request["unit"] in M.MASKED_BY_BARRIER and request["operation"] in ("UNMASK", "START"), "request binding")
        node, unit, operation = request["node"], request["unit"], request["operation"]
        for phase in M.PHASES + (("pve-guests.service",),):
            if unit in phase: break
            for participant in names:
                for earlier in phase:
                    need(evidence["current"][participant]["observation"]["services"][earlier]
                         == baseline["records"][participant]["services"][earlier], "earlier restore phase incomplete")
        original = baseline["records"][node]["services"][unit]
        current = evidence["current"][node]
        state = current["observation"]["services"][unit]
        source = self.unit_source(current["service_namespace"], unit, state["masked"], M)
        need(before == {"active": state["active"], "masked": state["masked"], "substate": state["substate"], "source": source},
             "local unit differs from fresh cohort")
        need(not original["masked"] and (operation != "START" or (original["active"] and unit in M.TARGETS)), "baseline forbids request")
        need(state["masked"] if operation == "UNMASK" else not state["active"] and not state["masked"], "request predecessor")
        need(state["active"] == original["active"] if unit == "pve-guests.service" else not state["active"], "request drain")
        need(type(executor) is dict and executor.get("schema") == "slt-local-restore-source/v1"
             and executor.get("authority") == "NONE"
             and executor.get("package", {}).get("package-artifact-sha256", {}).get("sha256")
             == current["observation"]["package_sha256"], "executor package differs")
        after_source = copy.deepcopy(source)
        if operation == "UNMASK":
            original_source = evidence["baseline_sources"][node]
            after_source["/etc/systemd/system/" + unit] = None
            after_source["effective:" + unit] = copy.deepcopy(original_source["effective:" + unit])
        binding = digest({"evidence_sha256": hashlib.sha256(raw).hexdigest(), "request": request, "executor": executor,
                          "before": before, "verifier_sha256": dict(self.code.hashes)})
        need(self.bound is None or self.bound == binding, "validator cannot change cohort/request")
        self.bound = binding
        need(self.code.loaded == set(FILES), "verifier closure not fully exercised")
        return {"schema": "slt-local-restore-explicit-grant/v1", "request_sha256": digest(request),
                "executor_sha256": digest(executor), "before_sha256": digest(before),
                "after_source_sha256": digest(after_source), "issued_at": evidence["issued_at"],
                "expires_at": evidence["expires_at"], "allowed_effect": operation, "authorization_sha256": binding}

    @staticmethod
    def file_identity(value, kind):
        need(type(value) is list and len(value) == 9 and all(type(v) is int and v >= 0 for v in value)
             and value[1] > 0 and kind(value[2]) and value[3] == 0 and value[5] == 1 and value[6] > 0,
             "source inode proof malformed")
        if kind is stat.S_ISREG:
            need(not value[2] & 0o022, "source inode writable")

    @staticmethod
    def runtime(raw, observation, workload, names, node, W):
        exact(raw, {"inventory_complete", "guests", "managed_mappers", "managed_open_lvs", "services", "ha_state"}, "runtime inputs")
        need(raw["inventory_complete"] is True and raw["managed_mappers"] == [] and raw["managed_open_lvs"] == []
             and raw["ha_state"] in ("DRAINED", "EMPTY_IDLE"), "runtime inventory unknown/nonempty")
        need(type(workload["consumers"]) is list and type(workload["config_sha256"]) is dict, "workload inventory malformed")
        expected = {}
        for consumer in workload["consumers"]:
            exact(consumer, {"vmid", "type", "name", "node", "status", "disks"}, "workload consumer")
            vmid = consumer["vmid"]
            need(type(vmid) is int and vmid > 0 and vmid not in expected and consumer["node"] in names
                 and consumer["type"] in ("qemu", "lxc") and consumer["status"] == "stopped", "workload consumer invalid")
            expected[vmid] = consumer
        need(type(raw["guests"]) is list, "runtime guests missing")
        seen, managed = set(), set()
        for guest in raw["guests"]:
            exact(guest, {"vmid", "type", "node", "status", "config"}, "runtime guest")
            vmid = guest["vmid"]
            need(type(vmid) is int and vmid > 0 and vmid not in seen and guest["node"] == node
                 and guest["type"] in ("qemu", "lxc") and guest["status"] in ("running", "stopped")
                 and type(guest["config"]) is str, "runtime guest identity")
            seen.add(vmid)
            disks = W.current_disks(guest["config"].encode(), set(workload["managed_storages"]), guest["type"])
            if not disks:
                need(vmid not in expected, "managed guest lost scope")
                continue
            need(vmid in expected and expected[vmid]["node"] == node and expected[vmid]["type"] == guest["type"]
                 and expected[vmid]["disks"] == disks and guest["status"] == "stopped"
                 and workload["config_sha256"].get(str(vmid)) == hashlib.sha256(guest["config"].encode()).hexdigest(),
                 "managed guest config/status differs")
            managed.add(vmid)
        need(managed == {v for v, c in expected.items() if c["node"] == node}, "managed guest coverage incomplete")
        need(type(raw["services"]) is list and len(raw["services"]) == len(observation["services"]), "runtime service coverage")
        derived = {}
        for row in raw["services"]:
            exact(row, {"unit", "active_state", "sub_state", "main_pid", "control_pid", "cgroup_empty", "job", "mask_target"}, "runtime service")
            unit = row["unit"]
            need(unit in observation["services"] and unit not in derived and row["active_state"] in ("active", "inactive")
                 and type(row["main_pid"]) is int and type(row["control_pid"]) is int and row["control_pid"] == 0
                 and type(row["cgroup_empty"]) is bool and row["job"] is None
                 and row["mask_target"] in (None, "/etc/systemd/system:/dev/null"), "runtime service identity")
            active = row["active_state"] == "active"
            if not active or unit == "pve-guests.service":
                need(row["main_pid"] == 0 and row["cgroup_empty"] is True, "drained unit retains executor")
            else:
                need(row["main_pid"] > 1 and row["cgroup_empty"] is False, "active executor missing")
            derived[unit] = {"active": active, "masked": row["mask_target"] is not None, "substate": row["sub_state"],
                             "job": row["job"], "control_pid": row["control_pid"]}
        need(derived == observation["services"], "service observation not derived from runtime")

    @staticmethod
    def unit_source(namespace, unit, masked, M):
        need(type(namespace) is dict and set(namespace) == {prefix + u for u in M.UNITS for prefix in
             ("/etc/systemd/system/", "/run/systemd/system/", "/usr/lib/systemd/system/", "effective:")}, "service namespace coverage")
        source = {prefix + unit: namespace[prefix + unit] for prefix in
                  ("/etc/systemd/system/", "/run/systemd/system/", "/usr/lib/systemd/system/", "effective:")}
        need(source["/run/systemd/system/" + unit] is None, "runtime override")
        vendor = source["/usr/lib/systemd/system/" + unit]
        exact(vendor, {"identity", "sha256"}, "vendor unit")
        need(M.sha(vendor["sha256"]), "vendor source missing")
        Validator.file_identity(vendor["identity"], stat.S_ISREG)
        mask = source["/etc/systemd/system/" + unit]
        if masked:
            exact(mask, {"mask_identity", "target"}, "mask")
            need(mask["target"] == "/dev/null" and type(mask["mask_identity"]) is list and mask["mask_identity"], "mask identity")
            Validator.file_identity(mask["mask_identity"], stat.S_ISLNK)
        else:
            need(mask is None, "foreign local override")
        effective = source["effective:" + unit]
        exact(effective, {"properties", "usrmerge_alias"}, "effective source")
        props = effective["properties"]
        exact(props, {"Id", "LoadState", "FragmentPath", "DropInPaths", "SourcePath", "UnitFileState", "Transient", "NeedDaemonReload"}, "effective properties")
        need(props["Id"] == unit and props["DropInPaths"] == props["SourcePath"] == ""
             and props["Transient"] == props["NeedDaemonReload"] == "no", "unsupported effective source")
        if masked:
            need(props["FragmentPath"] == "/dev/null" and props["LoadState"] == props["UnitFileState"] == "masked", "masked source differs")
        else:
            need(props["LoadState"] == "loaded" and props["UnitFileState"] in ("enabled", "disabled", "static", "indirect")
                 and props["FragmentPath"] == "/usr/lib/systemd/system/" + unit, "stock source differs")
        # Narrow initial closure: a /lib alias needs a separately qualified adapter.
        need(effective["usrmerge_alias"] is None, "usrmerge alias is not qualified by this release adapter")
        return source

    @staticmethod
    def archive(value, certified, node, now, A):
        exact(value, {"authorization", "attempt", "receipt", "current"}, "archive proof")
        auth, attempt, receipt, current = (value[k] for k in ("authorization", "attempt", "receipt", "current"))
        hold = certified["hold_by_node"][node]
        common = {"tx": certified["tx"], "generation": certified["generation"], "commit_id": certified["commit_id"],
                  "node": node, "boot_id": certified["participant_boots"][node],
                  "all_certified_plan_sha256": certified["plan_sha256"], "certificate_sha256": certified["certificate_sha256"],
                  "manifest_sha256": hold["manifest_sha256"]}
        exact(auth, set(common) | {"schema", "context_sha256", "source_identity_sha256", "certificate_identity_sha256",
              "operation_id", "issued_at", "expires_at", "effect"}, "archive authorization")
        need(all(auth[k] == v for k, v in common.items()) and auth["schema"] == "slt-live-hold-archive-authorization/v2"
             and auth["context_sha256"] == certified["context_sha256"] and auth["effect"] == "archive-exact-active-once"
             and auth["source_identity_sha256"] == digest(hold["source_identity"])
             and auth["certificate_identity_sha256"] == digest(hold["certificate_identity"])
             and type(auth["operation_id"]) is str and A.HEX32.fullmatch(auth["operation_id"]), "archive authorization identity")
        need(type(auth["issued_at"]) is int and type(auth["expires_at"]) is int
             and certified["latest_verification_finished_at"] <= auth["issued_at"] < auth["expires_at"] <= certified["release_not_after"]
             and auth["expires_at"] - auth["issued_at"] <= 900, "archive authorization time")
        name = f"{certified['tx']}-{certified['generation']}-{certified['commit_id']}.json"
        stable = {**common, "authorization_sha256": digest(auth), "source_identity": hold["source_identity"], "archive_name": name}
        exact(attempt, set(stable) | {"schema", "executor_pid", "executor_starttime", "started_at"}, "archive attempt")
        need(all(attempt[k] == v for k, v in stable.items()) and attempt["schema"] == "slt-layout-local-hold-release-attempt/v1"
             and type(attempt["executor_pid"]) is int and attempt["executor_pid"] > 1
             and type(attempt["executor_starttime"]) is int and attempt["executor_starttime"] > 0
             and type(attempt["started_at"]) is int and auth["issued_at"] <= attempt["started_at"] <= auth["expires_at"], "archive attempt differs")
        exact(receipt, set(stable) | {"schema", "attempt_sha256", "archive_identity", "rename_outcome", "directory_sync_complete",
              "started_at", "finished_at", "classification", "cluster_released"}, "archive receipt")
        need(all(receipt[k] == v for k, v in stable.items()) and receipt["schema"] == "slt-layout-local-hold-release-receipt/v1"
             and receipt["attempt_sha256"] == hashlib.sha256(canonical(attempt) + b"\n").hexdigest()
             and receipt["archive_identity"] == hold["source_identity"] and receipt["rename_outcome"] == "EXACT_ARCHIVE_CONFIRMED"
             and receipt["directory_sync_complete"] is True and receipt["classification"] == "LOCAL_HOLD_ARCHIVED"
             and receipt["cluster_released"] is False and receipt["started_at"] == attempt["started_at"]
             and type(receipt["finished_at"]) is int and receipt["started_at"] <= receipt["finished_at"] <= now, "archive receipt differs")
        exact(current, {"active_present", "archive_name", "archive_identity", "certificate_identity", "receipt_sha256", "observed_at"}, "archive readback")
        need(current["active_present"] is False and current["archive_name"] == name
             and current["archive_identity"] == hold["source_identity"] and current["certificate_identity"] == hold["certificate_identity"]
             and current["receipt_sha256"] == hashlib.sha256(canonical(receipt) + b"\n").hexdigest()
             and type(current["observed_at"]) is int and receipt["finished_at"] <= current["observed_at"] <= now
             and now - current["observed_at"] <= 30, "archive readback differs/stale")
