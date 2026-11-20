import csv
import io

import pytest

from app.config import LEDGER_LATE_SAMPLE, LEDGER_SAMPLE
from app.errors import DomainError
from app.ledger import store
from app.ledger.settle import explain, parse, settle

HEADER = ['record_type', 'heat', 'stage', 'recorded_at', 'meter',
          'interval_start', 'interval_end', 'value', 'unit', 'revision', 'source']


def ledger(*rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADER)
    writer.writerows(rows)
    return out.getvalue()


def sample(name):
    return LEDGER_SAMPLE.read_text(encoding='utf-8-sig') if name == 'main' \
        else LEDGER_LATE_SAMPLE.read_text(encoding='utf-8-sig')


def by_heat(result):
    return {heat['heat']: heat for heat in result['heats']}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_DB', str(tmp_path / 'ledger.sqlite3'))
    return str(tmp_path / 'ledger.sqlite3')


def test_settled_heat_has_output_power_and_emission():
    heat = by_heat(settle(parse(sample('main'))))['F-1108']
    assert heat['settled'] is True
    assert heat['output_kg'] == '2847.8800'
    assert heat['charge_kg'] == '3000.0000'
    assert heat['yield_pct'] == '94.9293'
    assert heat['electricity_kwh'] == '2100.0000'
    assert heat['emission_kg'] == '1197.6300'


def test_duplicate_reading_counted_once():
    heat = by_heat(settle(parse(sample('main'))))['F-1109']
    assert heat['electricity_kwh'] == '1960.0000'
    assert heat['duplicate_records'] == 1


def test_repeated_ingest_does_not_double_count(db):
    store.ingest(sample('main'))
    first = by_heat(store.heats())['F-1109']
    again = store.ingest(sample('main'))['ingest']
    second = by_heat(store.heats())['F-1109']
    assert again['inserted'] == 0
    assert first['electricity_kwh'] == second['electricity_kwh'] == '1960.0000'


def test_same_interval_is_never_charged_twice_in_one_snapshot():
    text = ledger(
        ['charge', 'H1', '投料', '2026-11-18T08:00:00Z', '', '', '', '100', 'kg', '1', 'a'],
        ['output', 'H1', '产出', '2026-11-18T12:00:00Z', '', '', '', '90', 'kg', '1', 'a'],
        ['meter', 'H1', '', '', 'M', '2026-11-18T08:00:00Z', '2026-11-18T12:00:00Z', '50', 'kWh', '1', 'a'],
        ['meter', 'H1', '', '', 'M', '2026-11-18T08:00:00Z', '2026-11-18T12:00:00Z', '50', 'kWh', '1', 'a'],
        ['acceptance', 'H1', '验收', '2026-11-18T13:00:00Z', '', '', '', '合格', '', '1', 'a'])
    heat = by_heat(settle(parse(text)))['H1']
    assert heat['electricity_kwh'] == '50.0000'
    assert heat['duplicate_records'] == 1


def test_overlapping_intervals_block_settlement():
    heat = by_heat(settle(parse(sample('main'))))['F-1111']
    assert heat['settled'] is False
    assert heat['electricity_kwh'] is None
    assert any(reason['code'] == 'METER_OVERLAP' for reason in heat['reasons'])


def test_conflicting_reading_is_isolated_not_averaged():
    heat = by_heat(settle(parse(sample('main'))))['F-1112']
    assert heat['settled'] is False
    assert heat['electricity_kwh'] is None
    assert '用电' in heat['unsettled_fields']


def test_rework_invalidates_old_acceptance():
    heat = by_heat(settle(parse(sample('main'))))['F-1110']
    assert heat['output']['stage'] == '返工'
    assert heat['output']['revision'] == 2
    assert heat['settled'] is False
    assert any(reason['code'] == 'ACCEPTANCE_STALE' for reason in heat['reasons'])
    assert [row['revision'] for row in heat['superseded_outputs']] == [1]


def test_rework_reacceptance_settles_and_explains():
    before = by_heat(settle(parse(sample('main'))))['F-1110']
    combined = parse(sample('main'))['rows'] + parse(sample('late'))['rows']
    after = by_heat(settle({'rows': combined, 'quarantine': [], 'counts': {}}))['F-1110']
    assert after['settled'] is True
    assert after['acceptance']['revision'] == 2
    changes = explain(before, after)
    assert any('验收更新' in change for change in changes)


