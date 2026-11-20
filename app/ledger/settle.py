"""炉次结算：把投料、产出、电表读数与验收记录按炉次汇总成可出证的结算快照。

规则（与 docs/DATA-DICTIONARY.md 的约定一致）：
- 时间区间一律半开 [start, end)，统一折算为 UTC ISO 字符串后比较；
- 同一 (heat, meter, interval_start, interval_end) 的读数只计一次：
  完全相同的记录按重复计数，内容不同则按显式 revision 裁决，同修订号冲突即整炉隔离；
- 产出按 (heat, revision) 裁决，高修订号替代低修订号；被替代的记录保留在证据中；
- 返工/复配产生新修订号的产出后，旧验收失效，必须重新验收才能结算；
- 只有“已结算”的炉次才允许出证；
- 结算快照带 digest：同一份读数重复运行不会产生新的结算版本。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal

from app.config import (EMISSION_FACTOR, EMISSION_FACTOR_VERSION,
                        LEDGER_HEADERS, LEDGER_LIMIT, RULE_VERSION)
from app.domain import mass_kg
from app.errors import DomainError
from app.numeric import fmt, number

OUTPUT_STAGES = {'产出', '返工', '复配'}
ACCEPTANCE_OK = {'合格', '通过'}
ACCEPTANCE_VALUES = ACCEPTANCE_OK | {'不合格', '待补测'}


def _stamp(value):
    text = (value or '').strip()
    if not text:
        raise DomainError('MISSING', '时间为空')
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        raise DomainError('TIME', '时间格式无效')
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _revision(value):
    text = (value or '').strip()
    if not text:
        return 1
    revision = number(text)
    if revision != revision.to_integral_value() or revision < 1:
        raise DomainError('VERSION', '修订号须为正整数')
    return int(revision)


def _read(text):
    text = text.lstrip('﻿')
    if not text.strip():
        raise DomainError('EMPTY', '台账为空')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        head = next(reader, None)
    except csv.Error:
        raise DomainError('STRUCTURE', '表头损坏')
    if head is None:
        raise DomainError('BLANK', '台账仅含空行')
    head = [cell.strip() for cell in head]
    if set(head) != set(LEDGER_HEADERS) or len(head) != len(LEDGER_HEADERS):
        raise DomainError('HEADER', '台账字段缺失或重复')
    return head, reader


def _clean(raw, line):
    kind = raw['record_type'].strip().lower()
    if kind not in ('charge', 'output', 'meter', 'acceptance'):
        raise DomainError('TYPE', '未知记录类型')
    heat = raw['heat'].strip()
    if not heat:
        raise DomainError('MISSING', '炉次为空')
    record = {'行号': line, 'type': kind, 'heat': heat,
              'revision': _revision(raw['revision']),
              'source': raw['source'].strip(), 'stage': raw['stage'].strip()}
    if kind in ('charge', 'output'):
        record['value_kg'] = mass_kg(raw['value'], raw['unit'])
        record['recorded_at'] = _stamp(raw['recorded_at'])
        if kind == 'output':
            stage = record['stage'] or '产出'
            if stage not in OUTPUT_STAGES:
                raise DomainError('STAGE', '产出阶段无效')
            record['stage'] = stage
        else:
            record['stage'] = record['stage'] or '投料'
    elif kind == 'meter':
        meter = raw['meter'].strip()
        if not meter:
            raise DomainError('MISSING', '电表编号为空')
        if raw['unit'].strip().lower() != 'kwh':
            raise DomainError('UNIT', '用电单位须为 kWh')
        value = number(raw['value'])
        if value < 0:
            raise DomainError('RANGE', '用电量不能为负')
        start, end = _stamp(raw['interval_start']), _stamp(raw['interval_end'])
        if start >= end:
            raise DomainError('RANGE', '读数区间起点须早于终点')
        record.update({'meter': meter, 'interval_start': start,
                       'interval_end': end, 'value_kwh': value})
    else:
        decision = raw['value'].strip()
        if decision not in ACCEPTANCE_VALUES:
            raise DomainError('RANGE', '验收结论无效')
        record['recorded_at'] = _stamp(raw['recorded_at'])
        record['decision'] = decision
    return record


def parse(text):
    head, reader = _read(text)
    rows, quarantine, seen = [], [], set()
    counts = {'input': 0, 'blank': 0, 'header': 0, 'duplicate': 0}
    duplicate_keys = []
    try:
        for cells in reader:
            if len(rows) + len(quarantine) >= LEDGER_LIMIT:
                raise DomainError('LIMIT', '台账超过一万行')
            if not cells or all(not cell.strip() for cell in cells):
                counts['blank'] += 1
                continue
            if [cell.strip() for cell in cells] == head:
                counts['header'] += 1
                continue
            counts['input'] += 1
            if len(cells) != len(head):
                quarantine.append({'行号': reader.line_num, '原值': '|'.join(cells),
                                   '原因码': 'COLUMNS', '原因': '列数不符'})
                continue
            raw = dict(zip(head, [cell.strip() for cell in cells]))
            fingerprint = tuple(raw[key] for key in LEDGER_HEADERS)
            if fingerprint in seen:
                counts['duplicate'] += 1
                try:
                    repeated = _clean(raw, reader.line_num)
                except DomainError:
                    continue
                duplicate_keys.append({'heat': repeated['heat'],
                                       'key': _record_key(repeated),
                                       'revision': repeated['revision'],
                                       'signature': hashlib.sha256(
                                           json.dumps(_signature(repeated), ensure_ascii=False)
                                           .encode('utf-8')).hexdigest()})
                continue
            seen.add(fingerprint)
            try:
                rows.append(_clean(raw, reader.line_num))
            except DomainError as error:
                quarantine.append({**raw, '行号': reader.line_num,
                                   '原因码': error.code, '原因': error.message})
    except csv.Error:
        raise DomainError('STRUCTURE', '引号未闭合或记录截断')
    if not rows and not quarantine:
        raise DomainError('HEADER_ONLY' if not counts['blank'] else 'BLANK', '台账没有业务记录')
    return {'rows': rows, 'quarantine': quarantine, 'counts': counts,
            'duplicate_keys': duplicate_keys}


def _signature(record):
    return tuple(sorted((key, str(value)) for key, value in record.items()
                        if key not in ('revision', '行号', 'source')))


def _record_key(record):
    if record['type'] == 'charge':
        return 'charge|%s' % record['recorded_at']
    if record['type'] == 'output':
        return 'output|%s' % record['revision']
    if record['type'] == 'meter':
        return 'meter|%s|%s|%s' % (record['meter'], record['interval_start'],
                                   record['interval_end'])
    return 'acceptance|%s' % record['revision']


def _pick(records, key):
    """按 key 分组，组内取最高 revision；同修订号同内容计重复，不同内容计冲突。"""
    groups = {}
    for record in records:
        groups.setdefault(key(record), []).append(record)
    selected, superseded, conflicts, duplicates = {}, [], [], 0
    for group_key, group in groups.items():
        top = max(record['revision'] for record in group)
        candidates = [record for record in group if record['revision'] == top]
        if len({_signature(record) for record in candidates}) > 1:
            conflicts.append(group_key)
            continue
        selected[group_key] = candidates[0]
        duplicates += len(candidates) - 1
        superseded.extend(record for record in group if record['revision'] < top)
    return selected, superseded, conflicts, duplicates


def _overlaps(meters):
    seen, hits = {}, []
    for record in meters:
        seen.setdefault((record['heat'], record['meter']), []).append(record)
    for (heat, meter), group in seen.items():
        group.sort(key=lambda item: (item['interval_start'], item['interval_end']))
        reach = None
        for record in group:
            if reach and record['interval_start'] < reach:
                hits.append('%s 的 %s 在 %s 处与前一区间重叠' % (heat, meter, record['interval_start']))
            reach = max(reach or '', record['interval_end'])
    return hits


def _pick_latest(records):
    """产出/验收按整炉取最高修订号；更低修订号整条留痕。"""
    if not records:
        return {}, [], [], 0
    top = max(record['revision'] for record in records)
    candidates = [record for record in records if record['revision'] == top]
    if len({_signature(record) for record in candidates}) > 1:
        return {}, [], [(records[0]['heat'], top)], 0
    return ({records[0]['heat']: candidates[0]}, [r for r in records if r['revision'] < top],
            [], len(candidates) - 1)


def _reasons(conflicts, charges, outputs, meters, acceptances, overlaps, charge_kg, outputs_conflict):
    reasons, fields = [], set()
    if not charges:
        reasons.append({'code': 'NO_CHARGE', 'text': '缺少投料记录'})
        fields.add('投料')
    if conflicts['charge']:
        reasons.append({'code': 'CHARGE_CONFLICT', 'text': '投料记录同修订号冲突'})
        fields.add('投料')
    if not outputs:
        reasons.append({'code': 'NO_OUTPUT', 'text': '缺少产出记录'})
        fields.add('产出')
    if outputs_conflict:
        reasons.append({'code': 'OUTPUT_CONFLICT', 'text': '产出记录同修订号冲突，无法确定有效产出'})
        fields.add('产出')
    if not meters:
        reasons.append({'code': 'NO_METER', 'text': '缺少用电读数'})
        fields.update({'用电', '排放'})
    if conflicts['meter']:
        reasons.append({'code': 'METER_CONFLICT',
                        'text': '同一读数区间存在同修订号冲突（%d 个区间，未计入）' % len(conflicts['meter'])})
        fields.update({'用电', '排放'})
    if overlaps:
        reasons.append({'code': 'METER_OVERLAP', 'text': '电表读数区间重叠：%s' % '；'.join(overlaps)})
        fields.update({'用电', '排放'})
    if not acceptances:
        reasons.append({'code': 'NO_ACCEPTANCE', 'text': '缺少验收记录'})
        fields.add('验收')
    if conflicts['acceptance']:
        reasons.append({'code': 'ACCEPTANCE_CONFLICT', 'text': '验收记录同修订号冲突'})
        fields.add('验收')
    return reasons, fields


def _acceptance_gate(acceptance, production_at):
    if acceptance is None:
        return None
    if acceptance['decision'] == '待补测':
        return {'code': 'ACCEPTANCE_PENDING', 'text': '验收未完成（待补测）'}
    if acceptance['decision'] == '不合格':
        return {'code': 'ACCEPTANCE_FAILED', 'text': '验收不合格'}
    if production_at and acceptance['recorded_at'] < production_at:
        return {'code': 'ACCEPTANCE_STALE', 'text': '验收早于最近产出/返工，需重新验收'}
    return None


def _digest(heat):
    canonical = {
        'heat': heat['heat'], 'rule_version': RULE_VERSION,
        'factor': EMISSION_FACTOR, 'factor_version': EMISSION_FACTOR_VERSION,
        'charge_kg': heat['charge_kg'], 'output': heat['output'],
        'electricity_kwh': heat['electricity_kwh'], 'emission_kg': heat['emission_kg'],
        'intervals': [{'key': item['key'], 'value_kwh': item['value_kwh'],
                       'revision': item['revision']} for item in heat['intervals']],
        'superseded_intervals': [item['key'] + '#' + str(item['revision'])
                                 for item in heat['superseded_intervals']],
        'superseded_outputs': [item['revision'] for item in heat['superseded_outputs']],
        'superseded_acceptances': [item['revision'] for item in heat['superseded_acceptances']],
        'acceptance': heat['acceptance'], 'reasons': heat['reasons'],
    }
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False,
                                     sort_keys=True).encode('utf-8')).hexdigest()


def settle(parsed):
    heats = {}
    for record in parsed['rows']:
        heats.setdefault(record['heat'], []).append(record)
    repeated = {}
    for item in parsed.get('duplicate_keys', []):
        repeated.setdefault(item['heat'], set()).add(
            (item['key'], item['revision'], item['signature']))
    stored = {}
    for conflict in parsed.get('conflicts', []):
        stored.setdefault(conflict['heat'], []).append(conflict)
        heats.setdefault(conflict['heat'], [])

    results, duplicate_records = [], 0
    for heat in sorted(heats):
        records = heats[heat]
        by_type = {kind: [r for r in records if r['type'] == kind]
                   for kind in ('charge', 'output', 'meter', 'acceptance')}
        ingest_conflicts = sorted(stored.get(heat, []), key=lambda c: (c['record_key'], c['revision']))

        charges_sel, charges_super, charge_conf, dup = _pick(
            by_type['charge'], lambda r: (r['heat'], r['recorded_at']))
        outputs_sel, outputs_super, output_conf, dup_out = _pick_latest(by_type['output'])
        meters_sel, meters_super, meter_conf, dup_meter = _pick(
            by_type['meter'],
            lambda r: (r['heat'], r['meter'], r['interval_start'], r['interval_end']))
        acceptance_sel, acceptance_super, acceptance_conf, dup_acc = _pick_latest(
            by_type['acceptance'])
        duplicate_records += dup + dup_out + dup_meter + dup_acc

        conflicts = {'charge': list(charge_conf), 'output': list(output_conf),
                     'meter': list(meter_conf), 'acceptance': list(acceptance_conf)}
        for conflict in ingest_conflicts:
            kind = conflict['record_key'].split('|', 1)[0]
            top = max((r['revision'] for r in records
                       if _record_key(r) == conflict['record_key']), default=0)
            if conflict['revision'] < top:
                continue  # 已被更高修订号取代，留痕但不阻塞
            if kind in conflicts:
                conflicts[kind].append(conflict['record_key'])
        charges = sorted(charges_sel.values(), key=lambda r: r['recorded_at'])
        charge_conflict = bool(conflicts['charge'])
        charge_kg = None if charge_conflict else sum(
            (r['value_kg'] for r in charges), Decimal(0))

        outputs = sorted(outputs_sel.values(), key=lambda r: r['revision'])
        output = outputs[-1] if outputs and not conflicts['output'] else None

        meters = sorted(meters_sel.values(),
                        key=lambda r: (r['meter'], r['interval_start'], r['interval_end']))
        overlaps = _overlaps(meters)
        electricity = (sum((r['value_kwh'] for r in meters), Decimal(0))
                       if meters and not conflicts['meter'] and not overlaps else None)
        emission = (fmt(electricity * Decimal(EMISSION_FACTOR))
                    if electricity is not None and not conflicts['output'] else None)

        acceptances = sorted(acceptance_sel.values(), key=lambda r: r['revision'])
        acceptance = acceptances[-1] if acceptances and not conflicts['acceptance'] else None
        production_at = max((r['recorded_at'] for r in outputs), default=None)
        gate = _acceptance_gate(acceptance, production_at)

        reasons, fields = _reasons(conflicts, list(charges_sel.values()),
                                   list(outputs_sel.values()), meters,
                                   list(acceptance_sel.values()), overlaps,
                                   charge_kg, bool(conflicts['output']))
        if gate:
            reasons.append(gate)
            fields.add('验收')

        output_row = None
        if output:
            output_row = {'value_kg': fmt(output['value_kg']), 'stage': output['stage'],
                          'revision': output['revision'],
                          'recorded_at': output['recorded_at'], 'source': output['source']}
        ordered_outputs = sorted(outputs_sel.values(), key=lambda r: r['revision'])
        replaced_outputs = [r for r in ordered_outputs if output is not None and r is not output]
        replaced_outputs.extend(outputs_super)
        acceptance_row = None
        if acceptance:
            acceptance_row = {'decision': acceptance['decision'],
                              'revision': acceptance['revision'],
                              'recorded_at': acceptance['recorded_at'],
                              'source': acceptance['source']}

        result = {
            'heat': heat, 'settled': not reasons,
            'status': '已结算' if not reasons else '未结算',
            'reasons': reasons, 'unsettled_fields': sorted(fields),
            'charge_kg': fmt(charge_kg) if charge_kg is not None else None,
            'output': output_row,
            'output_kg': output_row['value_kg'] if output_row else None,
            'yield_pct': fmt(output['value_kg'] / charge_kg * 100)
            if output and charge_kg and charge_kg > 0 else None,
            'electricity_kwh': fmt(electricity) if electricity is not None else None,
            'emission_kg': emission,
            'factor': EMISSION_FACTOR, 'factor_version': EMISSION_FACTOR_VERSION,
            'intervals': [{'key': '%s|%s|%s' % (r['meter'], r['interval_start'], r['interval_end']),
                           'meter': r['meter'], 'interval_start': r['interval_start'],
                           'interval_end': r['interval_end'], 'value_kwh': fmt(r['value_kwh']),
                           'revision': r['revision'], 'source': r['source']} for r in meters],
            'superseded_intervals': [{'key': '%s|%s|%s' % (r['meter'], r['interval_start'], r['interval_end']),
                                      'meter': r['meter'], 'interval_start': r['interval_start'],
                                      'interval_end': r['interval_end'], 'value_kwh': fmt(r['value_kwh']),
                                      'revision': r['revision'], 'source': r['source']}
                                     for r in meters_super],
            'superseded_outputs': [{'value_kg': fmt(r['value_kg']), 'stage': r['stage'],
                                    'revision': r['revision'], 'recorded_at': r['recorded_at'],
                                    'source': r['source']}
                                   for r in sorted(replaced_outputs, key=lambda r: r['revision'])],
            'superseded_acceptances': [{'decision': r['decision'], 'revision': r['revision'],
                                        'recorded_at': r['recorded_at'], 'source': r['source']}
                                       for r in sorted(acceptance_super,
                                                       key=lambda r: r['revision'])],
            'charges': [{'value_kg': fmt(r['value_kg']), 'recorded_at': r['recorded_at'],
                         'revision': r['revision'], 'source': r['source']} for r in charges],
            'acceptance': acceptance_row,
            'record_counts': {kind: len(by_type[kind]) for kind in by_type},
            'duplicate_records': dup + dup_out + dup_meter + dup_acc + len(repeated.get(heat, ())),
        }
        result['digest'] = _digest(result)
        results.append(result)

    return {'heats': results, 'quarantine': parsed['quarantine'],
            'counts': parsed['counts'], 'duplicate_records': duplicate_records}


def explain(previous, current):
    """对比上一版结算快照，生成业务可读的变化说明。"""
    if previous is None:
        return ['首次结算']
    changes = []
    if previous.get('output') != current.get('output'):
        before, after = previous.get('output') or {}, current.get('output') or {}
        changes.append('产出重新结算：%s rev%s %s kg → %s rev%s %s kg' % (
            before.get('stage', '无'), before.get('revision', '—'), before.get('value_kg', '—'),
            after.get('stage', '无'), after.get('revision', '—'), after.get('value_kg', '—')))
    if previous.get('charge_kg') != current.get('charge_kg'):
        changes.append('投料变更：%s kg → %s kg' % (
            previous.get('charge_kg') or '—', current.get('charge_kg') or '—'))
    before_keys = {item['key'] for item in previous.get('intervals', [])}
    after_keys = {item['key'] for item in current.get('intervals', [])}
    added, removed = after_keys - before_keys, before_keys - after_keys
    if added or removed:
        changes.append('用电读数区间变化：补入 %d 个、移除 %d 个（晚到或修订资料）' % (len(added), len(removed)))
    if (previous.get('electricity_kwh') != current.get('electricity_kwh')
            or previous.get('emission_kg') != current.get('emission_kg')):
        changes.append('用电 %s → %s kWh，排放 %s → %s kgCO2e（因子 %s）' % (
            previous.get('electricity_kwh') or '—', current.get('electricity_kwh') or '—',
            previous.get('emission_kg') or '—', current.get('emission_kg') or '—',
            current.get('factor')))
    if previous.get('acceptance') != current.get('acceptance'):
        before, after = previous.get('acceptance') or {}, current.get('acceptance') or {}
        changes.append('验收更新：%s rev%s → %s rev%s' % (
            before.get('decision', '无'), before.get('revision', '—'),
            after.get('decision', '无'), after.get('revision', '—')))
    if previous.get('superseded_intervals') != current.get('superseded_intervals'):
        changes.append('读数修订留痕：%d 条旧修订被更高修订号替代并保留' %
                       len(current.get('superseded_intervals', [])))
    before_codes = [item['code'] for item in previous.get('reasons', [])]
    after_codes = [item['code'] for item in current.get('reasons', [])]
    if before_codes != after_codes:
        changes.append('结算状态：%s → %s%s' % (
            previous.get('status', '—'), current.get('status', '—'),
            '' if not after_codes else '（%s）' % '、'.join(item['text'] for item in current['reasons'])))
    return changes or ['结算复核完成，数值未变化']
