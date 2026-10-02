#!/usr/bin/python3 -I
"""UNSHIPPED fixed subsystem bootstrap/fresh byte loader. No option CLI.

Production entry deliberately has NO effect authorizer. An independently
reviewed in-process grant provider may be supplied to Engine by a trusted
integration, never by stdin, argv, env or an arbitrary Python import path.

External OpenSSH subsystems still cross sshd's fixed account-shell startup
boundary. This server accepts no caller command and does not execute a shell;
it cannot prove sshd configuration/authentication from its own process.
"""
import contextlib
import copy
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import types

INSTALLED = Path('/usr/libexec/pve-sharedlvmthin')
STATE = Path('/var/lib/pve-sharedlvmthin/maintenance-transport')
PACKAGE = Path('/usr/share/pve-sharedlvmthin')
ENTRY = 'sharedlvmthin-exact-restore-transport'
LOCAL = 'layout-migration-exact-local-restore.py'
TRANSPORT = 'layout-migration-exact-transport.py'
WIRE = 'layout-migration-exact-wire.py'
VERIFIER = 'layout-migration-exact-release-validator.py'
NODE_RUNNER = 'layout-migration-barrier-node-runner.py'
LOCAL_FILES = (LOCAL, 'layout-migration-exact-baseline-collector.py',
         'layout-migration-exact-local-journal.py', 'layout-migration-exact-restore-model.py',
         'layout-migration-barrier-collector.py', 'layout-migration-barrier-backend.py',
         'layout-migration-barrier-model.py', 'layout-migration-barrier-file-journal.py',
         'layout-migration-node-evidence.py', 'layout-migration-workload-gate.py',
         'prelive_exact_journal_file_backend.py')
FILES = (ENTRY, TRANSPORT, WIRE, VERIFIER, NODE_RUNNER, 'layout-migration-barrier-node-latch.py',
         *LOCAL_FILES, 'layout-migration-all-certified-v2.py', 'layout-migration-release-certificate-v2.py',
         'layout-migration-all-ready-v2.py', 'layout-migration-all-configured-v2.py',
         'layout-migration-v2-context.py', 'layout-migration-topology-v2.py', 'layout-migration-plan.py',
         'layout-migration-release-certificate.py', 'layout-migration-all-ready-plan.py',
         'layout-migration-finalize-plan.py')
CONFIG = ('server-source-pins.json', 'plan.json', 'pins.json', 'workload.json', 'baseline.json')
MARKERS = ('package-flavor', 'package-artifact-sha256', 'runtime-build-id')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LANG': 'C'}
MAX_REQUEST, MAX_RESPONSE, MAX_FILE = 12 * 1024 * 1024, 262144, 4 * 1024 * 1024


class Refusal(ValueError): pass


def need(condition, message):
    if not condition: raise Refusal(message)


def root_required():
    need(os.name == 'posix' and os.getuid() == os.geteuid() == 0, 'root required')


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')


def strict(raw, maximum=MAX_FILE):
    need(type(raw) is bytes and 0 < len(raw) <= maximum, 'document bound')
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result, 'duplicate key'); result[key] = value
        return result
    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(Refusal('non-finite JSON')))
        need(canonical(value) == raw, 'noncanonical JSON')
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Refusal('invalid canonical document') from exc


def read_fixed(directory, name):
    root_required()
    allowed = FILES if directory == INSTALLED else CONFIG if directory == STATE else MARKERS if directory == PACKAGE else ()
    need(name in allowed, 'fixed bootstrap name required')
    fds = []
    try:
        parent = None
        for component in ('/',) + directory.parts[1:]:
            fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            fds.append(fd); info = os.fstat(fd)
            need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, 'unsafe ancestor')
            parent = fd
        if directory == STATE: need(stat.S_IMODE(info.st_mode) == 0o700, 'state root must be 0700')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        fds.append(fd); first = os.fstat(fd)
        need(stat.S_ISREG(first.st_mode) and first.st_uid == 0 and first.st_nlink == 1
             and not first.st_mode & 0o022 and 0 < first.st_size <= MAX_FILE
             and (directory != STATE or stat.S_IMODE(first.st_mode) == 0o600), 'unsafe input/source')
        raw = bytearray()
        while len(raw) <= MAX_FILE:
            block = os.read(fd, min(65536, MAX_FILE + 1 - len(raw)))
            if not block: break
            raw.extend(block)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,
                              s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        need(len(raw) == first.st_size and len(raw) <= MAX_FILE and identity(first) == identity(os.fstat(fd))
             == identity(os.stat(name, dir_fd=parent, follow_symlinks=False)), 'input/source changed')
        return bytes(raw)
    finally:
        for fd in reversed(fds): os.close(fd)


