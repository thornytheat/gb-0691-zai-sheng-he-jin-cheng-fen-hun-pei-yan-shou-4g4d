from flask import Blueprint, render_template, request

from app.config import (LEDGER_LATE_SAMPLE, LEDGER_SAMPLE, MODULES, RULE_VERSION,
                        SHORT, TITLE)
from app.ledger import store

bp = Blueprint('ledger_web', __name__)


@bp.route('/ledger')
def heat_board():
    ident = request.args.get('heat')
    board = store.heats()
    selected = None
    if ident:
        selected = store.heat(ident)
    elif board['heats']:
        selected = store.heat(board['heats'][0]['heat'])
    return render_template('ledger.html', title=TITLE, modules=MODULES, short=SHORT,
                           section='ledger', rule_version=RULE_VERSION,
                           board=board, selected=selected,
                           sample=LEDGER_SAMPLE.read_text(encoding='utf-8-sig'),
                           late=LEDGER_LATE_SAMPLE.read_text(encoding='utf-8-sig'))


@bp.route('/ledger/certificates')
def certificate_board():
    ident = request.args.get('heat')
    board = store.heats()
    selected = store.heat(ident) if ident else (store.heat(board['heats'][0]['heat'])
                                                if board['heats'] else None)
    return render_template('certificates.html', title=TITLE, modules=MODULES, short=SHORT,
                           section='certificates', rule_version=RULE_VERSION,
                           board=board, selected=selected,
                           certificates=store.certificates_for(ident or None))
