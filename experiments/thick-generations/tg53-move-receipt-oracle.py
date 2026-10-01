#!/usr/bin/env python3
"""Fail-closed oracle for one disposable PVE move_disk experiment."""

import argparse
import json
import re
import sys
from pathlib import Path


def load(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def volume_ids(rows):
    if not isinstance(rows, list):
        raise ValueError("inventory is not a JSON array")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("volid"), str):
            raise ValueError("inventory contains a row without an exact volid")
        result.append(row["volid"])
    if len(result) != len(set(result)):
        raise ValueError("inventory contains duplicate volids")
    return sorted(result)


def disk_value(config, disk):
    if not isinstance(config, dict) or not isinstance(config.get(disk), str):
        raise ValueError(f"configuration has no exact {disk} value")
    return config[disk]


def terminal_result(status):
    if not isinstance(status, dict):
        raise ValueError("task status is not a JSON object")
    upid = status.get("upid", "")
    if not isinstance(upid, str) or not re.fullmatch(
            r"UPID:[A-Za-z0-9][A-Za-z0-9.-]*:[^\r\n]+:", upid):
        raise ValueError("task status has no exact UPID")
    if status.get("status") != "stopped":
        raise ValueError("task is not terminal")
    result = status.get("exitstatus")
    if not isinstance(result, str) or not result.strip():
        raise ValueError("terminal task has no exitstatus")
    return upid, result.strip()


def evaluate(args):
    before = load(args.config_before)
    after = load(args.config_after)
    source_before = volume_ids(load(args.source_before))
    source_after = volume_ids(load(args.source_after))
    target_before = volume_ids(load(args.target_before))
    target_after = volume_ids(load(args.target_after))
    upid, result = terminal_result(load(args.task_status))
    old_value = disk_value(before, args.disk)
    new_value = disk_value(after, args.disk)
    if not old_value.startswith(args.source_volid + ",") and old_value != args.source_volid:
        raise ValueError("source volid does not match the pre-dispatch VM configuration")
    if args.source_volid not in source_before:
        raise ValueError("source volid was not present before dispatch")

    if args.expected == "REFUSED":
        if result == "OK":
            raise ValueError("expected admission refusal but task succeeded")
        if not args.refusal_regex:
            raise ValueError("REFUSED requires a nonempty exact admission reason regex")
        try:
            matched = re.search(args.refusal_regex, result)
        except re.error as exc:
            raise ValueError(f"invalid refusal regex: {exc}") from exc
        if not matched:
            raise ValueError("task failure is not the expected admission refusal")
        if new_value != old_value:
            raise ValueError("VM disk configuration changed during refused move")
        if source_after != source_before:
            raise ValueError("source inventory changed during refused move")
        if target_after != target_before:
            raise ValueError("target inventory changed during refused move")
        verdict = "REFUSED_BEFORE_EFFECT"
    else:
        if result != "OK":
            raise ValueError(f"move did not succeed: {result}")
        if not new_value.startswith(args.target_storage + ":"):
            raise ValueError("successful move did not publish the target storage")
        if args.source_volid in source_after:
            raise ValueError("successful delete-source move retained its source volid")
        target_volid = new_value.split(",", 1)[0]
        if target_volid not in target_after:
            raise ValueError("published target volid is absent from target inventory")
        verdict = "OK_WITH_SETTLED_INVENTORY"
    return upid, result, verdict


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--expected", choices=("OK", "REFUSED"), required=True)
    result.add_argument("--refusal-regex", default="")
    result.add_argument("--disk", required=True)
    result.add_argument("--source-volid", required=True)
    result.add_argument("--target-storage", required=True)
    for name in ("config-before", "config-after", "source-before", "source-after",
                 "target-before", "target-after", "task-status"):
        result.add_argument("--" + name, required=True)
    return result


def main():
    args = parser().parse_args()
    try:
        upid, result, verdict = evaluate(args)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"MOVE_ORACLE=UNKNOWN: {exc}", file=sys.stderr)
        return 2
    print(f"MOVE_UPID={upid}")
    print(f"MOVE_STATUS={result}")
    print(f"MOVE_ORACLE={verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
