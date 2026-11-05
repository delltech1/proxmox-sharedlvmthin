"""In-memory CLOSED-to-CLOSED ordering model, not a recovery implementation.

This model has no dispatcher, persistence backend, host probes or authority.
Its observations are caller-supplied symbols, not evidence of worker absence,
package identity or storage safety. Never use its result to admit real work.
"""
import copy
import hashlib
import json
import re


class Refused(ValueError):
    pass


def digest(value):
    return hashlib.sha256((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()


def closed(value, boot):
    return (isinstance(value, dict) and set(value) == {"state", "authority", "txid", "boot_id", "payload"}
            and value["state"] == "CLOSED" and value["authority"] == "NONE"
            and isinstance(value["txid"], str) and re.fullmatch(r"[0-9a-f]{32}", value["txid"])
            and value["boot_id"] == boot and isinstance(value["payload"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["payload"]))


def reconcile(store, proposal, *, boot_id, effects):
    """Advance one exact synthetic transaction; store writes may lose their ACK.

    store supplies get(key) and write(key, value). Every write is treated as
    atomic, but may persist and then raise. Non-atomic/torn filesystem writes
    and true process/cluster fencing are outside this model.
    """
    def need(ok, why):
        if not ok:
            raise Refused(why)

    need(isinstance(proposal, dict) and set(proposal) == {
        "node", "old_boot", "new_boot", "old_latch", "new_latch", "old_release", "new_release",
        "old_ledger", "new_ledger"}, "proposal shape")
    old_boot, new_boot = proposal["old_boot"], proposal["new_boot"]
    uuid = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    need(all(isinstance(v, str) and re.fullmatch(uuid, v) for v in (old_boot, new_boot))
         and old_boot != new_boot and boot_id == new_boot, "boot changed or invalid")
    need(isinstance(proposal["node"], str) and bool(proposal["node"]), "node missing")
    old, new = proposal["old_latch"], proposal["new_latch"]
    need(closed(old, old_boot) and closed(new, new_boot) and old["txid"] != new["txid"],
         "latch identity is not closed")
    for prefix, latch in (("old", old), ("new", new)):
        release = proposal[prefix + "_release"]
        ledger = proposal[prefix + "_ledger"]
        need(release == {"qualified": "UNQUALIFIED", "latch_sha256": digest(latch)},
             "release does not bind closed latch")
        need(isinstance(ledger, dict) and set(ledger) == {"phase", "generation", "payload"}
             and ledger["phase"] == "ACTIVE" and isinstance(ledger["generation"], str)
             and re.fullmatch(r"[0-9a-f]{32}", ledger["generation"])
             and ledger["payload"] == latch["payload"], "ledger mismatch")
    need(proposal["old_ledger"]["generation"] != proposal["new_ledger"]["generation"],
         "ledger generation did not advance")
    expected = {"model": 1, "authority": "NONE", "proposal": copy.deepcopy(proposal),
                "proposal_sha256": digest(proposal)}
    current = store.get("intent")
    need(current is None or current == expected, "intent fork")

    # Only ordered prefixes are valid. A persisted new latch remains closed even
    # while the matching non-authorizing release or ledger has yet to follow.
    old_tuple = (proposal["old_latch"], proposal["old_release"], proposal["old_ledger"])
    new_tuple = (proposal["new_latch"], proposal["new_release"], proposal["new_ledger"])
    actual = tuple(store.get(k) for k in ("latch", "release", "ledger"))
    prefixes = [old_tuple, (new_tuple[0], old_tuple[1], old_tuple[2]),
                (new_tuple[0], new_tuple[1], old_tuple[2]), new_tuple]
    need(actual in prefixes, "records do not match an ordered prefix")
    index = prefixes.index(actual)
    need(index == 0 or current == expected, "handoff lacks its intent")
    done = store.get("complete")
    complete = {"model": 1, "authority": "NONE", "intent_sha256": digest(expected)}
    need(done is None or (done == complete and index == 3), "completion fork")
    need(effects in {"NO_DISPATCH_PROVEN", "EXACT_TARGET_CONFIRMED"}, "previous effect is unknown")
    if effects == "NO_DISPATCH_PROVEN":
        need(index == 0 and done is None, "no-dispatch observation conflicts with handoff")
        if current is None:
            store.write("intent", expected)
        return "CLOSED_ONLY_INTENT_RECORDED"
    need(current == expected, "target evidence without prior durable intent")
    for i, key in enumerate(("latch", "release", "ledger")):
        if index <= i:
            store.write(key, new_tuple[i])
    if done is None:
        store.write("complete", complete)
    return "CLOSED_ONLY_MODEL_COMPLETE"
