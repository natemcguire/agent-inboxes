"""Strict v1 envelopes. JCS serialization for the protocol's string/integer subset."""
import json
import re
from datetime import datetime, timezone

MAX_SEQ = 9007199254740991
MAX_REQUEST = 1048576
FIELDS = {'envelope_version', 'message_id', 'thread', 'sender', 'sender_session',
          'recipients', 'subject', 'body_markdown', 'reply_to_email_id', 'references',
          'sent_at', 'origin_device'}
THREAD_FIELDS = {'thread_id', 'root_message_id', 'home_project', 'subject', 'created_at'}
SLUG = r'[a-z0-9][a-z0-9._-]{0,127}'


def require(condition, reason='invalid_message'):
    if not condition:
        raise ValueError(reason)


def keys(value, expected):
    require(type(value) is dict and set(value) == expected)


def string(value, limit, blank=False):
    require(type(value) is str and '\0' not in value)
    require(0 < len(value.encode('utf-8')) <= limit)
    require(not blank or bool(value.strip()))


def identifier(value, prefix):
    string(value, 128)
    require(re.fullmatch(prefix + r'_[A-Za-z0-9_-]+', value) is not None)


def timestamp(value):
    require(type(value) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', value, re.ASCII))
    datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')


def normalize_time(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(dt.tzinfo is not None)
    dt = dt.astimezone(timezone.utc)
    return f'{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond // 1000:03d}Z'


def canonical(value):
    # All envelope keys are fixed ASCII; the only number is integer version 1.
    # Thus UTF-16 key ordering and ECMAScript number formatting reduce exactly
    # to this serialization. Validation excludes surrogates and NaN/floats.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def strict_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False))


def validate_envelope(e):
    keys(e, FIELDS)
    require(type(e['envelope_version']) is int and e['envelope_version'] == 1)
    identifier(e['message_id'], 'eml')
    t = e['thread']
    keys(t, THREAD_FIELDS)
    identifier(t['thread_id'], 'thr')
    identifier(t['root_message_id'], 'eml')
    require(type(t['home_project']) is str and re.fullmatch(SLUG, t['home_project']))
    for subject in (t['subject'], e['subject']):
        string(subject, 1024, blank=True)
    string(e['body_markdown'], 65536)
    for value in (e['origin_device'], e['sender_session']):
        if value is not None:
            string(value, 128)
    timestamp(t['created_at'])
    timestamp(e['sent_at'])
    r = e['recipients']
    keys(r, {'to', 'cc'})
    require(type(r['to']) is list and type(r['cc']) is list and len(r['to']) > 0)
    addresses = r['to'] + r['cc']
    require(len(addresses) <= 100)
    for address in [e['sender']] + addresses:
        require(type(address) is str and re.fullmatch(SLUG + '@' + SLUG, address))
    require(len(addresses) == len(set(addresses)))
    refs = e['references']
    require(type(refs) is list and len(refs) <= 256)
    for ref in refs:
        identifier(ref, 'eml')
    require(len(refs) == len(set(refs)) and e['message_id'] not in refs)
    parent = e['reply_to_email_id']
    if parent is None:
        require(refs == [] and t == dict(thread_id=t['thread_id'], root_message_id=e['message_id'],
                home_project=e['sender'].split('@')[1], subject=e['subject'], created_at=e['sent_at']))
    else:
        identifier(parent, 'eml')
        require(bool(refs) and refs[-1] == parent and refs[0] == t['root_message_id'])
    payload = canonical(e)
    require(len(payload.encode('utf-8')) <= 131072)
    return payload


def validate_ancestry(e, lookup):
    if e['reply_to_email_id'] is None:
        return
    parent = lookup(e['reply_to_email_id'])
    require(parent is not None, 'blocked_by_ancestor')
    require(parent['thread'] == e['thread'], 'thread_conflict')
    require(e['references'] == parent['references'] + [parent['message_id']], 'invalid ancestry')
    for ref in e['references']:
        ancestor = lookup(ref)
        require(ancestor is not None, 'blocked_by_ancestor')
        require(ancestor['thread'] == e['thread'], 'thread_conflict')
