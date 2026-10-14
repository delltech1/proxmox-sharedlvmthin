#!/usr/bin/env python3
"""Inert by default. Linux orchestration only; adapters own live safety proofs.

Polling never classifies a slow child as failed. An optional command deadline
means UNKNOWN, not cancellation: supervision continues until the child exits,
without verification or further dispatch. Resume never re-executes.
Commands must be synchronous adapters; detached work requires an adapter that
waits for its exact native task. No generic argv can establish storage safety.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError('non-finite JSON constant: ' + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def validate(plan):
    require(plan.get('schema') == 1, 'unsupported schema')
    identity = plan['identity']
    require(identity['profile'] in ('dual', 'thick-only'), 'invalid profile')
    for key in ('artifact_sha256', 'platform_sha256'):
        require(re.fullmatch('[0-9a-f]{64}', identity[key]), 'invalid identity digest')
    for key in ('package', 'version', 'platform'):
        require(isinstance(identity[key], str) and identity[key].strip(), 'missing exact identity')
    ids = plan['disposable_ids']
    require(isinstance(ids, list) and ids and all(type(i) is int and i > 0 for i in ids)
            and len(set(ids)) == len(ids), 'explicit unique disposable IDs required')
    coverage = plan['mandatory_coverage']
    require(isinstance(coverage, list) and coverage and
            all(isinstance(c, str) and re.fullmatch('[A-Za-z0-9_.-]+', c) for c in coverage)
            and len(set(coverage)) == len(coverage), 'invalid mandatory coverage')
    steps = plan['steps']
    require(isinstance(steps, list) and steps, 'no adapter steps supplied')
    seen = set(); provided = set()
    for step in steps:
        name = step['id']
        require(isinstance(name, str) and re.fullmatch('[A-Za-z0-9_-]+', name)
                and name not in seen, 'invalid/duplicate step ID')
        seen.add(name)
        for key in ('argv', 'verify_argv'):
            argv = step[key]
            require(isinstance(argv, list) and argv and all(isinstance(a, str) and '\0' not in a for a in argv)
                    and os.path.isabs(argv[0]), 'adapter argv requires absolute executable, no shell expansion')
        marker = step['success_marker']
        require(isinstance(marker, str) and marker and not any(c in marker for c in '\r\n\0'), 'invalid exact marker')
        require('observation_seconds' not in step, 'replace observation_seconds with explicit poll/deadline fields')
        timeout = step['poll_seconds']
        require(type(timeout) in (int, float) and math.isfinite(timeout)
                and 0 < timeout <= 86400, 'invalid poll interval')
        deadline = step.get('command_deadline_seconds')
        require(deadline is None or (type(deadline) in (int, float) and math.isfinite(deadline)
                and 0 < deadline <= 86400), 'invalid command deadline')
        require(isinstance(step['coverage'], list) and all(c in coverage for c in step['coverage']), 'unknown coverage')
        provided.update(step['coverage'])
    require(provided == set(coverage), 'mandatory coverage has no adapter')
    return plan


def identity(pid):
    stat = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
    return {'pid': pid, 'starttime': int(stat[19]),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


class Journal:
    def __init__(self, root):
        self.root = root
        self.sequence = 0

    def write(self, kind, value):
        self.sequence += 1
        path = self.root / f'{self.sequence:05d}-{kind}.json'
        with path.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def run_adapter(argv, step, phase, journal):
    journal.write('dispatch-intent', {'step': step['id'], 'phase': phase, 'argv': argv})
    stdout = journal.root / f"{step['id']}-{phase}.stdout"
    stderr = journal.root / f"{step['id']}-{phase}.stderr"
    with stdout.open('xb') as out, stderr.open('xb') as err:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                  '_worker', json.dumps(argv)], stdin=subprocess.PIPE,
                                 stdout=out, stderr=err, bufsize=0)
        try:
            process_identity = identity(child.pid)
            journal.write('process', {'step': step['id'], 'phase': phase,
                                     'identity': process_identity})
            started = time.monotonic()
            require(os.write(child.stdin.fileno(), b'GO\n') == 3, 'partial GO handoff')
            child.stdin.close()
            deadline = step.get('command_deadline_seconds')
            while True:
                remaining = None if deadline is None else deadline - (time.monotonic() - started)
                if remaining is not None and remaining <= 0:
                    journal.write('UNKNOWN', {'step': step['id'], 'phase': phase,
                                  'reason': 'explicit command deadline; still supervising, no retry/kill'})
                    print('UNKNOWN: command deadline; waiting for recorded child, no further dispatch', flush=True)
                    code = child.wait()
                    journal.write('observed-exit', {'step': step['id'], 'phase': phase, 'exit': code})
                    return 'UNKNOWN', stdout
                try:
                    code = child.wait(timeout=min(step['poll_seconds'], remaining) if remaining is not None else step['poll_seconds'])
                    if deadline is not None and time.monotonic() - started >= deadline:
                        journal.write('UNKNOWN', {'step': step['id'], 'phase': phase,
                                      'reason': 'terminal first observed after explicit deadline', 'exit': code})
                        return 'UNKNOWN', stdout
                    break
                except subprocess.TimeoutExpired:
                    # A just-exited but unreaped child retains its /proc identity.
                    require(identity(child.pid) == process_identity, 'tracked child identity changed')
                    journal.write('STILL_RUNNING', {'step': step['id'], 'phase': phase,
                                  'identity': process_identity, 'elapsed_seconds': time.monotonic() - started})
                    print(f"STILL_RUNNING: step={step['id']} phase={phase}; waiting for the same tracked child", flush=True)
            journal.write('exit', {'step': step['id'], 'phase': phase, 'exit': code})
            return ('PASS' if code == 0 else 'FAIL'), stdout
        finally:
            # Always reap, including pre-GO journal failure and post-GO interrupt.
            # Unbuffered pipe closure cannot flush a previously buffered grant.
            # No journal operation is permitted to interrupt this supervision.
            while child.stdin and not child.stdin.closed:
                try:
                    child.stdin.close()
                except BaseException:
                    continue
            while True:
                try:
                    child.wait()
                    break
                except KeyboardInterrupt:
                    continue
                except BaseException:
                    # Unknown wait failure is not proof the child has stopped.
                    try:
                        time.sleep(.1)
                    except BaseException:
                        pass


def result_report(plan, digest, states, ambiguous=False):
    status = 'UNKNOWN' if ambiguous or 'UNKNOWN' in states.values() else (
        'PASS' if all(v == 'PASS' for v in states.values()) else 'FAIL')
    coverage = {}
    for item in plan['mandatory_coverage']:
        relevant = [states[step['id']] for step in plan['steps'] if item in step['coverage']]
        coverage[item] = 'UNKNOWN' if 'UNKNOWN' in relevant else (
            'FAIL' if 'FAIL' in relevant else 'NOT_RUN' if 'NOT_RUN' in relevant else 'PASS')
    return {'status': status, 'steps': states, 'coverage': coverage,
            'plan_sha256': digest, 'identity': plan['identity'],
            'scope': 'adapter attestations only; no inferred live coverage'}


def execute(plan, digest, root):
    require(sys.platform.startswith('linux') and Path('/proc/self/stat').is_file(), 'execute requires Linux /proc identities')
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    journal = Journal(root)
    states = {step['id']: 'NOT_RUN' for step in plan['steps']}
    current = None
    ambiguous = False
    try:
        journal.write('plan', {'sha256': digest, 'plan': plan, 'runner': identity(os.getpid())})
        for step in plan['steps']:
            current = step['id']
            states[current] = 'UNKNOWN'
            print('STEP_STARTED: ' + current, flush=True)
            status, _ = run_adapter(step['argv'], step, 'command', journal)
            if status == 'PASS':
                status, output = run_adapter(step['verify_argv'], step, 'verify', journal)
                if status == 'PASS':
                    lines = output.read_text(errors='strict').splitlines()
                    if lines.count(step['success_marker']) != 1:
                        status = 'FAIL'
            states[current] = status
            journal.write('step-result', {'step': current, 'status': status})
            print('STEP_RESULT: ' + current + ' ' + status, flush=True)
            if status != 'PASS':
                break
    except BaseException as error:
        ambiguous = True
        if current is not None:
            states[current] = 'UNKNOWN'
        journal.write('UNKNOWN', {'reason': type(error).__name__ + ': ' + str(error)})
    report = result_report(plan, digest, states, ambiguous)
    journal.write('summary', report)
    print(json.dumps(report))
    return 0 if report['status'] == 'PASS' else 2


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '_worker':
        if sys.stdin.buffer.readline() != b'GO\n':
            return 3
        argv = strict_json(sys.argv[2])
        os.execv(argv[0], argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    require(not (args.execute and args.resume), 'resume cannot execute')
    raw = args.plan.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    plan = validate(strict_json(raw))
    if args.resume:
        print('REFUSED: read existing journal/process identities; automatic resume or retry is not implemented')
        return 2
    if not args.execute:
        print(json.dumps({'status': 'NOT_RUN', 'plan_sha256': digest,
                          'steps': len(plan['steps']), 'scope': 'syntax/coverage declaration only, not live admission'}))
        return 0
    require(args.plan_sha256 == digest, 'exact plan SHA256 required')
    require(args.run_dir is not None and args.run_dir.is_absolute(), 'absolute unique run directory required')
    return execute(plan, digest, args.run_dir)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError, OSError) as error:
        print('REFUSED: ' + str(error), file=sys.stderr)
        sys.exit(2)
