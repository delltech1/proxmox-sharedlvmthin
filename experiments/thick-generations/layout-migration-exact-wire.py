"""UNSHIPPED bounded raw-evidence framing. Parsing grants no authority."""
import base64
import hashlib
import json
import re

MAX_RAW = 8 * 1024 * 1024
CHUNK = 32768
MAX_CHUNKS = MAX_RAW // CHUNK
MAX_FRAME = 12 * 1024 * 1024
EVIDENCE_FIELDS = {'schema', 'authority', 'plan', 'baseline', 'baseline_sources',
    'baseline_captures', 'workload', 'current', 'certified_inputs', 'archives',
    'cohort_id', 'issued_at', 'expires_at'}


class Refusal(ValueError): pass


def need(value, message):
    if not value: raise Refusal(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def sha(raw): return hashlib.sha256(raw).hexdigest()
def digest(value): return sha(canonical(value))


def exact(value, fields):
    need(type(value) is dict and set(value) == set(fields), 'wire field coverage')


def strict(raw):
    need(type(raw) is bytes and 0 < len(raw) <= MAX_RAW, 'raw evidence ceiling')
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result, 'duplicate raw key'); result[key] = value
        return result
    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(Refusal('nonfinite raw JSON')))
        need(canonical(value) == raw, 'noncanonical raw JSON')
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Refusal('invalid raw evidence') from exc


def binding(envelope, pins):
    names = [r['node'] for r in pins['participants']]
    need(len(names) == 4 and len(set(names)) == 4, 'four-node pins required')
    hashes = {r['source_package']['sources_sha256'] for r in pins['participants']}
    need(len(hashes) == 1, 'mixed source closure')
    return {'challenge': envelope['challenge'], 'pins_sha256': digest(pins),
            'sources_sha256': next(iter(hashes)), 'plan_sha256': envelope['plan_sha256'],
            'tx': envelope['tx'], 'node': envelope['node'], 'boot_id': envelope['boot_id']}


def validate_evidence(value, envelope, pins):
    exact(value, EVIDENCE_FIELDS)
    need(value['schema'] == 'slt-exact-local-release-evidence/v1' and value['authority'] == 'NONE',
         'raw release evidence required, not verdict labels')
    need(type(value['cohort_id']) is str and re.fullmatch('[0-9a-f]{32}', value['cohort_id']), 'cohort identity')
    need(digest(value['plan']) == envelope['plan_sha256'] and value['plan']['tx'] == envelope['tx'], 'raw plan differs')
    names = [r['node'] for r in pins['participants']]
    need([r['node'] for r in value['plan']['participants']] == names, 'raw participant order')
    for field in ('baseline_sources', 'baseline_captures', 'current'):
        exact(value[field], names)
    exact(value['baseline']['records'], names)
    exact(value['archives'], names[:3])
    for row in pins['participants']:
        node = row['node']; current = value['current'][node]
        need(current['cohort_id'] == value['cohort_id']
             and current['observation']['node'] == node
             and current['observation']['boot_id'] == row['boot_id']
             and current['observation']['package_sha256'] == row['source_package']['artifact_sha256'],
             'raw cohort/node/boot/package differs')


def pack(effect, raw, envelope, pins):
    value = strict(raw); validate_evidence(value, envelope, pins)
    chunks = []
    for offset in range(0, len(raw), CHUNK):
        part = raw[offset:offset + CHUNK]
        chunks.append({'index': len(chunks), 'sha256': sha(part), 'base64': base64.b64encode(part).decode('ascii')})
    return {'effect': effect, 'release_evidence': {
        'schema': 'slt-exact-release-wire/v1', 'authority': 'NONE', **binding(envelope, pins),
        'cohort_id': value['cohort_id'], 'effect_sha256': digest(effect),
        'raw_sha256': sha(raw), 'byte_count': len(raw), 'chunks': chunks}}


def unpack(payload, envelope, pins):
    exact(payload, {'effect', 'release_evidence'})
    wire = payload['release_evidence']; expected = binding(envelope, pins)
    exact(wire, {'schema', 'authority', *expected, 'cohort_id', 'effect_sha256', 'raw_sha256', 'byte_count', 'chunks'})
    need(wire['schema'] == 'slt-exact-release-wire/v1' and wire['authority'] == 'NONE'
         and all(wire[k] == v for k, v in expected.items())
         and wire['effect_sha256'] == digest(payload['effect']), 'wire identity/challenge/source differs')
    count = wire['byte_count']; chunks = wire['chunks']
    need(type(count) is int and 0 < count <= MAX_RAW and type(chunks) is list
         and len(chunks) == (count + CHUNK - 1) // CHUNK <= MAX_CHUNKS, 'chunk/total ceiling')
    parts = []
    for index, chunk in enumerate(chunks):
        exact(chunk, {'index', 'sha256', 'base64'})
        need(type(chunk['index']) is int and chunk['index'] == index
             and type(chunk['base64']) is str and 0 < len(chunk['base64']) <= 4 * ((CHUNK + 2) // 3),
             'chunk index/encoding ceiling')
        try: part = base64.b64decode(chunk['base64'], validate=True)
        except (ValueError, TypeError) as exc: raise Refusal('chunk encoding') from exc
        need(len(part) == min(CHUNK, count - index * CHUNK)
             and base64.b64encode(part).decode('ascii') == chunk['base64'] and sha(part) == chunk['sha256'],
             'chunk length/hash differs')
        parts.append(part)
    raw = b''.join(parts)
    need(len(raw) == count and sha(raw) == wire['raw_sha256'], 'raw evidence digest differs')
    value = strict(raw); validate_evidence(value, envelope, pins)
    need(value['cohort_id'] == wire['cohort_id'], 'wire cohort differs')
    return payload['effect'], raw