class BytesLoader(importlib.abc.Loader):
    def __init__(self, raw, path): self.raw, self.path = bytes(raw), path
    def create_module(self, spec): return None
    def exec_module(self, module):
        module.__file__ = self.path
        sys.modules[module.__name__] = module
        exec(compile(self.raw, self.path, 'exec', dont_inherit=True), module.__dict__)


class SealedBundle:
    """No filesystem fallback for bundle spec imports, no pyc/source reread."""
    def __init__(self, sources, hashes, check):
        need(type(sources) is dict and set(sources) == set(FILES) and set(hashes) == set(FILES), 'bundle coverage')
        need(all(type(raw) is bytes and hashlib.sha256(raw).hexdigest() == hashes[name]
                 for name, raw in sources.items()), 'source hash mismatch')
        self.sources = types.MappingProxyType(dict(sources))
        self.hashes = types.MappingProxyType(dict(hashes))
        self.check = check

    @contextlib.contextmanager
    def imports(self):
        original = importlib.util.spec_from_file_location
        def spec(name, location, *args, **kwargs):
            path = Path(location)
            need(not args and not kwargs and path.parent == INSTALLED and path.name in FILES
                 and path.name != ENTRY, 'unapproved bundle import path')
            loader = BytesLoader(self.sources[path.name], str(path))
            return importlib.util.spec_from_loader(name, loader, origin=str(path))
        importlib.util.spec_from_file_location = spec
        old_bytecode = sys.dont_write_bytecode; sys.dont_write_bytecode = True
        try:
            self.check(); yield; self.check()
        finally:
            importlib.util.spec_from_file_location = original
            sys.dont_write_bytecode = old_bytecode

    def load(self, filename):
        need(filename in (LOCAL, TRANSPORT, WIRE, VERIFIER, NODE_RUNNER), 'fixed module entry required')
        with self.imports():
            spec = importlib.util.spec_from_file_location('sealed_' + filename.replace('-', '_').replace('.', '_'),
                                                         INSTALLED / filename)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            if filename == LOCAL:
                need(set(module.FILES) == set(LOCAL_FILES), 'local source set drift')
                module.LOADED_SOURCE_SHA256 = {name: self.hashes[name] for name in module.FILES}
            return module


def fresh_server():
    """Bootstrap trust ends at measured immutable server bytes, not a reread import."""
    pin_raw = read_fixed(STATE, 'server-source-pins.json')
    pins = strict(pin_raw)
    raw = read_fixed(INSTALLED, ENTRY)
    need(type(pins) is dict and pins.get('schema') == 'slt-exact-server-source-pins/v1'
         and pins.get('authority') == 'NONE' and type(pins.get('sources')) is dict
         and pins['sources'].get(ENTRY) == hashlib.sha256(raw).hexdigest(), 'server source approval differs')
    loader = BytesLoader(raw, str(INSTALLED / ENTRY))
    spec = importlib.util.spec_from_loader('measured_exact_transport_server', loader, origin=str(INSTALLED / ENTRY))
    module = importlib.util.module_from_spec(spec); loader.exec_module(module)
    need(read_fixed(STATE, 'server-source-pins.json') == pin_raw and read_fixed(INSTALLED, ENTRY) == raw,
         'server source changed during bootstrap')
    return module


def package_probe(package):
    need(package in ('pve-sharedlvmthin', 'pve-sharedlvmthin-thick'), 'fixed package profile')
    import resource
    def restrict(): resource.setrlimit(resource.RLIMIT_FSIZE, (16384, 16384))
    rows = []
    for argv in (['/usr/bin/dpkg-query', '-W', '-f=${db:Status-Abbrev}|${Version}|${Architecture}', package],
                 ['/usr/bin/dpkg', '--audit'], ['/usr/bin/dpkg', '--verify', package]):
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    timeout=5, env=ENV, check=False, preexec_fn=restrict)
            out.seek(0); err.seek(0); raw, errors = out.read(16385), err.read(16385)
        need(result.returncode == 0 and not errors and len(raw) <= 16384, 'package probe failed')
        rows.append(raw)
    need(not rows[1] and not rows[2], 'package database/payload not clean')
    return rows[0]


