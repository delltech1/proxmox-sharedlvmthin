#!/usr/bin/python3
"""UNSHIPPED, authority-NONE four-node transport, no CLI or remote installer.

The unshipped companion server binds HELPER to measured installed sources and
invokes the existing local executor only with a separately trusted authorizer.
Caller SSH exec commands are forbidden; the server's fixed external subsystem
still has the account-shell startup trust boundary imposed by OpenSSH.

The coordinator owns durable intent/release validation. A mandatory in-process
dispatch_guard verifies that boundary; caller JSON cannot supply it. This
process poisons itself on uncertainty, but durable anti-replay across fresh
processes belongs to the coordinator/local executor journals, not transport.
"""
import base64
import copy
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import struct
import subprocess
import time
import secrets

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('exact_transport_model', HERE / 'layout-migration-exact-restore-model.py')
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
Refusal, need = M.Refusal, M.need
NODES = ('node-a', 'node-b', 'node-c', 'node-control')
ROOT = Path('/var/lib/pve-sharedlvmthin/maintenance-transport')
HELPER = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-exact-restore-transport'
SUBSYSTEM = 'sharedlvmthin-exact-restore-v1'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LANG': 'C'}
MAX_WIRE = 262144
MAX_REQUEST = 12 * 1024 * 1024
MAX_ERROR = 16384
DEADLINE = 20
REQUEST_FIELDS = {'tx', 'plan_sha256', 'node', 'boot_id', 'unit', 'operation',
                  'baseline_sha256', 'release_sha256'}


def root_required():
    need(os.name == 'posix' and os.getuid() == os.geteuid() == 0, 'root-required POSIX transport')


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                       allow_nan=False) + '\n').encode('ascii')


def strict(raw, maximum=MAX_WIRE):
    need(type(raw) is bytes and 0 < len(raw) <= maximum, 'JSON byte bound')
    def pairs(rows):
        value = {}
        for key, item in rows:
            need(key not in value, 'duplicate JSON key')
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(Refusal('non-finite JSON')))
        need(canonical(value) == raw, 'noncanonical JSON')
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise Refusal('invalid canonical JSON') from exc


def ssh_key(value):
    need(type(value) is str and value.startswith('ssh-ed25519 ') and value.count(' ') == 1,
         'only exact ed25519 public host keys are accepted')
    try:
        encoded = value.split(' ')[1]
        raw = base64.b64decode(encoded, validate=True)
        expected_prefix = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32)
        need(raw[:19] == expected_prefix and len(raw) == 51
             and base64.b64encode(raw).decode() == encoded, 'host key wire encoding')
    except (ValueError, TypeError) as exc:
        raise Refusal('invalid host key') from exc
    return 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')


