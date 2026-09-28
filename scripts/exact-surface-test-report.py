#!/usr/bin/python3
"""Run the catalogue-bound surface tests and emit a sealed result report."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import unittest


HEX_RE = re.compile(r"[a-f0-9]{64}\Z")
COMMIT_RE = re.compile(r"[A-Za-z0-9._+-]{7,128}\Z")


def strict_load(path):
    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=reject_duplicates)


def canonical_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def result_id(relative, identifier, kind, item):
    return f"{relative}::{identifier}[{kind}={item}]"


def run_one(root, relative, identifier, kind, item):
    path = (root / relative).resolve()
    path.relative_to(root.resolve())
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"unsafe test file: {relative}")
    module_name = "slt_surface_" + hashlib.sha256(
        f"{relative}:{identifier}".encode()
    ).hexdigest()
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"test module cannot be loaded: {relative}")
    module = importlib.util.module_from_spec(spec)
    old_kind = os.environ.get("SLT_EXACT_SURFACE_KIND")
    old_item = os.environ.get("SLT_EXACT_SURFACE_ITEM")
    os.environ["SLT_EXACT_SURFACE_KIND"] = kind
    os.environ["SLT_EXACT_SURFACE_ITEM"] = item
    try:
        spec.loader.exec_module(module)
        suite = unittest.defaultTestLoader.loadTestsFromName(identifier, module)
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    finally:
        if old_kind is None:
            os.environ.pop("SLT_EXACT_SURFACE_KIND", None)
        else:
            os.environ["SLT_EXACT_SURFACE_KIND"] = old_kind
        if old_item is None:
            os.environ.pop("SLT_EXACT_SURFACE_ITEM", None)
        else:
            os.environ["SLT_EXACT_SURFACE_ITEM"] = old_item
    passed = result.testsRun == 1 and result.wasSuccessful() \
        and not result.skipped and not result.expectedFailures \
        and not result.unexpectedSuccesses
    return "PASS" if passed else "FAIL"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--catalogue", required=True)
    parser.add_argument("--contract-fingerprint", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--plugin-commit", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.source_root).resolve()
    catalogue_path = pathlib.Path(args.catalogue).resolve()
    catalogue_path.relative_to(root)
    if not catalogue_path.is_file() or catalogue_path.is_symlink() \
            or not HEX_RE.fullmatch(args.contract_fingerprint) \
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.node) \
            or not COMMIT_RE.fullmatch(args.plugin_commit):
        raise SystemExit("unsafe report identity or catalogue")
    raw = catalogue_path.read_bytes()
    catalogue = strict_load(catalogue_path)
    surface = catalogue.get("source_dependency_coverage", {}).get(
        "exact_surface_tests", {}
    )
    results = []
    coverage = catalogue.get("source_dependency_coverage", {})
    for name in ("commands", "perl_symbols", "hooks"):
        binding = surface.get(name)
        if not isinstance(binding, dict) \
                or binding.get("framework") != "python-unittest":
            raise SystemExit(f"unsupported surface binding: {name}")
        relative, identifier = binding.get("file"), binding.get("id")
        if not isinstance(relative, str) or not isinstance(identifier, str):
            raise SystemExit(f"malformed surface binding: {name}")
        items = coverage.get(name)
        if not isinstance(items, dict) or not items:
            raise SystemExit(f"empty exact surface: {name}")
        for item in sorted(items):
            results.append({
                "id": result_id(relative, identifier, name, item),
                "status": run_one(root, relative, identifier, name, item),
            })
    report = {
        "kind": "exact-surface-test-report", "schema": 1,
        "catalogue_sha256": hashlib.sha256(raw).hexdigest(),
        "contract_fingerprint": args.contract_fingerprint,
        "node": args.node, "plugin_commit": args.plugin_commit,
        "results": sorted(results, key=lambda row: row["id"]),
    }
    document = {"report": report, "sha256": canonical_digest(report)}
    rendered = json.dumps(document, sort_keys=True, indent=2) + "\n"
    if args.output:
        output = pathlib.Path(args.output)
        temporary = output.with_name(f"{output.name}.tmp.{os.getpid()}")
        with open(temporary, "x", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    sys.stdout.write(rendered)
    return 0 if all(row["status"] == "PASS" for row in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