class FixedContext:
    def __init__(self):
        root_required()
        need(Path(__file__).absolute() == INSTALLED / ENTRY, 'server not at fixed installed entry')
        self.pin_raw = read_fixed(STATE, 'server-source-pins.json')
        pins = strict(self.pin_raw)
        need(type(pins) is dict and set(pins) == {'schema', 'authority', 'sources', 'phase'}
             and pins['schema'] == 'slt-exact-server-source-pins/v1' and pins['authority'] == 'NONE'
             and pins['phase'] in ('BEFORE', 'AFTER') and type(pins['sources']) is dict
             and set(pins['sources']) == set(FILES), 'root source manifest invalid')
        self.phase = pins['phase']
        self.raw = {name: read_fixed(INSTALLED, name) for name in FILES}
        self.config = {name: read_fixed(STATE, name) for name in ('plan.json', 'pins.json', 'workload.json')}
        self.markers = {name: read_fixed(PACKAGE, name) for name in MARKERS}
        self.bundle = SealedBundle(self.raw, pins['sources'], self.check)
        self.transport = self.bundle.load(TRANSPORT)
        self.local = self.bundle.load(LOCAL)
        self.wire = self.bundle.load(WIRE)
        self.verifier_module = self.bundle.load(VERIFIER)
        self.node_runner = self.bundle.load(NODE_RUNNER)
        self.plan, self.pins, self.workload = (strict(self.config[name]) for name in ('plan.json', 'pins.json', 'workload.json'))
        self.transport.M.validate_plan(self.plan, int(time.time()))
        self.transport.validate_pins(self.plan, self.pins)
        node = socket.gethostname()
        rows = [row for row in self.pins['participants'] if row['node'] == node]
        need(len(rows) == 1, 'server hostname differs')
        self.row = rows[0]
        package = self.row['source_package']
        need(self.row['helper_sha256'] == hashlib.sha256(self.raw[ENTRY]).hexdigest()
             and package['sources_sha256'] == self.transport.M.digest(pins['sources'])
             and self.workload.get('evidence_sha256') == self.plan['workload_sha256'], 'bundle/workload binding')
        for name, field in (('package-artifact-sha256', 'artifact_sha256'), ('runtime-build-id', 'runtime_build_id')):
            need(self.markers[name] == (package[field] + '\n').encode('ascii'), 'installed package marker differs')
        need(self.markers['package-flavor'] == (b'dual\n' if package['name'] == 'pve-sharedlvmthin' else b'thick-only\n'),
             'installed profile differs')
        self.check_package()

    def check(self):
        need(read_fixed(STATE, 'server-source-pins.json') == self.pin_raw, 'approved source manifest changed')
        need(all(read_fixed(INSTALLED, name) == raw for name, raw in self.raw.items()), 'installed source changed')
        need(all(read_fixed(STATE, name) == raw for name, raw in self.config.items()), 'fixed input changed')
        need(all(read_fixed(PACKAGE, name) == raw for name, raw in self.markers.items()), 'package marker changed')

    def check_package(self):
        package = self.row['source_package']
        result = package_probe(package['name'])
        need(result in {('ii |' + package['version'] + '|all').encode(),
                        ('hi |' + package['version'] + '|all').encode()}, 'installed package version/status differs')
        self.check()

    def baseline(self): return strict(read_fixed(STATE, 'baseline.json'))

    def release_verifier(self):
        # This unshipped evaluator recomputes evidence but does not admit it.
        # Engine additionally requires an independent exact trusted grant.
        v = self.verifier_module
        return v.Validator.for_offline_test({n: self.bundle.sources[n] for n in v.FILES},
                                            {n: self.bundle.hashes[n] for n in v.FILES})

    def reserve_entry(self, envelope, grant):
        """Durable wire/source identity before the existing node-runner latch.

        The directory key excludes the challenge: a new challenge cannot retry
        the same node/operation. Any pre-effect crash requires inspection.
        """
        latch = self.node_runner.L
        root = latch._safe_root(STATE / 'entry-attempts', create=True)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        attempt_fd = None
        try:
            name = 'attempt-' + latch.request_nonce(envelope['payload'])
            os.mkdir(name, 0o700, dir_fd=root_fd); os.fsync(root_fd)
            attempt_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
            info = os.fstat(attempt_fd)
            need(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700, 'unsafe wire attempt directory')
            raw = canonical({'schema': 'slt-exact-entry-wire-intent/v1', 'authority': 'NONE',
                'envelope': envelope, 'grant': grant, 'pins_sha256': self.transport.M.digest(self.pins),
                'source_package': self.row['source_package'], 'effect_retry_allowed': False})
            ack = latch._write_once(attempt_fd, 'wire-intent.json', raw)
            expected = hashlib.sha256(raw).hexdigest()
            need(ack == {'sha256': expected, 'file_synced': True, 'dir_synced': True, 'closed': True},
                 'wire intent persistence uncertain')
            return expected
        finally:
            if attempt_fd is not None: os.close(attempt_fd)
            os.close(root_fd)


