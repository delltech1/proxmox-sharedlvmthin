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
    reserve = request.get('reserve_percent', 0)
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
    if min(path_counts) > 64:
        raise Refusal('minimum path count exceeds plugin limit of 64')
    existing = evidence.get('storage_definitions')
    if not isinstance(existing, list):
        raise Refusal('storage definitions unavailable')
    seen_ids = set()
    for item in existing:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            raise Refusal('storage definition ambiguous')
        if item['id'] in seen_ids:
            raise Refusal('duplicate storage definition')
        seen_ids.add(item['id'])
        same_name = item.get('vg_name') == vg
        existing_uuid = item.get('vg_uuid')
        existing_pv = item.get('pv_uuid')
        existing_wwid = item.get('wwid')
        if ((existing_uuid is not None and not isinstance(existing_uuid, str)) or
                (existing_pv is not None and not isinstance(existing_pv, str)) or
                (existing_wwid is not None and not isinstance(existing_wwid, str))):
            raise Refusal('storage identity is ambiguous')
        same_uuid = isinstance(existing_uuid, str) and existing_uuid.casefold() == vg_uuid.casefold()
        same_pv = isinstance(existing_pv, str) and existing_pv == pv_uuid
        same_wwid = isinstance(existing_wwid, str) and existing_wwid.casefold() == wwid.casefold()
        requested_id = item['id'] in aliases.values()
        if requested_id or same_name or same_uuid or same_pv or same_wwid:
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


# Private proof-of-concept: a missing sibling may be proposed only while the
# original VG is still empty on every observed node. Config must come from a
# complete, stable PVE configuration snapshot; these inputs are not authority.
RESUME_NUMERIC = {
    'slt-expected-min-paths': (1, 64), 'slt-vg-reserve-percent': (0, 50),
    'slt-vg-reserve-gib': (0, 1048576), 'slt-lock-timeout': (10, 86400),
    'slt-lock-yield-ms': (0, 5000), 'slt-bridge-admission-timeout': (60, 604800),
    'slt-tg-close-timeout': (1, 300), 'slt-tg-command-deadline-sec': (5, 600),
    'slt-tg-max-active-materializations': (1, 64), 'slt-tg-region-size-kib': (64, 4096),
    'slt-tg-hydration-timeout': (60, 86400), 'slt-tg-hydration-threshold': (1, 256),
    'slt-tg-hydration-batch-size': (1, 256), 'slt-mutation-admission-timeout': (10, 86400),
}
RESUME_IDENTITY = {'slt-vgname', 'slt-vg-layout', 'slt-allocation-mode',
                   'slt-expected-vg-uuid', 'slt-expected-pv-uuid', 'slt-expected-wwid'}
RESUME_ENUM = {'slt-tg-online-materialization': {'asynchronous', 'synchronous'}}


