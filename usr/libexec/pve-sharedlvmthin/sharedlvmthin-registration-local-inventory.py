#!/usr/bin/python3
"""Read-only local multipath/LVM inventory for Thick registration.

An observation from this helper is not a cluster-wide safety proof. The
coordinator must independently query every relevant node, verify SSH identity,
quorum and the complete PVE storage configuration, and recheck before use.
"""
import decimal
import json
import os
import re
import subprocess
import sys


class InventoryError(ValueError):
    pass


def run_json(args):
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30, check=True, text=True,
                              env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        return json.loads(proc.stdout)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise InventoryError('inventory command failed: ' + args[0]) from exc


def rows(report, key):
    try:
        reports = report['report']
        if not isinstance(reports, list) or len(reports) != 1:
            raise ValueError()
        result = reports[0][key]
        if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise InventoryError('invalid ' + key + ' report') from exc


def number(value):
    try:
        n = decimal.Decimal(str(value).strip())
        if not n.is_finite() or n < 0 or n != n.to_integral_value():
            raise ValueError()
        return int(n)
    except (decimal.InvalidOperation, ValueError) as exc:
        raise InventoryError('invalid LVM numeric field') from exc


def dm_uuid(mapper, realpath=os.path.realpath):
    device = os.path.basename(realpath(mapper))
    if not re.fullmatch(r'dm-[0-9]+', device):
        raise InventoryError('mapper is not a device-mapper device')
    try:
        with open('/sys/class/block/' + device + '/dm/uuid', encoding='ascii') as stream:
            return stream.read().strip()
    except (OSError, UnicodeError) as exc:
        raise InventoryError('mapper UUID unavailable') from exc


def collect(run=run_json, realpath=os.path.realpath, isblock=None, mapper_uuid=dm_uuid):
    if isblock is None:
        import stat
        def isblock(path):
            try:
                return stat.S_ISBLK(os.stat(path).st_mode)
            except OSError:
                return False
    # Newly initialized shared devices may be absent from another node's LVM
    # hints.  Inventory must scan rather than report that stale view as fresh.
    vg_rows = rows(run(['/sbin/vgs', '--readonly', '--nohints', '--reportformat', 'json', '--units', 'b', '--nosuffix',
                        '-o', 'vg_name,vg_uuid,vg_size,vg_free,pv_count']), 'vg')
    pv_rows = rows(run(['/sbin/pvs', '--readonly', '--nohints', '--reportformat', 'json', '-o',
                        'pv_name,pv_uuid,vg_name,vg_uuid']), 'pv')
    lv_rows = rows(run(['/sbin/lvs', '--readonly', '--nohints', '-a', '--reportformat', 'json', '-o',
                        'vg_name,lv_name']), 'lv')
    scsi = run(['/usr/bin/lsblk', '--scsi', '--json', '-o', 'NAME,TRAN'])
    scsi = scsi.get('blockdevices') if isinstance(scsi, dict) else None
    if not isinstance(scsi, list) or any(not isinstance(row, dict) for row in scsi):
        raise InventoryError('invalid SCSI transport report')
    transports = {}
    for row in scsi:
        name = row.get('name')
        if not isinstance(name, str) or name in transports:
            raise InventoryError('ambiguous SCSI path name')
        transports[name] = row.get('tran')
    maps = run(['/sbin/multipathd', 'show', 'maps', 'json'])
    maps = maps.get('maps') if isinstance(maps, dict) else None
    if not isinstance(maps, list):
        raise InventoryError('invalid multipath map report')
    candidates = []
    for vg in vg_rows:
        name = vg.get('vg_name', '').strip()
        uuid = vg.get('vg_uuid', '').strip()
        associated = [p for p in pv_rows if p.get('vg_uuid', '').strip() == uuid]
        if not name or not uuid or number(vg.get('pv_count')) != 1 or len(associated) != 1:
            continue
        pv = associated[0]
        pv_name = pv.get('pv_name', '').strip()
        pv_uuid = pv.get('pv_uuid', '').strip()
        matching = []
        for item in maps:
            if not isinstance(item, dict):
                raise InventoryError('invalid multipath map')
            wwid = item.get('uuid')
            if not isinstance(wwid, str) or not re.fullmatch(r'[0-9a-fA-F]{16,128}', wwid):
                continue
            alias = item.get('name')
            if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}', alias):
                continue
            mapper = '/dev/mapper/' + alias
            if (isblock(mapper) and pv_name and realpath(mapper) == realpath(pv_name)
                    and mapper_uuid(mapper) == 'mpath-' + wwid):
                matching.append((item, wwid, mapper))
        if len(matching) != 1 or not pv_uuid:
            continue
        item, wwid, mapper = matching[0]
        canonical = '/dev/mapper/' + wwid
        canonical_present = isblock(canonical) and realpath(canonical) == realpath(mapper)
        groups = item.get('path_groups')
        if not isinstance(groups, list):
            raise InventoryError('invalid multipath path groups')
        paths = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('paths'), list):
                raise InventoryError('invalid multipath paths')
            paths.extend(group['paths'])
        if any(not isinstance(path, dict) for path in paths):
            raise InventoryError('invalid path observation')
        devices = [path.get('dev_t') for path in paths]
        if any(not isinstance(dev, str) or not re.fullmatch(r'[0-9]+:[0-9]+', dev) for dev in devices) or len(devices) != len(set(devices)):
            raise InventoryError('duplicate or missing path identity')
        healthy = sum(1 for path in paths if path.get('dm_st') == 'active'
                      and path.get('chk_st') == 'ready' and path.get('dev_st') == 'running')
        kinds = {transports.get(path.get('dev')) for path in paths}
        if kinds == {'iscsi'}:
            transport = 'iscsi'
        elif kinds == {'sas'}:
            transport = 'sas'
        elif kinds and kinds <= {'fc', 'fcoe'}:
            # lsblk can report either FC or FCoE.  An FC label alone does not
            # establish the physical fabric, so retain the combined hint.
            transport = 'fc-or-fcoe'
        else:
            transport = 'mixed-or-unknown'
        owned_lvs = [lv for lv in lv_rows if lv.get('vg_name', '').strip() == name]
        candidates.append({'name': name, 'vg_uuid': uuid, 'pv_uuid': pv_uuid, 'wwid': wwid,
                           'mapper': mapper, 'size_bytes': number(vg.get('vg_size')),
                           'canonical_mapper_present': canonical_present,
                           'free_bytes': number(vg.get('vg_free')), 'pv_count': 1,
                           'healthy_paths': healthy, 'total_paths': len(paths),
                           'transport': transport,
                           'empty_vg': len(owned_lvs) == 0,
                           'existing_lv_count': len(owned_lvs)})
    return {'result': 'OBSERVED', 'candidates': candidates}


if __name__ == '__main__':
    try:
        print(json.dumps(collect(), sort_keys=True))
    except InventoryError as exc:
        print(json.dumps({'result': 'UNKNOWN', 'reason': str(exc)}))
        sys.exit(1)