def issued_grant(t, result, recomputed, effect, source, before, evidence_raw):
    """Validate the closed provider boundary independently of its journal code."""
    need(type(result) is dict and set(result) == {'schema', 'authority', 'server_grant', 'issued_grant', 'issuance'},
         'independent provider result is not closed')
    need(result['schema'] == 'slt-exact-grant-provider-result/v1' and result['authority'] == 'NONE'
         and result['server_grant'] == recomputed, 'independent grant differs from raw evidence recomputation')
    record, grant = result['issuance'], result['issued_grant']
    t.M.exact(record, {'schema', 'authority', 'request', 'executor_sha256', 'before_sha256', 'evidence_sha256',
        'source_closure_sha256', 'phase_index', 'phase_prefix_sha256', 'server_grant', 'grant', 'record_sha256'}, 'issuance')
    t.M.exact(grant, set(recomputed), 'issued grant')
    body = copy.deepcopy(record); claimed = body.pop('record_sha256')
    binding = {k: record[k] for k in ('request', 'executor_sha256', 'before_sha256', 'evidence_sha256',
                                    'source_closure_sha256', 'phase_index', 'phase_prefix_sha256', 'server_grant')}
    need(record['schema'] == 'slt-exact-grant-issuance-model/v1' and record['authority'] == 'NONE'
         and claimed == t.M.digest(body) and record['request'] == effect
         and record['executor_sha256'] == t.M.digest(source) and record['before_sha256'] == t.M.digest(before)
         and record['evidence_sha256'] == hashlib.sha256(evidence_raw).hexdigest()
         and all(t.M.sha(record[k]) for k in ('source_closure_sha256', 'phase_prefix_sha256'))
         and type(record['phase_index']) is int and 0 <= record['phase_index'] < 4 * 9 * 2
         and record['server_grant'] == recomputed and record['grant'] == grant
         and grant == {**recomputed, 'authorization_sha256': t.M.digest(binding)},
         'issued grant phase/evidence binding differs')
    return copy.deepcopy(grant)


