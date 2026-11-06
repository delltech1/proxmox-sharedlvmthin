#!/usr/bin/python3
"""Read-only Thick registration plan; never executes storage mutations.

The caller must collect fresh authenticated evidence. A plan is not an
authorization to apply: apply must re-collect evidence under the PVE config
lock and enforce the requester's permissions at the mutation boundary.
"""
import json
import re
import shlex
import sys


class Refusal(ValueError):
    pass


def _name(value, kind):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise Refusal('invalid ' + kind)
    return value


def _uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}', value):
        raise Refusal('invalid LVM UUID')
    return value


def plan(request, evidence):
    if not isinstance(request, dict) or not isinstance(evidence, dict):
        raise Refusal('invalid input')
    if evidence.get('quorate') is not True or evidence.get('inventory_complete') is not True:
        raise Refusal('quorum or inventory not proven')
    nodes = request.get('nodes')
    if not isinstance(nodes, list) or not nodes or any(not isinstance(n, str) for n in nodes) or len(nodes) != len(set(nodes)):
        raise Refusal('invalid selected nodes')
    nodes = sorted(_name(n, 'node') for n in nodes)
    members = evidence.get('members')
    if not isinstance(members, dict) or any(members.get(n) is not True for n in nodes):
        raise Refusal('selected node unavailable')
    vg = _name(request.get('vg'), 'VG')
    aliases = request.get('aliases')
    if not isinstance(aliases, dict) or not aliases or set(aliases) - {'eager', 'lazy'}:
        raise Refusal('invalid modes')
    aliases = {mode: _name(sid, 'storage ID') for mode, sid in aliases.items()}
    if len(set(aliases.values())) != len(aliases):
        raise Refusal('duplicate requested storage ID')
    reserve = request.get('reserve_percent', 5)
    if type(reserve) is not int or not 0 <= reserve <= 50:
        raise Refusal('new registration reserve must be between 0 and 50 percent')
    expected = evidence.get('vg')
    if not isinstance(expected, dict) or expected.get('name') != vg or expected.get('pv_count') != 1:
        raise Refusal('VG is not a proven single-PV VG')
    vg_uuid = _uuid(expected.get('vg_uuid'))
    pv_uuid = _uuid(expected.get('pv_uuid'))
    wwid = expected.get('wwid')
    if not isinstance(wwid, str) or not re.fullmatch(r'[0-9a-fA-F]{16,128}', wwid):
        raise Refusal('invalid WWID')
    size = expected.get('size_bytes')
    free = expected.get('free_bytes')
    if type(size) is not int or type(free) is not int or size <= 0 or not 0 <= free <= size:
        raise Refusal('invalid VG capacity')
    if expected.get('dedicated') is not True:
        raise Refusal('dedicated VG not proven')
    peers = evidence.get('node_evidence')
    if not isinstance(peers, dict):
        raise Refusal('peer evidence missing')
    path_counts = []
    for node in nodes:
        item = peers.get(node)
        if not isinstance(item, dict) or item.get('complete') is not True or item.get('peer_ssh_verified') is not True:
            raise Refusal('peer evidence incomplete')
        if any(item.get(k) != expected.get(k) for k in ('vg_uuid', 'pv_uuid', 'wwid', 'size_bytes')):
            raise Refusal('peer identity mismatch')
        paths = item.get('healthy_paths')
        if type(paths) is not int or paths < 1 or item.get('mapper_verified') is not True:
            raise Refusal('multipath identity or healthy path missing')
        path_counts.append(paths)
    existing = evidence.get('storage_definitions')
    if not isinstance(existing, list):
        raise Refusal('storage definitions unavailable')
    for item in existing:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            raise Refusal('storage definition ambiguous')
        same_name = item.get('vg_name') == vg
        same_uuid = item.get('vg_uuid') == vg_uuid
        requested_id = item['id'] in aliases.values()
        if requested_id or same_name or same_uuid:
            # A matching name and identity alone do not prove same-VG alias
            # policy parity. Until every effective plugin option is validated,
            # require manual review instead of inferring an idempotent match.
            raise Refusal('existing storage association requires full policy review')
        elif item.get('type') in ('sharedlvmthin', 'lvm', 'lvmthin') and not item.get('vg_name'):
            raise Refusal('storage VG association is ambiguous')
    result = []
    for mode, sid in sorted(aliases.items()):
        cfg = {'type': 'sharedlvmthin', 'storage': sid, 'slt-vgname': vg,
               'slt-allocation-mode': 'thick-generations' if mode == 'eager' else 'thick-generations-lazy',
               'slt-vg-layout': 'isolated', 'slt-expected-vg-uuid': vg_uuid,
               'slt-expected-pv-uuid': pv_uuid, 'slt-expected-wwid': wwid,
               'slt-expected-min-paths': min(path_counts), 'slt-vg-reserve-percent': reserve,
               'nodes': ','.join(nodes), 'shared': 1, 'content': 'images,rootdir'}
        args = ['pvesm', 'add', cfg['type'], sid]
        for key, value in cfg.items():
            if key not in ('type', 'storage'):
                args.extend(['--' + key, str(value)])
        result.append({'mode': mode, 'status': 'CREATE', 'api_parameters': cfg, 'cli': shlex.join(args)})
    return {'result': 'PREVIEW_ONLY', 'vg_size_bytes': size, 'vg_free_bytes': free,
            'expected_min_paths': min(path_counts), 'definitions': result}


if __name__ == '__main__':
    try:
        data = json.load(sys.stdin)
        print(json.dumps(plan(data['request'], data['evidence']), sort_keys=True))
    except (Refusal, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({'result': 'REFUSED', 'reason': str(exc)}))
        sys.exit(1)