def validate_pins(plan, pins):
    M.exact(pins, {'schema', 'authority', 'plan_sha256', 'helper_path', 'participants'}, 'transport pins')
    need(pins['schema'] == 'slt-exact-transport-pins/v1' and pins['authority'] == 'NONE'
         and pins['plan_sha256'] == M.digest(plan) and pins['helper_path'] == HELPER, 'pin identity')
    need(type(pins['participants']) is list and len(pins['participants']) == 4, 'closed four-node cohort')
    need(tuple(row['node'] for row in plan['participants']) == NODES
         and all(row['role'] == ('CONTROL_ONLY' if row['node'] == 'node-control' else 'SAN')
                 for row in plan['participants']), 'fixed SAN/control cohort')
    hosts, keys, helpers = set(), set(), set()
    for index, row in enumerate(pins['participants']):
        M.exact(row, {'node', 'host', 'boot_id', 'ssh_host_key', 'ssh_host_fingerprint',
                      'helper_sha256', 'source_package'}, 'participant pins')
        need(row['node'] == NODES[index] and row['boot_id'] == plan['participants'][index]['boot_id'],
             'hostname/boot/participant order differs')
        try:
            host = ipaddress.ip_address(row['host'])
            need(str(host) == row['host'] and not host.is_unspecified and not host.is_multicast
                 and not host.is_loopback and '%' not in row['host'], 'noncanonical peer address')
        except (ValueError, TypeError) as exc:
            raise Refusal('numeric peer address required; no aliases') from exc
        need(ssh_key(row['ssh_host_key']) == row['ssh_host_fingerprint'], 'host-key fingerprint differs')
        need(M.sha(row['helper_sha256']), 'helper SHA required')
        package = row['source_package']
        M.exact(package, {'name', 'version', 'artifact_sha256', 'runtime_build_id', 'sources_sha256'}, 'source package')
        need(package['name'] in ('pve-sharedlvmthin', 'pve-sharedlvmthin-thick')
             and type(package['version']) is str and re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,128}', package['version'])
             and all(M.sha(package[key]) for key in ('artifact_sha256', 'runtime_build_id', 'sources_sha256')),
             'source package identity malformed')
        need(package['artifact_sha256'] in (plan['participants'][index]['before_package_sha256'],
                                            plan['participants'][index]['after_package_sha256']),
             'package outside pinned plan')
        hosts.add(row['host']); keys.add(row['ssh_host_key'])
        helpers.add((row['helper_sha256'], package['sources_sha256']))
    need(len(hosts) == len(keys) == 4 and len(helpers) == 1, 'duplicate peer/key or mixed helper cohort')
    need(len({row['boot_id'] for row in pins['participants']}) == 4, 'duplicate boot identity')


def known_hosts(pins):
    return ''.join(row['node'] + ' ' + row['ssh_host_key'] + '\n'
                   for row in pins['participants']).encode('ascii')


class FixedFiles:
    """Read-only root-owned 0700/0600 fixed local bootstrap; no repair/create."""
    def __init__(self):
        root_required()
        self.snapshot = self._read_all()
        self.identity_path = str(ROOT / 'identity_ed25519')
        self.known_hosts_path = str(ROOT / 'known_hosts')

    @staticmethod
    def read(name, maximum):
        need(name in {'plan.json', 'pins.json', 'known_hosts', 'identity_ed25519'}, 'fixed bootstrap files only')
        root_required()
        opened = []
        try:
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            opened.append(fd)
            root_info = os.fstat(fd)
            need(root_info.st_uid == 0 and not root_info.st_mode & 0o022, 'unsafe root ancestor')
            for part in ROOT.parts[1:]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                opened.append(fd)
                info = os.fstat(fd)
                need(info.st_uid == 0 and not info.st_mode & 0o022, 'unsafe bootstrap ancestor')
            need(stat.S_IMODE(info.st_mode) == 0o700, 'bootstrap root must be 0700')
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
            opened.append(file_fd)
            before = os.fstat(file_fd)
            need(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                 and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= maximum,
                 'unsafe bootstrap file identity')
            chunks, size = [], 0
            while size <= maximum:
                block = os.read(file_fd, min(65536, maximum + 1 - size))
                if not block:
                    break
                chunks.append(block); size += len(block)
            signature = lambda s: (s.st_dev, s.st_ino, s.st_uid, s.st_mode, s.st_nlink,
                                   s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            need(size == before.st_size and size <= maximum
                 and signature(before) == signature(os.fstat(file_fd))
                 == signature(os.stat(name, dir_fd=fd, follow_symlinks=False)), 'bootstrap changed during read')
            return b''.join(chunks)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def _read_all(self):
        values = {name: self.read(name, MAX_WIRE if name.endswith('.json') else 16384)
                  for name in ('plan.json', 'pins.json', 'known_hosts', 'identity_ed25519')}
        plan, pins = strict(values['plan.json']), strict(values['pins.json'])
        M.validate_plan(plan, plan['issued_at']); validate_pins(plan, pins)
        need(values['known_hosts'] == known_hosts(pins), 'known_hosts differs from exact pins')
        # Never retain/output the private key bytes in transport evidence.
        values['identity_ed25519'] = hashlib.sha256(values['identity_ed25519']).digest()
        return plan, pins, values

    def load(self):
        self.check()
        return copy.deepcopy(self.snapshot[:2])

    def check(self):
        need(self._read_all() == self.snapshot, 'bootstrap identity drift')


def ssh_argv(node, host):
    need(node in NODES and type(host) is str, 'fixed SSH participant required')
    try:
        address = ipaddress.ip_address(host)
        need(str(address) == host and not address.is_unspecified and not address.is_multicast
             and not address.is_loopback and '%' not in host, 'fixed numeric SSH destination required')
    except ValueError as exc:
        raise Refusal('SSH destination is not numeric') from exc
    options = ['BatchMode=yes', 'StrictHostKeyChecking=yes', 'HostKeyAlgorithms=ssh-ed25519',
               'UpdateHostKeys=no', 'CheckHostIP=no', 'CanonicalizeHostname=no', 'ConnectionAttempts=1',
               'ConnectTimeout=5', 'NumberOfPasswordPrompts=0', 'IdentitiesOnly=yes', 'IdentityAgent=none',
               'ForwardAgent=no', 'ForwardX11=no', 'ClearAllForwardings=yes', 'ControlMaster=no',
               'ControlPath=none', 'ControlPersist=no', 'ProxyCommand=none', 'ProxyJump=none',
               'PermitLocalCommand=no', 'GlobalKnownHostsFile=/dev/null',
               'UserKnownHostsFile=' + str(ROOT / 'known_hosts'), 'HostKeyAlias=' + node]
    return ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-p', '22', '-l', 'root',
            '-i', str(ROOT / 'identity_ed25519')] + [arg for opt in options for arg in ('-o', opt)] \
        + ['-s', '--', host, SUBSYSTEM]


