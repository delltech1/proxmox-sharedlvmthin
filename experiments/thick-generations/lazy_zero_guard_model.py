#!/usr/bin/python3
"""Pure lazy-zero guard/publication epoch model; no OS or storage backend."""

import copy
import hashlib
import json
import re


LIMITS = {
    "runtime_authorized": False,
    "production_authorized": False,
    "shared_owner_qualified": False,
    "power_loss_qualified": False,
    "linear_pivot_qualified": False,
}
RECONFIGURE_KINDS = frozenset({"RELOAD", "RESUME", "REOPEN"})


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def exact_int(value, minimum=0):
    return type(value) is int and value >= minimum


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def _hex32(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _devno(value):
    return type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", value) is not None


def _validate_loop(role, value, size):
    keys = {"kind", "role", "devno", "diskseq", "size_bytes",
            "backing_dev", "backing_inode", "offset", "sizelimit",
            "readonly", "holders"}
    require(type(value) is dict and set(value) == keys, role + " schema")
    require(value["kind"] == "loop" and value["role"] == role, role + " role")
    require(_devno(value["devno"]) and exact_int(value["diskseq"], 1), role + " identity")
    require(type(value["size_bytes"]) is int and value["size_bytes"] == size
            and value["readonly"] is False,
            role + " geometry")
    require(all(exact_int(value[key], 1) for key in ("backing_dev", "backing_inode")),
            role + " backing")
    require(type(value["offset"]) is int and value["offset"] == 0
            and type(value["sizelimit"]) is int and value["sizelimit"] == 0,
            role + " mapping")
    require(type(value["holders"]) is list
            and all(type(item) is str for item in value["holders"])
            and len(value["holders"]) == len(set(value["holders"])), role + " holders")


def _validate_dm(role, value):
    keys = {"kind", "role", "devno", "diskseq", "size_bytes", "name",
            "uuid", "table", "readonly", "suspended", "open_count",
            "holders", "dependencies"}
    require(type(value) is dict and set(value) == keys, role + " schema")
    require(value["kind"] == "dm" and value["role"] == role, role + " role")
    require(_devno(value["devno"]) and exact_int(value["diskseq"], 1), role + " identity")
    require(type(value["size_bytes"]) is int
            and value["size_bytes"] == 134217728, role + " geometry")
    require(type(value["name"]) is str and type(value["uuid"]) is str,
            role + " naming")
    require(type(value["table"]) is str and type(value["readonly"]) is bool
            and type(value["suspended"]) is bool
            and exact_int(value["open_count"]), role + " state")
    require(type(value["holders"]) is list and type(value["dependencies"]) is list
            and all(type(item) is str for item in value["holders"] + value["dependencies"])
            and len(value["holders"]) == len(set(value["holders"])),
            role + " graph")


def validate_fixture(value):
    keys = {"schema", "nonce", "boot_id", "purpose", "region_sectors", "roles"}
    require(type(value) is dict and set(value) == keys, "fixture schema")
    require(type(value["schema"]) is int and value["schema"] == 1
            and _hex32(value["nonce"]), "fixture identity")
    require(type(value["boot_id"]) is str
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                             value["boot_id"]) is not None,
            "boot identity")
    require(value["purpose"] in ("GUARDED", "NEGATIVE_L2"), "fixture purpose")
    require(type(value["region_sectors"]) is int
            and value["region_sectors"] == 2048, "region geometry")
    roles = value["roles"]
    require(type(roles) is dict and set(roles) == {"data", "metadata", "zero", "clone"},
            "role set")
    _validate_loop("data", roles["data"], 134217728)
    _validate_loop("metadata", roles["metadata"], 33554432)
    _validate_dm("zero", roles["zero"])
    _validate_dm("clone", roles["clone"])
    devnos = [roles[name]["devno"] for name in ("data", "metadata", "zero", "clone")]
    require(len(devnos) == len(set(devnos)), "role devnos alias")
    backings = [(roles[name]["backing_dev"], roles[name]["backing_inode"])
                for name in ("data", "metadata")]
    require(len(set(backings)) == 2, "loop backing alias")
    zero = roles["zero"]
    clone = roles["clone"]
    require(zero["name"] == "slt-lazy-zero-" + value["nonce"]
            and zero["uuid"] == "SLT-LAZY-ZERO-" + value["nonce"],
            "zero naming")
    require(clone["name"] == "slt-lazy-clone-" + value["nonce"]
            and clone["uuid"] == "SLT-LAZY-CLONE-" + value["nonce"],
            "clone naming")
    require(zero["table"] == "0 262144 zero" and zero["readonly"] is True
            and zero["suspended"] is False and zero["open_count"] == 0
            and zero["dependencies"] == [], "zero target semantics")
    expected = ("0 262144 clone " + roles["metadata"]["devno"] + " "
                + roles["data"]["devno"] + " " + zero["devno"]
                + " 2048 2 no_hydration no_discard_passdown")
    require(clone["table"] == expected and clone["readonly"] is False
            and clone["suspended"] is False and clone["open_count"] == 0,
            "clone target semantics")
    require(clone["dependencies"] == [roles["data"]["devno"],
                                      roles["metadata"]["devno"], zero["devno"]],
            "clone dependencies")
    require(clone["holders"] == []
            and roles["data"]["holders"] == [clone["devno"]]
            and roles["metadata"]["holders"] == [clone["devno"]]
            and zero["holders"] == [clone["devno"]], "holder graph")
    return copy.deepcopy(value)


