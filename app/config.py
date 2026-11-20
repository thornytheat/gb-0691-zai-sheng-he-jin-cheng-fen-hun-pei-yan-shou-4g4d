from pathlib import Path
RULE_VERSION = "2026.11.19.1"
TITLE = '再生合金成分混配与验收'
SHORT = 'alloy-blend'
MODULES = ['来料接收', '成分归一', '配方约束', '混配试算', '不确定区间', '验收对照', '复配批次']
HEADERS = ['lot', 'batch', 'mass', 'unit', 'moisture', 'element', 'assay', 'basis', 'revision']
SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "operations.csv"
LIMIT = 10000

# 炉次结算台账：与成分/验收管线分开采数，证明只在“验收完成 + 结算完成”后出具。
LEDGER_HEADERS = ['record_type', 'heat', 'stage', 'recorded_at', 'meter',
                  'interval_start', 'interval_end', 'value', 'unit', 'revision', 'source']
LEDGER_SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "ledger.csv"
LEDGER_LATE_SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "ledger-late.csv"
EMISSION_FACTOR = '0.5703'
EMISSION_FACTOR_VERSION = '电网边际排放因子 2026.1'
LEDGER_LIMIT = 10000
