#!/usr/bin/env python3
"""Single challenged exchange with the disposable read-only guest protocol.

No retries, disk writes, VM lifecycle calls or implicit safe admission. Caller
must prove VM/socket/process ownership before connecting. Timeout is UNKNOWN.
SET_RAM changes only the guest protocol's in-memory nonce.
"""
import argparse
import json
import re
import secrets
import socket
import time
from pathlib import Path


class Unknown(RuntimeError):
    pass


def wait_ready(connection, seconds=90, max_bytes=262144):
    """Receive-only. Never feed a probe/newline to firmware or a bootloader."""
    if not 0 < seconds <= 3600:
        raise ValueError('invalid readiness observation interval')
    end = time.monotonic() + seconds
    pending = b''; received = 0
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise Unknown('agent READY not observed; no request dispatched')
        connection.settimeout(remaining)
        try:
            data = connection.recv(4096)
        except socket.timeout as error:
            raise Unknown('agent READY not observed; no request dispatched') from error
        if not data:
            raise Unknown('console disconnected before READY; no request dispatched')
        received += len(data)
        if received > max_bytes:
            raise Unknown('readiness byte limit exceeded; no request dispatched')
        pending += data
        while b'\n' in pending:
            raw, pending = pending.split(b'\n', 1)
            if raw.rstrip(b'\r') == b'SLT_READONLY_GUEST_PROTOCOL=1':
                return {'status': 'READY_OBSERVED', 'request_dispatched': False,
                        'scope': 'owned-agent greeting only; caller must prove console/process binding'}


def request(command, arguments, challenge):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', challenge):
        raise ValueError('invalid challenge')
    if command in ('SYSINFO', 'GET_RAM'):
        if arguments:
            raise ValueError('unexpected arguments')
    elif command == 'SET_RAM':
        if len(arguments) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', arguments[0]):
            raise ValueError('invalid RAM nonce')
    elif command == 'VERIFY':
        if len(arguments) != 3 or not re.fullmatch(r'(vd|sd)[a-z]{1,10}', arguments[0]):
            raise ValueError('invalid approved whole disk')
        offset = arguments[1]
        if not re.fullmatch(r'(0|[1-9][0-9]{0,14})', offset) or int(offset) % 4096:
            raise ValueError('invalid aligned offset')
        if not re.fullmatch(r'[0-9a-f]{64}', arguments[2]):
            raise ValueError('invalid expected SHA256')
    else:
        raise ValueError('unsupported command')
    return (' '.join([command, challenge, *arguments]) + '\n').encode('ascii')


def response(command, arguments, challenge, line):
    """Return completion, ignoring only unrelated console/challenge lines."""
    fields = line.split(' ')
    if len(fields) < 2 or fields[1] != challenge:
        return False
    if fields[0] == 'ERROR':
        raise Unknown('guest refused challenged request: ' + line)
    if command == 'SYSINFO':
        if fields[0] == 'SYSINFO_END' and len(fields) == 2:
            return True
        if fields[0] in ('SYSINFO', 'BLOCK', 'NIC'):
            return False
        raise Unknown('unexpected challenged SYSINFO response')
    expected = {'SET_RAM': ['RAM_SET', challenge, *arguments],
                'VERIFY': ['VERIFY_OK', challenge, *arguments]}
    if command == 'GET_RAM':
        if len(fields) == 3 and fields[:2] == ['RAM', challenge] and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', fields[2]):
            return True
    elif fields == expected[command]:
        return True
    raise Unknown('unexpected challenged response')


def exchange(connection, command, arguments, challenge, seconds=30, max_bytes=262144):
    payload = request(command, arguments, challenge)
    if not 0 < seconds <= 3600:
        raise ValueError('invalid observation interval')
    end = time.monotonic() + seconds
    connection.settimeout(seconds)
    # Exactly one send. An exception means UNKNOWN, never retransmission.
    connection.sendall(payload)
    pending = b''; lines = []; received = 0; sysinfo_started = False
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise Unknown('guest observation expired; no request retry')
        connection.settimeout(remaining)
        try:
            data = connection.recv(4096)
        except socket.timeout as error:
            raise Unknown('guest observation expired; no request retry') from error
        if not data:
            raise Unknown('console disconnected before challenged completion')
        received += len(data)
        if received > max_bytes:
            raise Unknown('console observation byte limit exceeded')
        pending += data
        while b'\n' in pending:
            raw, pending = pending.split(b'\n', 1)
            try:
                line = raw.rstrip(b'\r').decode('ascii')
            except UnicodeDecodeError as error:
                # Firmware banners may use non-ASCII terminal graphics. They
                # are not protocol evidence. A matching challenge must remain
                # strict ASCII; never repair its response with replacement.
                if challenge.encode('ascii') in raw:
                    raise Unknown('invalid challenged console encoding') from error
                continue
            fields = line.split(' ')
            if len(fields) > 1 and fields[1] == challenge:
                lines.append(line)
                if command == 'SYSINFO' and fields[0] == 'SYSINFO':
                    if sysinfo_started or len(fields) != 6 or fields[2] != 'BOOT' or fields[4] != 'EFI' or fields[5] not in ('YES', 'NO') or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', fields[3]):
                        raise Unknown('invalid/duplicate SYSINFO header')
                    sysinfo_started = True
                elif command == 'SYSINFO' and fields[0] in ('BLOCK', 'NIC'):
                    if not sysinfo_started or len(fields) != 6:
                        raise Unknown('inventory before header or malformed record')
                    encoded = fields[2:]
                    if fields[0] == 'BLOCK':
                        if not re.fullmatch(r'[0-9]{1,13}', fields[3]):
                            raise Unknown('invalid block size')
                        # Some emulated whole disks expose a readable empty
                        # serial. Empty serial is evidence, not an identity.
                        if not re.fullmatch(r'(?:[0-9a-f]{2})*', fields[5]):
                            raise Unknown('invalid block serial encoding')
                        encoded = [fields[2], fields[4]]
                    if any(not re.fullmatch(r'(?:[0-9a-f]{2})+', value) for value in encoded):
                        raise Unknown('invalid inventory hex encoding')
            complete = response(command, arguments, challenge, line)
            if complete:
                if command == 'SYSINFO' and not sysinfo_started:
                    raise Unknown('SYSINFO completion without challenged header')
                return {'status': 'OBSERVED', 'command': command, 'challenge': challenge,
                        'lines': lines, 'scope': 'guest response only; caller must verify lifecycle/data postconditions'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True, type=Path)
    parser.add_argument('--observe-sec', type=float, default=30)
    parser.add_argument('--wait-ready', action='store_true',
                        help='receive only until owned-agent greeting; never probe firmware')
    parser.add_argument('command', choices=('SYSINFO', 'GET_RAM', 'SET_RAM', 'VERIFY'))
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    if not args.socket.is_absolute():
        parser.error('absolute preflight-verified socket required')
    challenge = secrets.token_hex(16)
    request(args.command, args.arguments, challenge)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(args.observe_sec)
        connection.connect(str(args.socket))
        if args.wait_ready:
            wait_ready(connection, args.observe_sec)
        result = exchange(connection, args.command, args.arguments, challenge, args.observe_sec)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, Unknown) as error:
        print(json.dumps({'status': 'UNKNOWN', 'reason': str(error), 'retry': 'PROHIBITED'}))
        raise SystemExit(2)
