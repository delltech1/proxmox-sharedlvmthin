#!/usr/bin/env python3
"""Observe one caller-supplied native UPID; never dispatch or discover tasks.

CLI exit zero is not native async task success. This verifies task identity and
terminal status, not storage postconditions. Expected refusal still requires
independent unchanged-data/config verification before an operation can pass.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys

UPID = re.compile(r'UPID:([A-Za-z0-9_.-]+):([0-9A-Fa-f]{8}):([0-9A-Fa-f]{8,16}):([0-9A-Fa-f]{8}):([A-Za-z0-9_-]+):([1-9][0-9]*):([A-Za-z0-9_.@!+-]+):')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('non-finite JSON value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def binding(upid, node, task_type, vmid, user):
    match = UPID.fullmatch(upid)
    if not match:
        raise ValueError('invalid explicit VM task UPID')
    n, pid, pstart, start, kind, guest, owner = match.groups()
    if (n, kind, guest, owner) != (node, task_type, str(vmid), user):
        raise ValueError('requested identity does not match UPID')
    return dict(upid=upid, node=n, type=kind, id=guest, user=owner,
                pid=int(pid, 16), pstart=int(pstart, 16), starttime=int(start, 16))


def extract_dispatch_upid(raw, node, task_type, vmid, user):
    """One whole-line JSON UPID amid non-JSON tool diagnostics; never latest."""
    if not isinstance(raw, bytes) or not raw.endswith(b'\n'):
        raise ValueError('incomplete dispatch transcript')
    records = []
    for line in raw.decode('utf-8', errors='strict').splitlines():
        if not line.strip():
            continue
        try:
            record = strict_json(line)
        except ValueError:
            # Broken JSON/UPID is ambiguous, not ignorable diagnostic noise.
            if 'UPID' in line or line.lstrip().startswith(('"', '{', '[')):
                raise ValueError('ambiguous dispatch record')
            continue
        records.append(record)
    if len(records) != 1 or not isinstance(records[0], str):
        raise ValueError('dispatch requires exactly one JSON string record')
    return {'identity': binding(records[0], node, task_type, vmid, user),
            'raw_sha256': hashlib.sha256(raw).hexdigest()}


def status_checked(value, expected):
    if not isinstance(value, dict):
        raise ValueError('missing task status object')
    for key, wanted in expected.items():
        actual = value.get(key)
        if key == 'id' and type(actual) is int:
            actual = str(actual)
        if type(actual) is not type(wanted) or actual != wanted:
            raise ValueError('task identity mismatch: ' + key)
    if value.get('status') not in ('running', 'stopped'):
        raise ValueError('unknown task state')
    if value['status'] == 'stopped' and (not isinstance(value.get('exitstatus'), str)
                                        or not value['exitstatus']):
        raise ValueError('terminal exitstatus missing')
    return value


def fetch(path, *, log=False):
    argv = ['/usr/bin/pvesh', 'get', path, '--output-format', 'json']
    if log:
        argv += ['--start', '0', '--limit', '50']
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError('native read failed')
    return strict_json(result.stdout)


def observe(expected, refusal=None):
    path = '/nodes/' + expected['node'] + '/tasks/' + expected['upid']
    before = status_checked(fetch(path + '/status'), expected)
    if before['status'] != 'stopped':
        return {'result': 'UNKNOWN', 'reason': 'task still running'}, 2
    log = fetch(path + '/log', log=True)
    if not isinstance(log, list) or not log:
        raise ValueError('task log evidence missing')
    last = 0
    for row in log:
        if not isinstance(row, dict) or type(row.get('n')) is not int or row['n'] <= last \
                or not isinstance(row.get('t'), str):
            raise ValueError('invalid ordered log evidence')
        last = row['n']
    after = status_checked(fetch(path + '/status'), expected)
    if after['status'] != 'stopped' or after.get('exitstatus') != before['exitstatus']:
        raise ValueError('task changed during observation')
    wanted = refusal if refusal is not None else 'OK'
    accepted = before['exitstatus'] == wanted
    result = 'EXPECTED_REFUSAL' if accepted and refusal is not None else ('PASS' if accepted else 'FAIL')
    return {'result': result, 'identity': expected,
            'expected_exitstatus': wanted,
            'exitstatus_sha256': hashlib.sha256(before['exitstatus'].encode()).hexdigest(),
            'log_sample_sha256': hashlib.sha256(json.dumps(log, sort_keys=True).encode()).hexdigest(),
            'log_scope': 'first 50 lines, not complete transcript',
            'scope': 'native task only; storage postconditions independently required'}, 0 if accepted else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('upid', 'node', 'type', 'vmid', 'user'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--expect-refusal')
    args = parser.parse_args()
    expected = binding(args.upid, args.node, args.type, args.vmid, args.user)
    if args.expect_refusal is not None and (not args.expect_refusal or args.expect_refusal == 'OK'
            or len(args.expect_refusal) > 512 or any(ord(c) < 32 for c in args.expect_refusal)):
        raise ValueError('invalid exact refusal expectation')
    result, code = observe(expected, args.expect_refusal)
    print(json.dumps(result, sort_keys=True))
    if code == 0:
        print('PVE_NATIVE_TASK_' + result['result'])
    return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        # Never echo raw native stderr/logs, which may contain private details.
        print(json.dumps({'result': 'UNKNOWN', 'reason': type(error).__name__}))
        sys.exit(2)