def bounded_process(argv, raw, timeout):
    """Bound both streams while reading, not after communicate() allocates them."""
    root_required()
    nodes = [node for node in NODES if 'HostKeyAlias=' + node in argv] if type(argv) is list else []
    need(len(nodes) == 1 and len(argv) > 2 and argv == ssh_argv(nodes[0], argv[-2]), 'noncanonical SSH argv')
    need(type(raw) is bytes and len(raw) <= MAX_REQUEST and 0 < timeout <= DEADLINE, 'request/process bounds')
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=ENV, shell=False, start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    started, sent = time.monotonic(), 0
    try:
        for pipe, label in ((process.stdin, 'stdin'), (process.stdout, 'stdout'), (process.stderr, 'stderr')):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_WRITE if label == 'stdin' else selectors.EVENT_READ, label)
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            need(remaining > 0, 'SSH deadline; outcome unknown')
            for key, _ in selector.select(min(remaining, 0.1)):
                if key.data == 'stdin':
                    sent += os.write(key.fd, raw[sent:])
                    if sent == len(raw):
                        selector.unregister(key.fileobj); key.fileobj.close()
                else:
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj); key.fileobj.close()
                    else:
                        output[key.data].extend(block)
                        need(len(output[key.data]) <= (MAX_WIRE if key.data == 'stdout' else MAX_ERROR),
                             'SSH output limit; outcome unknown')
        remaining = timeout - (time.monotonic() - started)
        need(remaining > 0, 'SSH exit deadline; outcome unknown')
        return process.wait(timeout=remaining), bytes(output['stdout']), bytes(output['stderr'])
    finally:
        selector.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def validate_receipt(receipt, request, plan, observed_at):
    M.exact(receipt, {'schema', 'authority', 'state', 'request_sha256', 'intent_sha256', 'after',
                      'command', 'started_at', 'finished_at', 'effect_retry_allowed'}, 'local receipt')
    need(receipt['authority'] == 'NONE' and receipt['schema'] == 'slt-local-restore-receipt/v1'
         and receipt['state'] == 'LOCAL_EFFECT_CONFIRMED' and receipt['request_sha256'] == M.digest(request)
         and receipt['effect_retry_allowed'] is False and M.sha(receipt['intent_sha256']),
         'local effect not confirmed; no redispatch')
    M.exact(receipt['command'], {'exit_code', 'stdout_sha256', 'stderr_sha256'}, 'command receipt')
    M.exact(receipt['after'], {'active', 'masked', 'substate', 'source'}, 'unit receipt')
    need(type(receipt['command']['exit_code']) is int and receipt['command']['exit_code'] == 0
         and M.sha(receipt['command']['stdout_sha256']) and M.sha(receipt['command']['stderr_sha256'])
         and type(receipt['started_at']) is int and type(receipt['finished_at']) is int
         and plan['issued_at'] <= receipt['started_at'] <= receipt['finished_at'] <= observed_at
         and type(receipt['after']['active']) is bool and receipt['after']['masked'] is False
         and (request['operation'] != 'START' or receipt['after']['active'])
         and receipt['after']['substate'] == (('exited' if request['unit'] == 'pve-guests.service'
             else 'running') if receipt['after']['active'] else 'dead'), 'invalid local command/timing/postcondition')
    need(type(receipt['after']['source']) is dict and bool(receipt['after']['source']), 'missing unit source proof')
    return copy.deepcopy(receipt)