def test_late_reading_changes_amounts_and_is_explained():
    before = by_heat(settle(parse(sample('main'))))['F-1109']
    combined = parse(sample('main'))['rows'] + parse(sample('late'))['rows']
    after = by_heat(settle({'rows': combined, 'quarantine': [], 'counts': {}}))['F-1109']
    assert after['electricity_kwh'] == '2600.0000'
    assert after['emission_kg'] == '1482.7800'
    changes = explain(before, after)
    assert any('补入 1 个' in change for change in changes)
    assert any('排放' in change for change in changes)


def test_settlement_version_only_grows_on_change(db):
    store.ingest(sample('main'))
    assert by_heat(store.heats())['F-1109']['version'] == 1
    store.ingest(sample('main'))
    assert by_heat(store.heats())['F-1109']['version'] == 1
    store.ingest(sample('late'))
    assert by_heat(store.heats())['F-1109']['version'] == 2
    history = store.heat('F-1109')['versions']
    assert [row['version'] for row in history] == [1, 2]
    assert history[0]['payload']['electricity_kwh'] == '1960.0000'


def test_certificate_requires_settled_heat(db):
    store.ingest(sample('main'))
    with pytest.raises(DomainError) as error:
        store.issue('F-1110')
    assert error.value.status == 409 and error.value.code == 'UNSETTLED'
    assert store.certificates_for('F-1110') == []


def test_certificate_is_idempotent_per_settlement(db):
    store.ingest(sample('main'))
    first = store.issue('F-1108')
    second = store.issue('F-1108')
    assert first['issued'] is True and second['issued'] is False
    assert first['certificate']['certificate_id'] == second['certificate']['certificate_id']
    assert len(store.certificates_for('F-1108')) == 1


def test_revision_keeps_old_certificate_recoverable(db):
    store.ingest(sample('main'))
    old = store.issue('F-1109')['certificate']
    store.ingest(sample('late'))
    assert store.certificate(old['certificate_id'])['status'] == '待换证'
    new = store.issue('F-1109')['certificate']
    assert new['certificate_id'] != old['certificate_id']
    assert store.certificate(old['certificate_id'])['status'] == '已替代'
    assert store.certificate(old['certificate_id'])['superseded_by'] == new['certificate_id']
    assert store.certificate(old['certificate_id'])['electricity_kwh'] == '1960.0000'
    assert new['electricity_kwh'] == '2600.0000'


def test_unsetsettled_heat_invalidates_certificate_but_keeps_row(db):
    store.ingest(sample('main'))
    old = store.issue('F-1108')['certificate']
    extra = ledger(['meter', 'F-1108', '', '', 'M-01',
                    '2026-11-18T16:00:00Z', '2026-11-18T18:00:00Z', '10', 'kWh', '1', 'overlap'])
    store.ingest(extra)
    assert store.certificate(old['certificate_id'])['status'] == '已失效'
    with pytest.raises(DomainError):
        store.issue('F-1108')


def test_failed_certificate_keeps_previous_version(db, monkeypatch):
    store.ingest(sample('main'))
    old = store.issue('F-1109')['certificate']
    store.ingest(sample('late'))

    def broken(*args, **kwargs):
        raise RuntimeError('disk full')

    monkeypatch.setattr(store, '_seal', broken)
    with pytest.raises(DomainError) as error:
        store.issue('F-1109')
    assert error.value.code == 'CERT_FAILED'
    kept = store.certificate(old['certificate_id'])
    assert kept['status'] in ('待换证', '有效')
    assert kept['electricity_kwh'] == '1960.0000'
    assert len(store.certificates_for('F-1109')) == 1


def test_reverting_settlement_reactivates_original_certificate(db):
    store.ingest(sample('main'))
    old = store.issue('F-1109')['certificate']
    store.ingest(sample('late'))
    new = store.issue('F-1109')['certificate']
    assert store.certificate(old['certificate_id'])['status'] == '已替代'
    # 晚到读数被上游撤回（台账只追加，这里模拟更正后移除该读数）
    with store.connection() as conn:
        conn.execute('DELETE FROM ledger_records WHERE heat=? AND record_key LIKE ?',
                     ('F-1109', 'meter|M-02|2026-11-18T15:30:00Z%'))
    back = store.issue('F-1109')
    assert back['issued'] is False
    assert back['certificate']['certificate_id'] == old['certificate_id']
    assert store.certificate(old['certificate_id'])['status'] == '有效'
    assert store.certificate(new['certificate_id'])['status'] == '待换证'