def graph_digest(fixture):
    return hashlib.sha256(_canonical(fixture)).hexdigest()


class GuardEpochModel:
    def __init__(self, fixture):
        self.fixture = validate_fixture(fixture)
        self.digest = graph_digest(self.fixture)
        self.epoch = 1
        self.state = "GRAPH_VERIFIED"
        self.guard = None
        self.was_withdrawn = False

    def _poison(self, message):
        self.state = "UNKNOWN"
        self.guard = None
        raise Refusal(message)

    def invalidate(self):
        """Irreversibly remove model authority after outer protocol ambiguity."""
        self.state = "UNKNOWN"
        self.guard = None

    def accept_guard(self, receipt):
        require(self.state == "GRAPH_VERIFIED", "guard state")
        keys = {"schema", "fixture_nonce", "boot_id", "epoch", "graph_digest",
                "clone_devno", "clone_diskseq", "clone_table", "queue_devno",
                "queue_diskseq", "discard_max_bytes", "attempt_id"}
        if type(receipt) is not dict or set(receipt) != keys:
            self._poison("guard schema")
        clone = self.fixture["roles"]["clone"]
        valid = (type(receipt["schema"]) is int and receipt["schema"] == 1
                 and receipt["fixture_nonce"] == self.fixture["nonce"]
                 and receipt["boot_id"] == self.fixture["boot_id"]
                 and type(receipt["epoch"]) is int and receipt["epoch"] == self.epoch
                 and receipt["graph_digest"] == self.digest
                 and receipt["clone_devno"] == clone["devno"]
                 and type(receipt["clone_diskseq"]) is int
                 and receipt["clone_diskseq"] == clone["diskseq"]
                 and receipt["clone_table"] == clone["table"]
                 and receipt["queue_devno"] == clone["devno"]
                 and type(receipt["queue_diskseq"]) is int
                 and receipt["queue_diskseq"] == clone["diskseq"]
                 and type(receipt["discard_max_bytes"]) is int
                 and receipt["discard_max_bytes"] == 0
                 and _hex32(receipt["attempt_id"]))
        if not valid:
            self._poison("guard identity or epoch mismatch")
        self.guard = copy.deepcopy(receipt)
        self.state = "GUARD_VERIFIED"

    def publish(self):
        require(self.state == "GUARD_VERIFIED", "publication state")
        require(self.fixture["purpose"] == "GUARDED", "negative fixture cannot publish")
        self.state = "MODEL_PUBLISHED"
        return {"classification": "MODEL_GUARD_PUBLICATION_ORDER_VALID", **LIMITS}

    def withdraw(self, terminal, open_count):
        require(self.state == "MODEL_PUBLISHED", "withdraw state")
        require(terminal is True and type(open_count) is int and open_count == 0,
                "terminal zero-open proof")
        self.state = "UNPUBLISHED"
        self.was_withdrawn = True

    def reconfigure(self, kind, fixture, terminal, open_count):
        require(kind in RECONFIGURE_KINDS, "reconfigure kind")
        require(self.state == "UNPUBLISHED" and self.was_withdrawn,
                "reconfigure requires completed withdrawal")
        require(terminal is True and type(open_count) is int and open_count == 0,
                "reconfigure requires terminal zero-open proof")
        self.epoch += 1
        self.guard = None
        try:
            observed = validate_fixture(fixture)
            require(observed["nonce"] == self.fixture["nonce"]
                    and observed["boot_id"] == self.fixture["boot_id"]
                    and observed["purpose"] == self.fixture["purpose"],
                    "fixture continuity changed")
            if kind in ("RELOAD", "RESUME"):
                require(observed == self.fixture,
                        "reload/resume must preserve the exact graph")
            if kind == "REOPEN":
                for role in ("data", "metadata"):
                    require(observed["roles"][role] == self.fixture["roles"][role],
                            "reopen " + role + " identity changed")
                require(observed["roles"]["zero"] == self.fixture["roles"]["zero"],
                        "reopen zero-source identity changed")
        except BaseException:
            self.state = "UNKNOWN"
            raise
        self.fixture = observed
        self.digest = graph_digest(observed)
        self.state = "GRAPH_VERIFIED"
        self.was_withdrawn = False