def effect_key(request):
    return request['node'] + ':' + request['unit'] + ':' + request['operation']


class Transport:
    """Implements only observe(node) / dispatch_typed(request), authority NONE."""
    authority = 'NONE'

    def __init__(self, *, files=None, runner=bounded_process, dispatch_guard=None, receipt_journal=None,
                 clock=time.time, nonce=lambda: secrets.token_hex(32), require_root=root_required):
        require_root()
        self.files = files if files is not None else FixedFiles()
        self.plan, self.pins = self.files.load()
        M.validate_plan(self.plan, int(clock())); validate_pins(self.plan, self.pins)
        self.runner, self.dispatch_guard, self.clock, self.nonce, self.require_root = runner, dispatch_guard, clock, nonce, require_root
        self.receipt_journal = receipt_journal
        self.plan_hash, self.pin_hash = M.digest(self.plan), M.digest(self.pins)
        self.owner_pid, self.last_time = os.getpid(), int(clock())
        self.poisoned, self.seen_nonces, self.effects = False, set(), set()

    def _check(self, *, inspection=False):
        self.require_root()
        need((inspection or not self.poisoned) and self.owner_pid == os.getpid(), 'poisoned/forked transport; inspect only')
        need(M.digest(self.plan) == self.plan_hash and M.digest(self.pins) == self.pin_hash, 'mutable pin drift')
        now = int(self.clock()); M.validate_plan(self.plan, self.plan['issued_at'] if inspection else now)
        need(now >= self.last_time, 'transport clock regressed')
        self.last_time = now
        self.files.check()

    def argv(self, row):
        # Numeric destination + independently pinned HostKeyAlias; no exec
        # command, shell interpolation, environment forwarding or user argv.
        return ssh_argv(row['node'], row['host'])

    def _exchange(self, node, kind, payload, *, release_evidence=None):
        inspection = kind == 'INSPECT_RESTORE'
        self._check(inspection=inspection)
        need(node in NODES and kind in ('READ_OBSERVATION', 'RESTORE_EFFECT', 'INSPECT_RESTORE', 'ENTRY_BLOCK', 'SERVICE_DRAIN'), 'closed transport vocabulary')
        row = self.pins['participants'][NODES.index(node)]
        challenge = self.nonce()
        need(type(challenge) is str and re.fullmatch(r'[0-9a-f]{64}', challenge)
             and challenge not in self.seen_nonces, 'nonce reuse/invalid challenge')
        self.seen_nonces.add(challenge)
        request = {'schema': 'slt-exact-transport-request/v1', 'authority': 'NONE', 'kind': kind,
                   'tx': self.plan['tx'], 'plan_sha256': self.plan_hash, 'node': node,
                   'boot_id': row['boot_id'], 'challenge': challenge, 'payload': payload}
        try:
            if release_evidence is not None:
                need(kind == 'RESTORE_EFFECT', 'release evidence only belongs to restore')
                spec = importlib.util.spec_from_file_location('exact_transport_wire', HERE / 'layout-migration-exact-wire.py')
                wire = importlib.util.module_from_spec(spec); spec.loader.exec_module(wire)
                request['payload'] = wire.pack(payload, release_evidence, request, self.pins)
            raw = canonical(request)
            need(len(raw) <= (MAX_REQUEST if kind == 'RESTORE_EFFECT' else 4096), 'typed request ceiling')
            if kind == 'RESTORE_EFFECT':
                attempt = {'schema': 'slt-exact-transport-attempt/v1', 'authority': 'NONE', 'effect': payload,
                           'wire_sha256': M.digest(request), 'challenge': challenge, 'pins_sha256': self.pin_hash,
                           'effect_retry_allowed': False}
                self._persist('transport:' + effect_key(payload) + ':intent', attempt)
            self.files.check()
            remaining = DEADLINE if inspection else min(DEADLINE, self.plan['expires_at'] - int(self.clock()))
            need(remaining > 0, 'transport deadline exhausted before dispatch')
            rc, out, err = self.runner(self.argv(row), raw,
                                       remaining)
            need(type(rc) is int and rc == 0 and type(out) is bytes and type(err) is bytes
                 and not err and len(out) <= MAX_WIRE, 'SSH refused/ambiguous output')
            response = strict(out)
            M.exact(response, {'schema', 'authority', 'kind', 'tx', 'plan_sha256', 'node', 'boot_id',
                               'challenge', 'request_sha256', 'helper_path', 'helper_sha256',
                               'source_package', 'payload'}, 'transport response')
            need(response['schema'] == 'slt-exact-transport-response/v1' and response['authority'] == 'NONE'
                 and all(response[key] == request[key] for key in
                         ('kind', 'tx', 'plan_sha256', 'node', 'boot_id', 'challenge'))
                 and response['request_sha256'] == M.digest(request)
                 and response['helper_path'] == HELPER and response['helper_sha256'] == row['helper_sha256']
                 and response['source_package'] == row['source_package'], 'response identity/challenge/helper differs')
            self._check(inspection=inspection)
            return copy.deepcopy(response['payload'])
        except BaseException:
            self.poisoned = True
            raise

    def observe(self, node):
        try:
            value = self._exchange(node, 'READ_OBSERVATION', {})
            row = self.pins['participants'][NODES.index(node)]
            # The coordinator chooses before/after semantics; transport never
            # promotes one into the other or substitutes cached observations.
            validator = object.__new__(M.Coordinator); validator.plan = self.plan
            before = value.get('package_sha256') == next(p for p in self.plan['participants']
                        if p['node'] == node)['before_package_sha256'] \
                and value.get('storage_cfg_sha256') == self.plan['storage_cfg_sha256']
            validator.validate_record(value, node, before=before)
            need(value['package_sha256'] == row['source_package']['artifact_sha256'], 'observed/loaded package differs')
            return value
        except BaseException:
            self.poisoned = True
            raise

    def collect(self):
        records = {node: self.observe(node) for node in NODES}
        self._check()
        return {'authority': 'NONE', 'plan_sha256': self.plan_hash, 'records': records}

    def dispatch_typed(self, request, *, release_evidence=None):
        receipt = self.dispatch_restore_once(request, release_evidence=release_evidence)
        return {'request_sha256': receipt['request_sha256'], 'result': 'CONFIRMED'}

    def _persist(self, key, value):
        need(self.receipt_journal is not None, 'durable transport journal absent')
        need(self.receipt_journal.create_once(self.plan['tx'], key, copy.deepcopy(value))
             == {'durable': True, 'sha256': M.digest(value)}, 'transport journal ACK unknown')
        need(self.receipt_journal.read(self.plan['tx'], key) == value, 'transport journal readback differs')

    def dispatch_restore_once(self, request, *, release_evidence=None):
        """Return full verified receipt only after create-only durable publication."""
        # Legacy raw-less shape remains useful to isolated model backends;
        # the fixed subsystem server refuses it, including with an authorizer.
        self._check()
        M.exact(request, REQUEST_FIELDS, 'restore effect')
        node = request['node']
        need(node in NODES, 'unknown effect node')
        row = self.pins['participants'][NODES.index(node)]
        need(request['tx'] == self.plan['tx'] and request['plan_sha256'] == self.plan_hash
             and request['boot_id'] == row['boot_id'] and M.sha(request['baseline_sha256'])
             and M.sha(request['release_sha256']) and request['operation'] in ('UNMASK', 'START')
             and request['unit'] in M.MASKED_BY_BARRIER
             and (request['operation'] != 'START' or request['unit'] in M.TARGETS), 'invalid restore identity/effect')
        key = (node, request['unit'], request['operation'])
        need(key not in self.effects and callable(self.dispatch_guard), 'no retry/default dispatch authority')
        need(self.receipt_journal is not None, 'durable transport journal absent')
        name = effect_key(request)
        need(all(self.receipt_journal.read(self.plan['tx'], record) is None for record in
                 ('transport:' + name + ':intent', 'transport:' + name + ':done', 'receipt:' + name)),
             'prior durable transport attempt; inspection only')
        need(self.dispatch_guard(copy.deepcopy(request), copy.deepcopy(self.pins)) is True,
             'trusted coordinator dispatch guard refused')
        self.effects.add(key)  # Before any possible network effect, never undone.
        try:
            receipt = self._exchange(node, 'RESTORE_EFFECT', copy.deepcopy(request), release_evidence=release_evidence)
            validate_receipt(receipt, request, self.plan, self.last_time)
            self._persist('receipt:' + name, receipt)
            attempt = self.receipt_journal.read(self.plan['tx'], 'transport:' + name + ':intent')
            self._persist('transport:' + name + ':done', {'authority': 'NONE', 'attempt_sha256': M.digest(attempt),
                          'receipt_sha256': M.digest(receipt), 'effect_retry_allowed': False})
            return copy.deepcopy(receipt)
        except BaseException:
            self.poisoned = True
            raise

    def inspect_restore_attempt(self, request, *, challenge=None, remote=False):
        """Read-only recovery, including poisoned/expired attempts; never redispatch."""
        self._check(inspection=True)
        M.exact(request, REQUEST_FIELDS, 'inspection request')
        need(request['tx'] == self.plan['tx'] and request['plan_sha256'] == self.plan_hash
             and request['node'] in NODES and request['unit'] in M.MASKED_BY_BARRIER
             and request['operation'] in ('UNMASK', 'START') and self.receipt_journal is not None,
             'inspection request/journal differs')
        name = effect_key(request)
        result = {'schema': 'slt-exact-restore-inspection/v1', 'authority': 'NONE', 'state': 'UNKNOWN',
                  'request_sha256': M.digest(request), 'wire_sha256': None, 'intent_sha256': None,
                  'receipt': None, 'effect_retry_allowed': False}
        attempt = self.receipt_journal.read(self.plan['tx'], 'transport:' + name + ':intent')
        if attempt is None: return result
        M.exact(attempt, {'schema', 'authority', 'effect', 'wire_sha256', 'challenge', 'pins_sha256',
                         'effect_retry_allowed'}, 'transport attempt')
        need(attempt['schema'] == 'slt-exact-transport-attempt/v1' and attempt['authority'] == 'NONE'
             and attempt['effect_retry_allowed'] is False and M.sha(attempt['wire_sha256'])
             and M.sha(attempt['challenge']), 'transport attempt malformed')
        if (attempt['effect'] != request or attempt['pins_sha256'] != self.pin_hash
                or challenge is not None and challenge != attempt['challenge']): return result
        result['wire_sha256'] = attempt['wire_sha256']
        if remote:
            observed = self._exchange(request['node'], 'INSPECT_RESTORE',
                                      {'effect': copy.deepcopy(request), 'wire_sha256': attempt['wire_sha256']})
            M.exact(observed, set(result), 'inspection response')
            need(all(observed[k] == result[k] for k in ('schema', 'authority', 'request_sha256', 'wire_sha256', 'effect_retry_allowed'))
                 and observed['state'] in ('UNKNOWN', 'COMPLETED_HISTORICAL'), 'inspection response differs')
            if observed['state'] == 'UNKNOWN':
                need(observed['receipt'] is None and (observed['intent_sha256'] is None or M.sha(observed['intent_sha256'])),
                     'unknown inspection carries completion')
                return observed
            receipt = observed['receipt']
            need(observed['intent_sha256'] == receipt.get('intent_sha256'), 'inspection intent differs')
        else:
            receipt = self.receipt_journal.read(self.plan['tx'], 'receipt:' + name)
            done = self.receipt_journal.read(self.plan['tx'], 'transport:' + name + ':done')
            if receipt is None or done is None: return result
            need(done == {'authority': 'NONE', 'attempt_sha256': M.digest(attempt),
                         'receipt_sha256': M.digest(receipt), 'effect_retry_allowed': False}, 'transport completion differs')
        validate_receipt(receipt, request, self.plan, self.last_time)
        result.update(state='COMPLETED_HISTORICAL', intent_sha256=receipt['intent_sha256'], receipt=copy.deepcopy(receipt))
        return result

    def dispatch_entry(self, request):
        """One exact legacy node-runner request; no restore/release authority."""
        self._check()
        M.exact(request, {'tx', 'plan_sha256', 'node', 'operation'}, 'entry request')
        node, operation = request['node'], request['operation']
        need(node in NODES and operation in ('ENTRY_BLOCK', 'SERVICE_DRAIN')
             and request['tx'] == self.plan['tx'] and M.sha(request['plan_sha256']), 'entry vocabulary/identity')
        key = (node, 'BARRIER_ENTRY', operation)
        need(key not in self.effects and callable(self.dispatch_guard), 'no retry/default entry authority')
        need(self.dispatch_guard(copy.deepcopy(request), copy.deepcopy(self.pins)) is True, 'entry guard refused')
        self.effects.add(key)
        try:
            result = self._exchange(node, operation, copy.deepcopy(request))
            M.exact(result, {'schema', 'authority', 'state', 'request_sha256', 'attempt_nonce',
                             'attempt', 'response', 'wire_intent_sha256', 'effect_retry_allowed'}, 'entry response')
            identity = hashlib.sha256(canonical(request)).hexdigest()
            need(result['schema'] == 'slt-exact-entry-wire-result/v1' and result['authority'] == 'NONE'
                 and result['state'] == 'OBSERVED_COMPLETE' and result['request_sha256'] == identity
                 and result['attempt_nonce'] == identity[:32] and result['effect_retry_allowed'] is False,
                 'entry attempt binding differs')
            need(M.sha(result['wire_intent_sha256']), 'wire durable identity absent')
            attempt = result['attempt']
            M.exact(attempt, {'state', 'records', 'retry_authorized', 'release_authorized'}, 'entry latch')
            need(type(attempt) is dict and attempt.get('state') == 'COMPLETED_HISTORICAL'
                 and attempt.get('retry_authorized') is False and attempt.get('release_authorized') is False,
                 'entry durable completion absent')
            records = attempt['records']
            need(type(records) is list and len(records) == 2, 'entry latch records incomplete')
            M.exact(records[0], {'schema', 'request', 'boot_id', 'state'}, 'entry intent')
            M.exact(records[1], {'schema', 'request_sha256', 'state', 'response'}, 'entry outcome')
            need(records[0] == {'schema': 'slt-barrier-node-attempt/v1', 'request': request,
                 'boot_id': self.pins['participants'][NODES.index(node)]['boot_id'], 'state': 'ATTEMPT_RESERVED'}
                 and records[1] == {'schema': 'slt-barrier-node-result/v1', 'request_sha256': identity,
                 'state': 'OBSERVED_COMPLETE', 'response': result['response']}
                 and type(result['response']) is dict and result['response'].get('request') == request,
                 'entry attempt/result source differs')
            return copy.deepcopy(result)
        except BaseException:
            self.poisoned = True
            raise
