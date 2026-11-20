from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, jsonify, request

from app.errors import DomainError
from app.ledger import store
from app.reader import decode

bp = Blueprint('ledger', __name__)


@bp.route('/api/ledger/ingest', methods=['POST'])
def ingest():
    if request.files:
        text = decode(request.files['file'].read())
    else:
        text = (request.get_json(silent=True) or {}).get('csv')
    return jsonify(store.ingest(text))


@bp.route('/api/ledger/settle', methods=['POST'])
def settle():
    return jsonify(store.run_settlement())


@bp.route('/api/ledger/heats')
def heat_list():
    return jsonify(store.heats())


@bp.route('/api/ledger/heats/<ident>')
def heat_detail(ident):
    return jsonify(store.heat(ident))


@bp.route('/api/ledger/certificates')
def certificate_list():
    return jsonify(store.certificates_for(request.args.get('heat')))


@bp.route('/api/ledger/certificate', methods=['POST'])
def certificate_issue():
    body = request.get_json(silent=True) or {}
    heat = (body.get('heat') or '').strip()
    if not heat:
        raise DomainError('MISSING', '缺少炉次')
    return jsonify(store.issue(heat))


@bp.route('/api/ledger/certificates/<ident>')
def certificate_detail(ident):
    return jsonify(store.certificate(ident))


@bp.route('/api/ledger/certificate.csv')
def certificate_csv():
    ident = (request.args.get('cert_id') or '').strip()
    heat = (request.args.get('heat') or '').strip()
    if ident:
        certificate = store.certificate(ident)
    elif heat:
        listed = [c for c in store.certificates_for(heat) if c['status'] == '有效']
        if not listed:
            raise DomainError('NOT_FOUND', '该炉次没有有效证明', 404)
        certificate = store.certificate(listed[0]['id'])
    else:
        raise DomainError('MISSING', '需要 cert_id 或 heat')
    rows = [
        ('证明编号', certificate['certificate_id']),
        ('炉次', certificate['heat']),
        ('状态', certificate.get('status', '有效')),
        ('结算版本', certificate['settlement_version']),
        ('规则版本', certificate['rule_version']),
        ('生成时间', certificate['generated_at']),
        ('投料_kg', certificate['charge_kg']),
        ('产出阶段', (certificate.get('output') or {}).get('stage')),
        ('产出_kg', (certificate.get('output') or {}).get('value_kg')),
        ('产出修订', (certificate.get('output') or {}).get('revision')),
        ('产出率_pct', certificate['yield_pct']),
        ('用电_kWh', certificate['electricity_kwh']),
        ('排放_kgCO2e', certificate['emission_kg']),
        ('排放因子', certificate['factor']),
        ('因子版本', certificate['factor_version']),
        ('验收结论', (certificate.get('acceptance') or {}).get('decision')),
        ('验收时间', (certificate.get('acceptance') or {}).get('recorded_at')),
        ('验收修订', (certificate.get('acceptance') or {}).get('revision')),
        ('结算摘要', certificate['digest']),
        ('封印', certificate['seal']),
    ]
    for interval in certificate['intervals']:
        rows.append(('读数区间', '%s %s~%s %s kWh rev%s' % (
            interval['meter'], interval['interval_start'], interval['interval_end'],
            interval['value_kwh'], interval['revision'])))
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['字段', '值'])
    writer.writerows(rows)
    name = certificate['certificate_id'].replace('/', '_')
    return Response('﻿' + out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=%s.csv' % name})