def test_certificate_contains_factor_and_intervals(db):
    store.ingest(sample('main'))
    certificate = store.issue('F-1108')['certificate']
    assert certificate['factor'] == '0.5703'
    assert certificate['factor_version']
    assert len(certificate['intervals']) == 2
    assert certificate['seal']


def test_unparseable_ledger_is_rejected():
    with pytest.raises(DomainError) as error:
        parse('record_type,heat\nmeter,F-1\n')
    assert error.value.code == 'HEADER'
    with pytest.raises(DomainError):
        parse('')
    zero_span = parse(ledger(['meter', 'H1', '', '', 'M', '2026-11-18T08:00:00Z',
                              '2026-11-18T08:00:00Z', '5', 'kWh', '1', 'a']))
    assert zero_span['quarantine'][0]['原因码'] == 'RANGE'


def test_bad_rows_are_quarantined_with_line_numbers():
    parsed = parse(ledger(['charge', '', '投料', '2026-11-18T08:00:00Z', '', '', '',
                           '100', 'kg', '1', 'a'],
                          ['charge', 'H1', '投料', 'not-a-time', '', '', '', '100', 'kg', '1', 'a'],
                          ['meter', 'H1', '', '', 'M', '2026-11-18T08:00:00Z',
                           '2026-11-18T12:00:00Z', '5', 'MWh', '1', 'a']))
    codes = [row['原因码'] for row in parsed['quarantine']]
    assert codes == ['MISSING', 'TIME', 'UNIT']
    assert [row['行号'] for row in parsed['quarantine']] == [2, 3, 4]


def test_pages_render(client):
    assert client.get('/ledger').status_code == 200
    assert client.get('/ledger/certificates').status_code == 200
    run = client.post('/api/ledger/ingest', json={}).get_json()
    assert run['ingest']['inserted'] > 0
    page = client.get('/ledger?heat=F-1110').data.decode()
    assert 'F-1110' in page and 'ACCEPTANCE_STALE' in page
    assert client.get('/ledger/certificates').status_code == 200


def test_api_certificate_flow(client):
    client.post('/api/ledger/ingest', json={})
    refused = client.post('/api/ledger/certificate', json={'heat': 'F-1110'})
    assert refused.status_code == 409 and refused.get_json()['code'] == 'UNSETTLED'
    issued = client.post('/api/ledger/certificate', json={'heat': 'F-1108'}).get_json()
    assert issued['issued'] is True
    ident = issued['certificate']['certificate_id']
    listing = client.get('/api/ledger/certificates?heat=F-1108').get_json()
    assert [row['id'] for row in listing] == [ident]
    assert client.get('/api/ledger/certificates/' + ident).get_json()['status'] == '有效'
    exported = client.get('/api/ledger/certificate.csv?cert_id=' + ident).data.decode('utf-8-sig')
    assert '排放_kgCO2e,1197.6300' in exported and '封印' in exported


def test_api_rejects_bad_requests(client):
    assert client.post('/api/ledger/certificate', json={}).status_code == 422
    assert client.post('/api/ledger/certificate', json={'heat': 'nope'}).status_code == 404
    assert client.get('/api/ledger/heats/nope').status_code == 404
    assert client.post('/api/ledger/ingest', json={'csv': ''}).status_code == 422
    assert client.get('/api/ledger/certificate.csv').status_code == 422
    import io
    assert client.post('/api/ledger/ingest',
                       data={'wrong': (io.BytesIO(b'a,b'), 'x.csv')}).status_code == 400


def test_ledger_upload_form(client):
    import io
    payload = LEDGER_SAMPLE.read_bytes()
    response = client.post('/api/ledger/ingest',
                           data={'file': (io.BytesIO(payload), 'ledger.csv')},
                           content_type='multipart/form-data')
    assert response.status_code == 200
    assert client.get('/api/ledger/heats').get_json()['heats']


def test_reingest_same_file_is_a_no_op(db):
    first = store.ingest(sample('main'))['ingest']
    second = store.ingest(sample('main'))['ingest']
    assert first['inserted'] == 25 and second['inserted'] == 0
    assert second['reused'] is True
    heats = by_heat(store.heats())
    assert heats['F-1109']['duplicate_records'] == first['counts']['duplicate']
    assert heats['F-1109']['version'] == 1


def test_late_file_alone_does_not_duplicate_existing_readings(db):
    store.ingest(sample('main'))
    store.ingest(sample('late'))
    store.ingest(sample('late'))
    heat = by_heat(store.heats())['F-1109']
    assert heat['electricity_kwh'] == '2600.0000'
    assert len(heat['intervals']) == 3
    assert heat['version'] == 2