def resume_plan(request, evidence, config):
    """Build a reviewed sibling preview; no storage writes or authority."""
    if not isinstance(config, dict) or evidence.get('config_snapshot_complete') is not True:
        raise Refusal('complete stable storage configuration required')
    sid = _name(config.get('storage'), 'existing storage ID')
    new_id = _name(request.get('missing_id'), 'missing storage ID')
    if new_id == sid or config.get('type') != 'sharedlvmthin' \
            or type(config.get('shared')) not in (str, int) or config.get('shared') not in (1, '1') \
            or type(config.get('disable', 0)) not in (str, int) or config.get('disable', 0) not in (0, '0'):
        raise Refusal('existing alias is not an enabled shared Thick storage')
    content = config.get('content')
    if not isinstance(content, str) or sorted(content.split(',')) != ['images', 'rootdir']:
        raise Refusal('explicit matching images and rootdir content required')
    # An explicit node list avoids guessing what an unscoped storage means.
    nodes = request.get('nodes')
    node_config = config.get('nodes')
    if not isinstance(nodes, list) or not isinstance(node_config, str):
        raise Refusal('explicit node scope required')
    configured_nodes = node_config.split(',')
    if any(not n or n.strip() != n for n in configured_nodes) \
            or len(set(configured_nodes)) != len(configured_nodes) \
            or sorted(configured_nodes) != sorted(nodes):
        raise Refusal('node scope mismatch')
    if config.get('slt-vg-layout') != 'isolated':
        raise Refusal('explicit isolated layout required')
    modes = {'thick-generations': 'lazy', 'thick-generations-lazy': 'eager'}
    if config.get('slt-allocation-mode') not in modes:
        raise Refusal('explicit Thick allocation mode required')
    if config.get('slt-vgname') != request.get('vg'):
        raise Refusal('VG name mismatch')
    for key, field in (('slt-expected-vg-uuid', 'vg_uuid'),
                       ('slt-expected-pv-uuid', 'pv_uuid'), ('slt-expected-wwid', 'wwid')):
        if config.get(key) != evidence.get('vg', {}).get(field):
            raise Refusal('existing alias identity differs from observed VG')
    if not any(k in config for k in ('slt-vg-reserve-percent', 'slt-vg-reserve-gib')):
        raise Refusal('existing alias reserve is absent')
    unknown = {key for key in config if key.startswith('slt-')} - RESUME_IDENTITY \
              - set(RESUME_NUMERIC) - set(RESUME_ENUM)
    if unknown:
        raise Refusal('unreviewed storage policy: ' + ','.join(sorted(unknown)))
    generic = set(config) - RESUME_IDENTITY - set(RESUME_NUMERIC) - set(RESUME_ENUM)
    if generic - {'storage', 'type', 'nodes', 'content', 'shared', 'disable', 'comment'}:
        raise Refusal('unreviewed generic storage policy')
    numeric = {}
    for key, (low, high) in RESUME_NUMERIC.items():
        if key in config:
            value = config[key]
            if type(value) not in (str, int) or not re.fullmatch(r'0|[1-9][0-9]*', str(value)) \
                    or not low <= int(value) <= high:
                raise Refusal('invalid existing policy: ' + key)
            numeric[key] = int(value)
    for key, allowed in RESUME_ENUM.items():
        if key in config and config[key] not in allowed:
            raise Refusal('invalid existing policy: ' + key)
    if evidence.get('vg', {}).get('empty_on_all_nodes') is not True \
            or any(evidence.get('node_evidence', {}).get(node, {}).get('empty_vg') is not True for node in nodes):
        raise Refusal('empty VG not proven on every node')
    existing = evidence.get('storage_definitions')
    if not isinstance(existing, list):
        raise Refusal('storage definitions unavailable')
    matches = [row for row in existing if isinstance(row, dict) and row.get('id') == sid]
    if len(matches) != 1:
        raise Refusal('existing alias is not unique')
    old = matches[0]
    expected_old = {'type': 'sharedlvmthin', 'vg_name': request['vg'],
                    'vg_uuid': config['slt-expected-vg-uuid'],
                    'pv_uuid': config['slt-expected-pv-uuid'], 'wwid': config['slt-expected-wwid'],
                    'allocation_mode': config['slt-allocation-mode']}
    if any(old.get(key) != value for key, value in expected_old.items()):
        raise Refusal('existing alias observation does not match full config')
    trimmed = dict(evidence, storage_definitions=[row for row in existing if row is not old])
    mode = modes[config['slt-allocation-mode']]
    checked = plan({'nodes': nodes, 'vg': request['vg'], 'aliases': {mode: new_id},
                    'reserve_percent': numeric.get('slt-vg-reserve-percent', 0)}, trimmed)
    if numeric.get('slt-expected-min-paths', 1) > checked['expected_min_paths']:
        raise Refusal('observed paths do not meet the existing alias minimum')
    cfg = checked['definitions'][0]['api_parameters']
    # Preserve absence as absence; it is not interchangeable with explicit
    # zero for reserve or min paths. Other absent keys retain plugin defaults.
    for key in ('slt-expected-min-paths', 'slt-vg-reserve-percent'):
        cfg.pop(key, None)
    cfg.update(numeric)
    for key in RESUME_ENUM:
        if key in config:
            cfg[key] = config[key]
    argv = ['pvesm', 'add', 'sharedlvmthin', new_id]
    for key, value in cfg.items():
        if key not in ('type', 'storage'):
            argv.extend(['--' + key, str(value)])
    visible_config = {key: value for key, value in config.items() if key != 'comment'}
    return {'result': 'PREVIEW_ONLY', 'authority': 'NONE', 'existing_id': sid,
            'existing_config': visible_config,
            'missing_mode': mode, 'api_parameters': cfg, 'argv': argv, 'cli': shlex.join(argv),
            'warning': 'Recheck configuration, emptiness and identities immediately before use; this preview is not fencing or authorization.'}


if __name__ == '__main__':
    try:
        data = json.load(sys.stdin)
        print(json.dumps(plan(data['request'], data['evidence']), sort_keys=True))
    except (Refusal, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({'result': 'REFUSED', 'reason': str(exc)}))
        sys.exit(1)
