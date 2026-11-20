"""炉次结算与证明的持久层。

所有表都是追加式：
- ledger_records 以 (heat, record_key, revision, signature) 为主键，同一条读数重复导入只计一次；
- settlements 以 (heat, version) 为主键，重跑同 digest 不产生新版本，数值变化才追加新版本；
- certificates 以 id 为主键，修订只新增版本，旧版永久保留并标记替代关系；
- 出证失败只写 certificate_log，不触碰任何既有证明行。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from app.config import LEDGER_SAMPLE, RULE_VERSION
from app.errors import DomainError
from app.ledger.settle import explain, parse, settle


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def connection():
    path = Path(os.environ.get('APP_DB', 'data/runs.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS ledger_records(
            heat TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL,
            signature TEXT NOT NULL, payload TEXT NOT NULL, first_seen TEXT NOT NULL,
            PRIMARY KEY(heat, record_key, revision, signature));
        CREATE TABLE IF NOT EXISTS ledger_duplicates(
            heat TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL,
            signature TEXT NOT NULL, PRIMARY KEY(heat, record_key, revision, signature));
        CREATE TABLE IF NOT EXISTS ledger_conflicts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, heat TEXT NOT NULL,
            record_key TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            UNIQUE(heat, record_key, revision, payload));
        CREATE TABLE IF NOT EXISTS ledger_ingests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL,
            rule_version TEXT NOT NULL, created_at TEXT NOT NULL, counts TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settlements(
            heat TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL,
            payload TEXT NOT NULL, changes TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(heat, version));
        CREATE TABLE IF NOT EXISTS certificates(
            id TEXT PRIMARY KEY, heat TEXT NOT NULL, cert_version INTEGER NOT NULL,
            settlement_version INTEGER NOT NULL, digest TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL, generated_at TEXT NOT NULL,
            superseded_by TEXT, superseded_at TEXT);
        CREATE TABLE IF NOT EXISTS certificate_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, heat TEXT NOT NULL,
            ok INTEGER NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
    ''')
    return db


def _record_key(record):
    kind = record['type']
    if kind == 'charge':
        return 'charge|%s' % record['recorded_at']
    if kind == 'output':
        return 'output|%s' % record['revision']
    if kind == 'meter':
        return 'meter|%s|%s|%s' % (record['meter'], record['interval_start'], record['interval_end'])
    return 'acceptance|%s' % record['revision']


def _signature(record):
    material = {key: (str(value) if isinstance(value, Decimal) else value)
                for key, value in record.items() if key != '行号'}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _encode(record):
    return json.dumps({key: (str(value) if isinstance(value, Decimal) else value)
                       for key, value in record.items()}, ensure_ascii=False, sort_keys=True)


def _remember_duplicate(db, heat, key, revision, signature):
    db.execute('INSERT OR IGNORE INTO ledger_duplicates(heat,record_key,revision,signature)'
               ' VALUES (?,?,?,?)', (heat, key, revision, signature))


def _decode(payload):
    record = json.loads(payload)
    if 'value_kg' in record:
        record['value_kg'] = Decimal(record['value_kg'])
    if 'value_kwh' in record:
        record['value_kwh'] = Decimal(record['value_kwh'])
    return record


def ingest(text=None):
    """导入一批台账记录；同一条读数重复导入不会产生新记录。"""
    if text is None:
        text = LEDGER_SAMPLE.read_text(encoding='utf-8-sig')
    parsed = parse(text)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    inserted = duplicates = 0
    notices = []
    with connection() as db:
        known = db.execute('SELECT counts FROM ledger_ingests WHERE sha256=? AND rule_version=?',
                           (digest, RULE_VERSION)).fetchone()
        if known is not None:
            # 同一份台账在相同规则版本下已导入：整批视为重复，不重复计入任何读数。
            previous = json.loads(known['counts'])
            return {'ingest': {'sha256': digest, 'inserted': 0,
                               'duplicates': previous.get('inserted', 0)
                               + previous.get('duplicates', 0),
                               'quarantined': previous.get('quarantined', 0),
                               'conflicts': previous.get('conflicts', 0),
                               'quarantine': previous.get('quarantine', []),
                               'counts': previous.get('counts', {}),
                               'reused': True},
                    'settlement': run_settlement()}
        for raw in parsed['rows']:
            record = {key: (str(value) if isinstance(value, Decimal) else value)
                      for key, value in raw.items()}
            signature = _signature(record)
            key = _record_key(record)
            prior = db.execute('SELECT revision,signature FROM ledger_records'
                               ' WHERE heat=? AND record_key=? AND revision=? AND signature=?',
                               (record['heat'], key, record['revision'], signature)).fetchone()
            if prior is not None:
                duplicates += 1
                _remember_duplicate(db, record['heat'], key, record['revision'], signature)
                continue
            same_revision = db.execute('SELECT signature FROM ledger_records'
                                       ' WHERE heat=? AND record_key=? AND revision=?',
                                       (record['heat'], key, record['revision'])).fetchall()
            if same_revision:
                db.execute('INSERT OR IGNORE INTO ledger_conflicts(heat,record_key,revision,payload,'
                           'first_seen) VALUES (?,?,?,?,?)',
                           (record['heat'], key, record['revision'],
                            _encode(raw), _now()))
                notices.append({**record, '原因码': 'CONFLICT',
                                '原因': '同一读数区间同修订号内容不一致，未采信'})
                continue
            db.execute('INSERT INTO ledger_records(heat,record_key,revision,signature,payload,'
                       'first_seen) VALUES (?,?,?,?,?,?)',
                       (record['heat'], key, record['revision'], signature,
                        _encode(raw), _now()))
            inserted += 1
        for item in parsed.get('duplicate_keys', []):
            _remember_duplicate(db, item['heat'], item['key'], item['revision'],
                                item['signature'])
        db.execute('INSERT INTO ledger_ingests(sha256,rule_version,created_at,counts) VALUES (?,?,?,?)',
                   (digest, RULE_VERSION, _now(),
                    json.dumps({'inserted': inserted, 'duplicates': duplicates,
                                'conflicts': len(notices),
                                'quarantined': len(parsed['quarantine']),
                                'quarantine': parsed['quarantine'] + notices,
                                'counts': parsed['counts']}, ensure_ascii=False, default=str)))
    settled = run_settlement()
    return {'ingest': {'sha256': digest, 'inserted': inserted, 'duplicates': duplicates,
                       'quarantined': len(parsed['quarantine']) + len(notices),
                       'conflicts': len(notices),
                       'quarantine': parsed['quarantine'] + notices,
                       'counts': parsed['counts']},
            'settlement': settled}


def _load(db):
    rows = db.execute('SELECT payload FROM ledger_records').fetchall()
    records = [_decode(row['payload']) for row in rows]
    conflicts = [{'heat': row['heat'], 'record_key': row['record_key'],
                  'revision': row['revision'], 'payload': _decode(row['payload'])}
                 for row in db.execute('SELECT heat,record_key,revision,payload FROM ledger_conflicts')]
    duplicate_keys = [{'heat': row['heat'], 'key': row['record_key'],
                       'revision': row['revision'], 'signature': row['signature']}
                      for row in db.execute('SELECT heat,record_key,revision,signature'
                                            ' FROM ledger_duplicates')]
    return records, conflicts, duplicate_keys


def run_settlement():
    with connection() as db:
        records, conflicts, duplicate_keys = _load(db)
        if not records and not conflicts:
            return {'heats': [], 'counts': {}, 'duplicate_records': 0}
        snapshot = settle({'rows': records, 'conflicts': conflicts,
                           'duplicate_keys': duplicate_keys,
                           'quarantine': [], 'counts': {}})
        summary = []
        for heat in snapshot['heats']:
            latest = db.execute(
                'SELECT version,digest,payload,changes,created_at FROM settlements'
                ' WHERE heat=? ORDER BY version DESC LIMIT 1', (heat['heat'],)).fetchone()
            if latest and latest['digest'] == heat['digest']:
                summary.append({**json.loads(latest['payload']),
                                'version': latest['version'],
                                'changes': json.loads(latest['changes']),
                                'settled_at': latest['created_at'],
                                'created': False})
                continue
            previous = json.loads(latest['payload']) if latest else None
            changes = explain(previous, heat)
            version = (latest['version'] + 1) if latest else 1
            created_at = _now()
            db.execute('INSERT INTO settlements(heat,version,digest,payload,changes,created_at)'
                       ' VALUES (?,?,?,?,?,?)',
                       (heat['heat'], version, heat['digest'],
                        json.dumps(heat, ensure_ascii=False, sort_keys=True),
                        json.dumps(changes, ensure_ascii=False), created_at))
            # 旧版证明不删除：结算变化时标记“待换证”，炉次转为未结算时标记“已失效”；
            # 只有新版证明成功生成后，才会在 issue() 中标记“已替代”。
            old = db.execute('SELECT id,digest,status FROM certificates WHERE heat=?'
                             ' AND status IN (?,?)',
                             (heat['heat'], '有效', '待换证')).fetchall()
            for row in old:
                if not heat['settled']:
                    db.execute('UPDATE certificates SET status=?, superseded_at=? WHERE id=?',
                               ('已失效', created_at, row['id']))
                elif row['digest'] != heat['digest']:
                    db.execute('UPDATE certificates SET status=?, superseded_at=? WHERE id=?',
                               ('待换证', created_at, row['id']))
            summary.append({**heat, 'version': version, 'changes': changes,
                            'settled_at': created_at, 'created': True})
    return {'heats': summary, 'duplicate_records': snapshot['duplicate_records']}


def heats():
    result = run_settlement()
    with connection() as db:
        certs = db.execute('SELECT id,heat,cert_version,status,generated_at,digest'
                           ' FROM certificates ORDER BY heat,cert_version DESC').fetchall()
    by_heat = {}
    for row in certs:
        by_heat.setdefault(row['heat'], []).append(dict(row))
    for heat in result['heats']:
        heat['certificates'] = by_heat.get(heat['heat'], [])
    return result


def heat(ident):
    result = run_settlement()
    match = next((item for item in result['heats'] if item['heat'] == ident), None)
    if match is None:
        raise DomainError('NOT_FOUND', '炉次不存在', 404)
    with connection() as db:
        versions = db.execute('SELECT version,digest,payload,changes,created_at FROM settlements'
                              ' WHERE heat=? ORDER BY version', (ident,)).fetchall()
        match['versions'] = [{'version': row['version'], 'digest': row['digest'],
                              'settled_at': row['created_at'],
                              'changes': json.loads(row['changes']),
                              'payload': json.loads(row['payload'])} for row in versions]
        match['certificates'] = [dict(row) for row in db.execute(
            'SELECT id,heat,cert_version,settlement_version,status,generated_at,digest,superseded_by'
            ' FROM certificates WHERE heat=? ORDER BY cert_version DESC', (ident,))]
    return match


def _seal(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((RULE_VERSION + ':' + canonical).encode('utf-8')).hexdigest()


def _log(ident, ok, message):
    try:
        with connection() as db:
            db.execute('INSERT INTO certificate_log(heat,ok,message,created_at) VALUES (?,?,?,?)',
                       (ident, 1 if ok else 0, message[:200], _now()))
    except Exception:
        pass


def issue(ident):
    """为已结算炉次出具证明。失败时不写入任何证明行，旧版保持可用。"""
    result = run_settlement()
    match = next((item for item in result['heats'] if item['heat'] == ident), None)
    if match is None:
        raise DomainError('NOT_FOUND', '炉次不存在', 404)
    if not match['settled']:
        raise DomainError('UNSETTLED', '炉次未结算，不能出证：%s' %
                          '、'.join(item['text'] for item in match['reasons']), 409)
    acceptance = match.get('acceptance') or {}
    if acceptance.get('decision') not in ('合格', '通过'):
        raise DomainError('ACCEPTANCE', '验收未完成，不能出证', 409)

    db = connection()
    try:
        current = db.execute('SELECT id,payload FROM certificates WHERE heat=? AND digest=?'
                             ' AND status=? ORDER BY cert_version DESC LIMIT 1',
                             (ident, match['digest'], '有效')).fetchone()
        if current is not None:
            return {'certificate': json.loads(current['payload']), 'issued': False}

        matching = db.execute('SELECT id,payload FROM certificates WHERE heat=? AND digest=?'
                              ' ORDER BY cert_version DESC LIMIT 1',
                              (ident, match['digest'])).fetchone()
        if matching is not None:
            # 结算回退到历史版本：原证明仍在，重新启用即可，不生成新版本。
            db.execute('UPDATE certificates SET status=?, superseded_by=NULL, superseded_at=NULL'
                       ' WHERE id=?', ('有效', matching['id']))
            db.commit()
            db.close()
            return {'certificate': json.loads(matching['payload']), 'issued': False}

        latest = db.execute('SELECT version FROM settlements WHERE heat=? AND digest=?',
                            (ident, match['digest'])).fetchone()
        cert_version = (db.execute('SELECT MAX(cert_version) FROM certificates WHERE heat=?',
                                   (ident,)).fetchone()[0] or 0) + 1
        payload = {
            'certificate_id': 'CERT-%s-V%d' % (ident, cert_version),
            'heat': ident, 'cert_version': cert_version,
            'settlement_version': latest['version'] if latest else None,
            'digest': match['digest'], 'rule_version': RULE_VERSION,
            'generated_at': _now(),
            'charge_kg': match['charge_kg'], 'output': match['output'],
            'yield_pct': match['yield_pct'],
            'electricity_kwh': match['electricity_kwh'], 'emission_kg': match['emission_kg'],
            'factor': match['factor'], 'factor_version': match['factor_version'],
            'intervals': match['intervals'], 'acceptance': match['acceptance'],
            'charges': match['charges'],
        }
        payload['seal'] = _seal(payload)
        previous = db.execute('SELECT id FROM certificates WHERE heat=? AND status IN (?,?)',
                              (ident, '有效', '待换证')).fetchall()
        db.execute('INSERT INTO certificates(id,heat,cert_version,settlement_version,digest,'
                   'payload,status,generated_at) VALUES (?,?,?,?,?,?,?,?)',
                   (payload['certificate_id'], ident, cert_version, payload['settlement_version'],
                    match['digest'], json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    '有效', payload['generated_at']))
        for row in previous:
            db.execute('UPDATE certificates SET status=?, superseded_by=?, superseded_at=? WHERE id=?',
                       ('已替代', payload['certificate_id'], _now(), row['id']))
        db.commit()
    except Exception as error:
        db.rollback()
        db.close()
        _log(ident, False, str(error))
        raise DomainError('CERT_FAILED', '证明生成失败，旧版证明与证据保留', 500)
    db.close()
    _log(ident, True, payload['certificate_id'])
    return {'certificate': payload, 'issued': True}


def certificate(ident):
    with connection() as db:
        row = db.execute('SELECT * FROM certificates WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise DomainError('NOT_FOUND', '证明不存在', 404)
    payload = json.loads(row['payload'])
    payload.update({'status': row['status'], 'superseded_by': row['superseded_by'],
                    'superseded_at': row['superseded_at'],
                    'settlement_version': row['settlement_version']})
    return payload


def certificates_for(ident=None):
    with connection() as db:
        sql = ('SELECT id,heat,cert_version,settlement_version,digest,status,generated_at,'
               'superseded_by,superseded_at FROM certificates')
        args = ()
        if ident:
            sql += ' WHERE heat=?'
            args = (ident,)
        rows = db.execute(sql + ' ORDER BY heat,cert_version', args).fetchall()
    return [dict(row) for row in rows]