class Engine:
    def __init__(self, context, *, authorizer=None, entry_authorizer=None):
        self.context, self.authorizer, self.used = context, authorizer, False
        self.entry_authorizer = entry_authorizer
        self.pid = os.getpid()

    def handle(self, request):
        root_required()
        need(not self.used and self.pid == os.getpid(), 'one request per fresh server process')
        self.used = True
        c, t = self.context, self.context.transport
        t.M.exact(request, {'schema', 'authority', 'kind', 'tx', 'plan_sha256', 'node', 'boot_id', 'challenge', 'payload'}, 'request')
        t.M.validate_plan(c.plan, c.plan['issued_at'] if request['kind'] == 'INSPECT_RESTORE' else int(time.time()))
        need(request['schema'] == 'slt-exact-transport-request/v1' and request['authority'] == 'NONE'
             and request['kind'] in ('READ_OBSERVATION', 'RESTORE_EFFECT', 'INSPECT_RESTORE', 'ENTRY_BLOCK', 'SERVICE_DRAIN')
             and request['tx'] == c.plan['tx'] and request['plan_sha256'] == t.M.digest(c.plan)
             and request['node'] == c.row['node'] == socket.gethostname()
             and request['boot_id'] == c.row['boot_id'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
             and type(request['challenge']) is str and re.fullmatch(r'[0-9a-f]{64}', request['challenge']),
             'request identity differs')
        need(len(canonical(request)) <= (MAX_REQUEST if request['kind'] == 'RESTORE_EFFECT' else 4096),
             'typed request ceiling')
        c.check_package()
        if request['kind'] == 'READ_OBSERVATION':
            need(request['payload'] == {}, 'observation takes no caller arguments')
            projected = copy.deepcopy(c.plan)
            if c.phase == 'AFTER':
                projected['storage_cfg_sha256'] = projected['target_storage_cfg_sha256']
                for row in projected['participants']: row['before_package_sha256'] = row['after_package_sha256']
            result = c.local.C.collect_local(projected, c.workload, timeout=12)
            need(type(result) is dict and result.get('authority') == 'NONE'
                 and result.get('mutation_performed') is False
                 and result.get('plan_sha256') == t.M.digest(projected), 'collector identity differs')
            payload = result['observation']
            validator = object.__new__(t.M.Coordinator); validator.plan = c.plan
            validator.validate_record(payload, request['node'], before=c.phase == 'BEFORE')
        elif request['kind'] == 'RESTORE_EFFECT':
            need(c.phase == 'AFTER' and callable(self.authorizer), 'independent exact grant provider absent')
            payload_request, evidence_raw = c.wire.unpack(request['payload'], request, c.pins)
            verifier = c.release_verifier()
            t.M.exact(payload_request, t.REQUEST_FIELDS, 'restore effect')
            need(all(payload_request[key] == request[key] for key in ('tx', 'plan_sha256', 'node', 'boot_id')),
                 'inner/outer effect binding differs')
            need(payload_request['operation'] in ('UNMASK', 'START')
                 and payload_request['unit'] in t.M.MASKED_BY_BARRIER
                 and (payload_request['operation'] != 'START' or payload_request['unit'] in t.M.TARGETS), 'effect vocabulary')
            def identity():
                c.check()
                value = copy.deepcopy(c.local.installed_identity())
                # The executor's durable intent/grant must bind this precise
                # measured wire invocation, not only its local restore subset.
                value['transport'] = {'schema': 'slt-exact-wire-executor/v1', 'authority': 'NONE',
                    'wire_sha256': t.M.digest(request), 'pins_sha256': t.M.digest(c.pins),
                    'sources_sha256': c.row['source_package']['sources_sha256'],
                    'helper_sha256': c.row['helper_sha256']}
                return value
            recomputed = []
            def authorize(effect, source, before):
                c.check()
                result = verifier.evaluate(evidence_raw, effect, source, before, now=int(time.time()))
                admitted = issued_grant(t, self.authorizer(copy.deepcopy(effect), copy.deepcopy(source), copy.deepcopy(before)),
                                        result, effect, source, before, evidence_raw)
                recomputed.append(t.M.digest(admitted))
                return admitted
            with c.local.LocalJournal(c.plan, create=False) as journal:
                executor = c.local.Executor(c.plan, journal, authorize=authorize,
                    baseline_reader=c.baseline, identity_reader=identity)
                payload = executor.execute(copy.deepcopy(payload_request))
            need(recomputed and len(set(recomputed)) == 1, 'no fresh recomputation; historical/unknown attempt is inspection-only')
        elif request['kind'] == 'INSPECT_RESTORE':
            payload = self.inspect_restore(request['payload'])
        else:
            payload = self.entry(request)
        c.check_package()
        need(socket.gethostname() == c.row['node']
             and Path('/proc/sys/kernel/random/boot_id').read_text().strip() == request['boot_id'], 'post-request identity drift')
        response = {key: request[key] for key in ('authority', 'kind', 'tx', 'plan_sha256', 'node', 'boot_id', 'challenge')}
        response.update(schema='slt-exact-transport-response/v1', request_sha256=t.M.digest(request),
                        helper_path=str(INSTALLED / ENTRY), helper_sha256=c.row['helper_sha256'],
                        source_package=copy.deepcopy(c.row['source_package']), payload=payload)
        need(len(canonical(response)) <= MAX_RESPONSE, 'response exceeds bound')
        return response

    def inspect_restore(self, payload):
        """Read historical proof only; a different wire attempt stays UNKNOWN."""
        c, t = self.context, self.context.transport
        need(c.phase == 'AFTER', 'restore inspection requires after context')
        t.M.exact(payload, {'effect', 'wire_sha256'}, 'restore inspection')
        effect, wire_hash = payload['effect'], payload['wire_sha256']
        t.M.exact(effect, t.REQUEST_FIELDS, 'inspection effect')
        need(effect['tx'] == c.plan['tx'] and effect['plan_sha256'] == t.M.digest(c.plan)
             and effect['node'] == c.row['node'] and effect['boot_id'] == c.row['boot_id']
             and effect['unit'] in t.M.MASKED_BY_BARRIER and effect['operation'] in ('UNMASK', 'START')
             and t.M.sha(wire_hash), 'inspection identity differs')
        result = {'schema': 'slt-exact-restore-inspection/v1', 'authority': 'NONE', 'state': 'UNKNOWN',
                  'request_sha256': t.M.digest(effect), 'wire_sha256': wire_hash,
                  'intent_sha256': None, 'receipt': None, 'effect_retry_allowed': False}
        key = effect['node'] + ':' + effect['unit'] + ':' + effect['operation']
        with c.local.LocalJournal(c.plan, create=False) as journal:
            intent, receipt = (journal.read(effect['tx'], key + suffix) for suffix in (':intent', ':done'))
        if intent is None: return result
        t.M.exact(intent, {'schema', 'authority', 'request', 'executor_identity', 'before', 'grant', 'process'}, 'inspection intent')
        expected = copy.deepcopy(c.local.installed_identity())
        expected['transport'] = {'schema': 'slt-exact-wire-executor/v1', 'authority': 'NONE',
            'wire_sha256': wire_hash, 'pins_sha256': t.M.digest(c.pins),
            'sources_sha256': c.row['source_package']['sources_sha256'], 'helper_sha256': c.row['helper_sha256']}
        if (intent['schema'] != 'slt-local-restore-intent/v1' or intent['authority'] != 'NONE'
                or intent['request'] != effect or intent['executor_identity'] != expected): return result
        result['intent_sha256'] = t.M.digest(intent)
        if receipt is None: return result
        t.validate_receipt(receipt, effect, c.plan, int(time.time()))
        grant = intent['grant']
        t.M.exact(grant, {'schema', 'request_sha256', 'executor_sha256', 'before_sha256', 'after_source_sha256',
                          'issued_at', 'expires_at', 'allowed_effect', 'authorization_sha256'}, 'inspection grant')
        need(grant['schema'] == 'slt-local-restore-explicit-grant/v1' and grant['allowed_effect'] == effect['operation']
             and t.M.sha(grant['authorization_sha256']) and type(grant['issued_at']) is int
             and type(grant['expires_at']) is int and 0 < grant['expires_at'] - grant['issued_at'] <= 30
             and receipt['intent_sha256'] == t.M.digest(intent) and grant['request_sha256'] == t.M.digest(effect)
             and grant['executor_sha256'] == t.M.digest(expected) and grant['before_sha256'] == t.M.digest(intent['before'])
             and grant['issued_at'] <= receipt['started_at'] <= grant['expires_at']
             and t.M.digest(receipt['after']['source']) == grant['after_source_sha256'], 'inspection receipt binding differs')
        result.update(state='COMPLETED_HISTORICAL', receipt=copy.deepcopy(receipt))
        return result

    def entry(self, envelope):
        c, t = self.context, self.context.transport
        need(c.phase == 'BEFORE' and callable(self.entry_authorizer), 'independent entry grant absent')
        runner = c.node_runner
        plan = c.local.C.legacy_plan(c.plan)
        request = envelope['payload']
        t.M.exact(request, {'tx', 'plan_sha256', 'node', 'operation'}, 'entry effect')
        need(request == {'tx': c.plan['tx'], 'plan_sha256': runner.B.MODEL.frozen_hash(plan),
                         'node': envelope['node'], 'operation': envelope['kind']}, 'entry projection differs')
        def admitted():
            c.check_package()
            grant = self.entry_authorizer(copy.deepcopy(envelope), copy.deepcopy(c.pins))
            t.M.exact(grant, {'schema', 'authority', 'wire_sha256', 'pins_sha256', 'sources_sha256',
                             'issued_at', 'expires_at', 'operation'}, 'entry grant')
            now = int(time.time())
            need(grant['schema'] == 'slt-exact-entry-grant/v1' and grant['authority'] == 'NONE'
                 and grant['wire_sha256'] == t.M.digest(envelope) and grant['pins_sha256'] == t.M.digest(c.pins)
                 and grant['sources_sha256'] == c.row['source_package']['sources_sha256']
                 and grant['operation'] == envelope['kind']
                 and type(grant['issued_at']) is int and type(grant['expires_at']) is int
                 and c.plan['issued_at'] <= grant['issued_at'] <= now <= grant['expires_at'] <= c.plan['expires_at']
                 and 0 < grant['expires_at'] - grant['issued_at'] <= 30, 'entry grant binding/lifetime')
            return grant
        grant = admitted()
        wire_intent = c.reserve_entry(copy.deepcopy(envelope), copy.deepcopy(grant))
        def execute(argv):
            need(admitted() == grant, 'entry admission drift')
            return runner.execute(argv)
        agent = runner.B.NodeAgent(plan, envelope['node'], lambda: runner.C.collect(plan, c.workload),
                                   execute, lambda: int(time.time()))
        response = runner.L.DurableNodeAgent(agent).request(copy.deepcopy(request))
        attempt = runner.L.inspect(request)
        need(attempt['state'] == 'COMPLETED_HISTORICAL', 'entry completion not durable')
        return {'schema': 'slt-exact-entry-wire-result/v1', 'authority': 'NONE', 'state': 'OBSERVED_COMPLETE',
                'request_sha256': hashlib.sha256(runner.L.canonical(request)).hexdigest(),
                'attempt_nonce': runner.L.request_nonce(request), 'attempt': attempt, 'response': response,
                'wire_intent_sha256': wire_intent,
                'effect_retry_allowed': False}


def read_one(fd=0):
    selector = selectors.DefaultSelector(); selector.register(fd, selectors.EVENT_READ)
    end, raw = time.monotonic() + 3, bytearray()
    try:
        while True:
            remaining = end - time.monotonic()
            need(remaining > 0 and selector.select(remaining), 'stdin framing deadline')
            block = os.read(fd, min(4096, MAX_REQUEST + 1 - len(raw)))
            if not block: break
            raw.extend(block); need(len(raw) <= MAX_REQUEST, 'request too large')
        return strict(bytes(raw), MAX_REQUEST)
    finally: selector.close()


def main():
    """No flags, no env config, no default effect authority, exactly one response."""
    raw = canonical({'schema': 'slt-exact-transport-error/v1', 'authority': 'NONE', 'state': 'REFUSED_OR_UNKNOWN'})
    code = 2
    try:
        root_required()
        need(sys.flags.isolated == 1 and sys.argv == [str(INSTALLED / ENTRY)], 'fixed Python -I subsystem entry required')
        need(not os.environ.get('SSH_ORIGINAL_COMMAND'), 'caller command forbidden')
        os.environ.clear(); os.environ.update(ENV)
        os.umask(0o077); sys.dont_write_bytecode = True
        def deadline(*_): raise Refusal('subsystem deadline; outcome unknown')
        signal.signal(signal.SIGALRM, deadline); signal.alarm(18)
        with open(os.devnull, 'w') as discard, contextlib.redirect_stdout(discard), contextlib.redirect_stderr(discard):
            request = read_one()
            # Deliberately no authorizer: production effects stay closed until
            # a separately reviewed integration supplies an independent grant.
            server = fresh_server()
            response = server.Engine(server.FixedContext()).handle(request)
        raw, code = canonical(response), 0
    except BaseException:
        pass
    finally:
        # Raw-fd output avoids interpreter buffered prints contaminating JSON.
        # A lost/partial write is UNKNOWN; no second response/retry is sent.
        try:
            signal.alarm(2)
            while raw:
                count = os.write(1, raw); need(count > 0, 'short stdout write'); raw = raw[count:]
        except BaseException: code = 2
        finally:
            for fd in (0, 1, 2):
                try: os.close(fd)
                except OSError: pass
    return code


if __name__ == '__main__':
    os._exit(main())
