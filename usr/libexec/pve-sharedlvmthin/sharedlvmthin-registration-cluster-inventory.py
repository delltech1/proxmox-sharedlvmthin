#!/usr/bin/python3
"""Read-only cluster correlation for Thick registration previews.

No device initialization, login, multipath changes, or storage writes. This
is evidence for a preview only; it is not fencing or a mutation authorization.
"""
import ipaddress
import hashlib
import datetime
import json
import os
import pathlib
import re
import selectors
import shlex
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


class InventoryError(ValueError):
    pass


MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024


def require_fresh_duration(elapsed):
    if type(elapsed) not in (float, int) or not 0 <= elapsed < 300:
        raise InventoryError('cluster discovery exceeded observation freshness limit; retry when ready')


def run_json(args, timeout=45):
    """Bound both pipes and wait time; timeout does not fence remote children."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise InventoryError('read-only inventory command failed')
    process = None
    try:
        deadline = time.monotonic() + timeout
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0,
                                   env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        limits = {'stdout': MAX_STDOUT_BYTES, 'stderr': MAX_STDERR_BYTES}
        with selectors.DefaultSelector() as selector:
            for name in ('stdout', 'stderr'):
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                if not selector.get_map():
                    # Pipes may close before the child exits.
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        raise
                    break
                for key, _ in selector.select(min(remaining, 0.25)):
                    try:
                        chunk = os.read(key.fileobj.fileno(), min(65536, limits[key.data] - len(buffers[key.data]) + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        buffers[key.data].extend(chunk)
                        if len(buffers[key.data]) > limits[key.data]:
                            raise InventoryError('read-only inventory output exceeded limit')
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, args)
        return json.loads(buffers['stdout'].decode('utf-8'))
    except (subprocess.SubprocessError, OSError, UnicodeError, ValueError) as exc:
        raise InventoryError('read-only inventory command failed') from exc
    finally:
        if process is not None:
            # Terminate only the direct child. Remote descendants may continue;
            # timeout is not proof of their exit and is never mutation authority.
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def members_from_status(status):
    if not isinstance(status, list) or any(not isinstance(row, dict) for row in status):
        raise InventoryError('invalid cluster status')
    cluster = [row for row in status if row.get('type') == 'cluster']
    if len(cluster) != 1 or cluster[0].get('quorate') != 1:
        raise InventoryError('cluster quorum not proven')
    result = {}
    for row in status:
        if row.get('type') != 'node':
            continue
        name, address = row.get('name'), row.get('ip')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name)
                or name in result or row.get('online') != 1):
            raise InventoryError('cluster node unavailable or ambiguous')
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError as exc:
            raise InventoryError('invalid cluster peer address') from exc
        result[name] = address
    if not result or cluster[0].get('nodes') != len(result):
        raise InventoryError('cluster membership incomplete')
    return result


def correlate(members, observations, storage):
    if not isinstance(storage, list) or any(not isinstance(x, dict) for x in storage):
        raise InventoryError('PVE storage inventory incomplete')
    if not isinstance(members, dict) or not isinstance(observations, dict) or set(observations) != set(members):
        raise InventoryError('node observations incomplete')
    definitions = []
    seen = set()
    for entry in storage:
        sid, kind = entry.get('storage'), entry.get('type')
        if not isinstance(sid, str) or not isinstance(kind, str) or sid in seen:
            raise InventoryError('PVE storage definition ambiguous')
        seen.add(sid)
        vg_name = entry.get('slt-vgname') if kind == 'sharedlvmthin' else entry.get('vgname')
        if kind in ('sharedlvmthin', 'lvm', 'lvmthin') and not isinstance(vg_name, str):
            raise InventoryError('PVE storage VG association unavailable')
        definitions.append({'id': sid, 'type': kind, 'vg_name': vg_name,
                            'vg_uuid': entry.get('slt-expected-vg-uuid'),
                            'pv_uuid': entry.get('slt-expected-pv-uuid'),
                            'wwid': entry.get('slt-expected-wwid'),
                            'allocation_mode': entry.get('slt-allocation-mode')})
    by_node = {}
    for node, result in observations.items():
        if not isinstance(result, dict) or result.get('result') != 'OBSERVED' or not isinstance(result.get('candidates'), list):
            raise InventoryError('node inventory unavailable')
        candidates = result['candidates']
        if any(not isinstance(c, dict) for c in candidates):
            raise InventoryError('invalid node candidate')
        # Check every observed candidate, including ones absent on peers.
        # Otherwise filtering to common VGs could hide a conflicting identity.
        for field in ('vg_uuid', 'pv_uuid', 'wwid'):
            values = [c.get(field) for c in candidates]
            if any(not isinstance(v, str) or not v for v in values):
                raise InventoryError('candidate identity incomplete')
            normalized = [v.casefold() if field in ('vg_uuid', 'wwid') else v for v in values]
            if len(set(normalized)) != len(normalized):
                raise InventoryError('conflicting candidate ' + field)
        ids = [c.get('vg_uuid') for c in candidates]
        if any(not isinstance(v, str) or not v for v in ids) or len(ids) != len(set(ids)):
            raise InventoryError('ambiguous VG identities')
        by_node[node] = {c['vg_uuid']: c for c in candidates}
    common = set.intersection(*(set(v) for v in by_node.values()))
    all_ids = set.union(*(set(v) for v in by_node.values()))
    unverified_reasons = set()
    if all_ids - common:
        unverified_reasons.add('vg_not_visible_on_every_node')
    candidates = []
    for uuid in sorted(common):
        observations_for_vg = {node: values[uuid] for node, values in by_node.items()}
        ref = next(iter(observations_for_vg.values()))
        identity = ('name', 'vg_uuid', 'pv_uuid', 'wwid', 'size_bytes', 'pv_count')
        if any(any(row.get(key) != ref.get(key) for key in identity)
               for row in observations_for_vg.values()):
            unverified_reasons.add('identity_mismatch')
            continue
        if any(type(row.get('healthy_paths')) is not int or row['healthy_paths'] < 1
               for row in observations_for_vg.values()):
            unverified_reasons.add('healthy_paths_unverified')
            continue
        def same_fold(left, right):
            return isinstance(left, str) and isinstance(right, str) and left.casefold() == right.casefold()
        aliases = [d for d in definitions if d.get('vg_name') == ref['name']
                   or same_fold(d.get('vg_uuid'), uuid)
                   or (isinstance(d.get('pv_uuid'), str) and d['pv_uuid'] == ref.get('pv_uuid'))
                   or same_fold(d.get('wwid'), ref.get('wwid'))]
        # Existing LVs need the plugin's full provenance validation; do not
        # treat their names or tags alone as evidence of exclusive ownership.
        empty_everywhere = all(row.get('empty_vg') is True for row in observations_for_vg.values())
        canonical_everywhere = all(row.get('canonical_mapper_present') is True for row in observations_for_vg.values())
        candidates.append({'name': ref['name'], 'vg_uuid': uuid, 'pv_uuid': ref['pv_uuid'],
                           'wwid': ref['wwid'], 'size_bytes': ref['size_bytes'],
                           'free_bytes': min(row['free_bytes'] for row in observations_for_vg.values()),
                           'paths_by_node': {n: row['healthy_paths'] for n, row in observations_for_vg.items()},
                           'transport_by_node': {n: row.get('transport', 'mixed-or-unknown') for n, row in observations_for_vg.items()},
                           'existing_aliases': aliases, 'empty_on_all_nodes': empty_everywhere,
                           'canonical_mapper_on_all_nodes': canonical_everywhere,
                           'eligible_for_new_vg_registration': empty_everywhere and canonical_everywhere and not aliases})
    return {'result': 'OBSERVED', 'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'nodes': sorted(members), 'candidates': candidates,
            'unverified_reasons': sorted(unverified_reasons),
            'storage_definitions': definitions,
            'notice': 'Read-only observation; not fencing or authorization to mutate storage.'}


def ssh_command(node, address):
    return ['/usr/bin/ssh', '-n', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-o', 'ConnectTimeout=5', '-o', 'HostKeyAlias=' + node,
            '-o', 'UserKnownHostsFile=/etc/pve/nodes/' + node + '/ssh_known_hosts',
            '-o', 'GlobalKnownHostsFile=none', 'root@' + address]


def require_stable_snapshot(before_members, after_members, before_config, after_config):
    if before_members != after_members or before_config != after_config:
        raise InventoryError('cluster membership or storage configuration changed during discovery')


def validate_evidence(report, evidence):
    """Recheck in-process observations; not authentication or a storage lock."""
    if not isinstance(report, dict) or not isinstance(evidence, dict):
        raise InventoryError('cluster evidence unavailable')
    before = members_from_status(evidence.get('status_before'))
    after = members_from_status(evidence.get('status_after'))
    if before != evidence.get('members_before') or after != evidence.get('members_after'):
        raise InventoryError('cluster status evidence inconsistent')
    if not isinstance(evidence.get('config_before'), str) or not re.fullmatch(r'[0-9a-f]{64}', evidence['config_before']):
        raise InventoryError('storage configuration digest unavailable')
    require_stable_snapshot(before, after, evidence.get('config_before'), evidence.get('config_after'))
    started = evidence.get('started_monotonic')
    if type(started) not in (int, float):
        raise InventoryError('observation start unavailable')
    require_fresh_duration(time.monotonic() - started)
    wall_start = evidence.get('started_wall_utc')
    if not isinstance(wall_start, str) or report.get('observed_at') != wall_start:
        raise InventoryError('observation timestamp inconsistent')
    try:
        parsed_start = datetime.datetime.fromisoformat(wall_start)
        if parsed_start.utcoffset() != datetime.timedelta(0) or parsed_start.isoformat() != wall_start:
            raise ValueError('timestamp is not canonical UTC')
    except ValueError as exc:
        raise InventoryError('observation timestamp invalid') from exc
    expected_pairs = {(a, b) for a in before for b in before if a != b}
    if evidence.get('peer_pairs') != expected_pairs:
        raise InventoryError('peer SSH coverage incomplete')
    reconstructed = correlate(before, evidence.get('observations'), evidence.get('storage'))
    if {k: v for k, v in report.items() if k != 'observed_at'} != {k: v for k, v in reconstructed.items() if k != 'observed_at'}:
        raise InventoryError('cluster report differs from observed evidence')


def verify_peer_matrix(members, local, runner=subprocess.run):
    """Prove current PVE-pinned SSH authentication from each peer to each peer.

    This does not prove future reachability, authorization to mutate, or
    fencing. Bounded concurrency avoids a burst of sessions on large clusters.
    """
    if not isinstance(members, dict) or not members or local not in members:
        raise InventoryError('invalid peer SSH matrix membership')
    failed = threading.Event()
    def probe(pair):
        if failed.is_set():
            raise InventoryError('peer SSH inventory cancelled after failure')
        source, target = pair
        target_cmd = ssh_command(target, members[target]) + ['/usr/bin/true']
        args = target_cmd if source == local else ssh_command(source, members[source]) + [shlex.join(target_cmd)]
        try:
            try:
                result = runner(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20, check=False,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
            except (subprocess.SubprocessError, OSError) as exc:
                raise InventoryError('peer SSH proof unavailable') from exc
            if result.returncode != 0:
                raise InventoryError('peer SSH proof failed for ' + source + ' to ' + target)
        except Exception as exc:
            failed.set()
            if isinstance(exc, InventoryError):
                raise
            raise InventoryError('peer SSH proof unavailable') from exc
        except BaseException:
            failed.set()
            raise
    pairs = [(a, b) for a in sorted(members) for b in sorted(members) if a != b]
    pool = ThreadPoolExecutor(max_workers=4)
    futures = []
    try:
        futures = [pool.submit(probe, pair) for pair in pairs]
        for future in as_completed(futures):
            future.result()
    except BaseException:
        failed.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return frozenset(pairs)


def collect_node_observations(members, local, helper, runner=None):
    """Read each participating node once, with bounded parallel fan-out."""
    if not isinstance(members, dict) or not members or local not in members or not isinstance(helper, str) or not helper:
        raise InventoryError('invalid node inventory request')
    if runner is None:
        runner = run_json
    failed = threading.Event()
    def probe(item):
        if failed.is_set():
            raise InventoryError('node inventory cancelled after a peer failure')
        node, address = item
        if node == local:
            args = ['/usr/bin/python3', '-c', helper]
        else:
            args = ssh_command(node, address) + ['/usr/bin/python3 -c ' + shlex.quote(helper)]
        try:
            return node, runner(args, timeout=60)
        except BaseException:
            failed.set()
            raise
    # Observe failures as they complete, instead of waiting behind an earlier
    # slow node in lexical order. Cancel work that has not started; already
    # running read-only probes must finish or reach their own timeout.
    pool = ThreadPoolExecutor(max_workers=4)
    futures = []
    try:
        futures = [pool.submit(probe, item) for item in sorted(members.items())]
        results = {}
        for future in as_completed(futures):
            node, value = future.result()
            results[node] = value
        return {node: results[node] for node in sorted(results)}
    except BaseException:
        for future in futures:
            future.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def collect_evidence():
    # observed_at must describe the oldest possible evidence, not the end of
    # a potentially slow multi-node scan. A slow scan is not a failed disk.
    started = datetime.datetime.now(datetime.timezone.utc)
    started_monotonic = time.monotonic()
    status_before = run_json(['/usr/bin/pvesh', 'get', '/cluster/status', '--output-format', 'json'])
    members = members_from_status(status_before)
    config_path = pathlib.Path('/etc/pve/storage.cfg')
    before_config = hashlib.sha256(config_path.read_bytes()).hexdigest()
    storage = run_json(['/usr/bin/pvesh', 'get', '/storage', '--output-format', 'json'])
    helper = pathlib.Path(__file__).with_name('sharedlvmthin-registration-local-inventory.py').read_text(encoding='utf-8')
    local = os.uname().nodename.split('.')[0]
    if local not in members:
        raise InventoryError('local node missing from cluster membership')
    peer_pairs = verify_peer_matrix(members, local)
    observations = collect_node_observations(members, local, helper)
    # A concurrent config or membership change invalidates the preview.
    status_after = run_json(['/usr/bin/pvesh', 'get', '/cluster/status', '--output-format', 'json'])
    current_members = members_from_status(status_after)
    after_config = hashlib.sha256(config_path.read_bytes()).hexdigest()
    if not isinstance(storage, list) or any(not isinstance(row, dict) for row in storage):
        raise InventoryError('PVE storage inventory incomplete')
    require_stable_snapshot(members, current_members, before_config, after_config)
    elapsed = time.monotonic() - started_monotonic
    require_fresh_duration(elapsed)
    result = correlate(members, observations, storage)
    result['observed_at'] = started.isoformat()
    return result, {'members_before': members, 'members_after': current_members,
                    'status_before': status_before, 'status_after': status_after,
                    'config_before': before_config, 'config_after': after_config,
                    'peer_pairs': peer_pairs, 'observations': observations, 'storage': storage,
                    'started_monotonic': started_monotonic, 'started_wall_utc': started.isoformat()}


def collect():
    # Preserve the existing public, read-only report schema.
    return collect_evidence()[0]


if __name__ == '__main__':
    try:
        print(json.dumps(collect(), sort_keys=True))
    except (InventoryError, OSError, ValueError) as exc:
        print(json.dumps({'result': 'UNKNOWN', 'reason': str(exc)}))
        sys.exit(1)
