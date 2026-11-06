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
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


class InventoryError(ValueError):
    pass


def run_json(args, timeout=45):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, check=True, timeout=timeout,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise InventoryError('read-only inventory command failed') from exc


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
    if set(observations) != set(members):
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
        aliases = [d for d in definitions if d.get('vg_name') == ref['name'] or d.get('vg_uuid') == uuid]
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
            '-o', 'ConnectTimeout=5', '-o', 'HostKeyAlias=' + node,
            '-o', 'UserKnownHostsFile=/etc/pve/nodes/' + node + '/ssh_known_hosts',
            '-o', 'GlobalKnownHostsFile=none', 'root@' + address]


def require_stable_snapshot(before_members, after_members, before_config, after_config):
    if before_members != after_members or before_config != after_config:
        raise InventoryError('cluster membership or storage configuration changed during discovery')


def verify_peer_matrix(members, local, runner=subprocess.run):
    """Prove current PVE-pinned SSH authentication from each peer to each peer.

    This does not prove future reachability, authorization to mutate, or
    fencing. Bounded concurrency avoids a burst of sessions on large clusters.
    """
    def probe(pair):
        source, target = pair
        target_cmd = ssh_command(target, members[target]) + ['/usr/bin/true']
        args = target_cmd if source == local else ssh_command(source, members[source]) + [shlex.join(target_cmd)]
        try:
            result = runner(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=20, check=False,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        except (subprocess.SubprocessError, OSError) as exc:
            raise InventoryError('peer SSH proof unavailable') from exc
        if result.returncode != 0:
            raise InventoryError('peer SSH proof failed for ' + source + ' to ' + target)
    pairs = [(a, b) for a in sorted(members) for b in sorted(members) if a != b]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(probe, pairs))


def collect():
    members = members_from_status(run_json(['/usr/bin/pvesh', 'get', '/cluster/status', '--output-format', 'json']))
    config_path = pathlib.Path('/etc/pve/storage.cfg')
    before_config = hashlib.sha256(config_path.read_bytes()).hexdigest()
    storage = run_json(['/usr/bin/pvesh', 'get', '/storage', '--output-format', 'json'])
    helper = pathlib.Path(__file__).with_name('sharedlvmthin-registration-local-inventory.py').read_text(encoding='utf-8')
    local = os.uname().nodename.split('.')[0]
    if local not in members:
        raise InventoryError('local node missing from cluster membership')
    verify_peer_matrix(members, local)
    observations = {}
    for node, address in sorted(members.items()):
        if node == local:
            args = ['/usr/bin/python3', '-c', helper]
        else:
            args = ssh_command(node, address) + ['/usr/bin/python3 -c ' + shlex.quote(helper)]
        observations[node] = run_json(args, timeout=60)
    # A concurrent config or membership change invalidates the preview.
    current_members = members_from_status(run_json(['/usr/bin/pvesh', 'get', '/cluster/status', '--output-format', 'json']))
    after_config = hashlib.sha256(config_path.read_bytes()).hexdigest()
    if not isinstance(storage, list) or any(not isinstance(row, dict) for row in storage):
        raise InventoryError('PVE storage inventory incomplete')
    require_stable_snapshot(members, current_members, before_config, after_config)
    return correlate(members, observations, storage)


if __name__ == '__main__':
    try:
        print(json.dumps(collect(), sort_keys=True))
    except (InventoryError, OSError, ValueError) as exc:
        print(json.dumps({'result': 'UNKNOWN', 'reason': str(exc)}))
        sys.exit(1)
