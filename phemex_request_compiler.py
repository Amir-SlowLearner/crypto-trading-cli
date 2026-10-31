"""Pure compilation of an OFFLINE business request. Never signs or sends it.

This intentionally implements only a reviewed subset of CCXT 4.5.90. Evidence
labels remain assertions, not proof that anything came from an exchange.
"""
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
import hashlib
import inspect
import json
import re

import ccxt
from ccxt.base.decimal_to_precision import decimal_to_precision
from ccxt.base.exchange import Exchange

from execution_policy import ValidatedIntent, evaluate, fingerprint
from safety import SafetyError


CCXT_VERSION = '4.5.90'
# Exact installed Python source, not an assertion of exchange compatibility.
SOURCE_PINS = (
    ('create_order', '4fe741ac3fa85be50631eee49d3fcc931f474bb16f656db46a51ad65593d943c'),
    ('describe', 'e7b84f73392400ef5d8e19877c61c5132d0b520b53dbf4c1b85e6d55c4ca297f'),
    ('amount_to_precision', 'cefc0488d30c72769858ee593a7411254449b3ddecf23bd7b8203eff390ea6a3'),
    ('price_to_precision', '85975fae916aa4d2895edd445d15ee862a8aff799ee233a3b89f4257be259c6e'),
    ('decimal_to_precision', '9740601cc47daee40d7dca857071907e1750ad0df8e610326e94ba8cdb9fd4bd'),
)
PROFILE = 'phemex-4.5.90-btc-hedge-ioc-v1'


class CompilationError(SafetyError):
    """Fixed diagnostic messages; no private evidence or library errors."""


def verify_installed_ccxt():
    """Explicit OFFLINE source inspection, separate from pure compilation.

    Never constructs an exchange, invokes create_order, or enables transport.
    """
    try:
        if ccxt.__version__ != CCXT_VERSION:
            raise ValueError
        functions = {'create_order': ccxt.phemex.create_order, 'describe': ccxt.phemex.describe,
                     'amount_to_precision': Exchange.amount_to_precision,
                     'price_to_precision': Exchange.price_to_precision,
                     'decimal_to_precision': decimal_to_precision}
        for name, digest in SOURCE_PINS:
            if hashlib.sha256(inspect.getsource(functions[name]).encode()).hexdigest() != digest:
                raise ValueError
    except (OSError, TypeError, ValueError, AttributeError):
        raise CompilationError('Installed CCXT source profile requires review') from None
    return PROFILE


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _decimal_string(value):
    # Formatting only. No Decimal arithmetic, rounding, or float conversion.
    result = format(Decimal(value), 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _precision_string(value, tick, rounding):
    try:
        with localcontext() as context:
            context.prec = 100
            result = decimal_to_precision(value, rounding, str(tick), ccxt.TICK_SIZE, ccxt.NO_PADDING)
        if Decimal(result) != Decimal(value):
            raise ValueError
    except (DecimalException, ValueError, AssertionError, TypeError):
        raise CompilationError('CCXT precision would alter the validated value') from None
    return _decimal_string(result)


@dataclass(frozen=True)
class CompiledRequest:
    plan: ValidatedIntent
    context: tuple
    client_order_id: str
    parameters: tuple
    profile: str = PROFILE

    @property
    def unsigned_business_request(self):
        # Returned dictionaries are copies; they cannot modify this frozen model.
        return {'environment': 'testnet', 'origin': 'https://testnet-api.phemex.com',
                'method': 'POST', 'api': 'private', 'path': '/g-orders',
                'parameters': dict(self.parameters)}

    @property
    def canonical_request(self):
        return _json(self.unsigned_business_request)

    @property
    def request_fingerprint(self):
        return fingerprint(self.canonical_request)

    @property
    def submission_enabled(self):
        return False

    @property
    def authenticated_evidence_verified(self):
        return False


def compile_request(intent, account, position, instrument, orders, capabilities, *, now, client_order_id):
    """Reevaluate typed assertions and compile a deterministic unsigned request.

    No I/O, credentials, CCXT exchange construction, signing, or transport. This
    neither establishes evidence authenticity nor creates execution authority.
    """
    if ccxt.__version__ != CCXT_VERSION:
        raise CompilationError('Unsupported CCXT version')
    if type(client_order_id) is not str or not re.fullmatch('tn_[0-9a-f]{32}', client_order_id):
        raise CompilationError('A reserved-format testnet client order ID is required')
    context = (intent, account, position, instrument, orders, capabilities)
    decision = evaluate(*context, now=now)
    if not decision.allowed:
        raise CompilationError('Offline policy rejected request compilation')
    # The inspected USDT branch writes amount directly to orderQtyRq. Requiring
    # size=1 avoids silently conflating contracts with base units for other sizes.
    if instrument.contract_size != Decimal('1'):
        raise CompilationError('Non-unit contract mapping is unverified')
    plan = decision.validated
    values = dict(plan.payload)
    if Decimal(values['contracts']) != Decimal(values['quantity']):
        raise CompilationError('Contract and native quantity units disagree')
    parameters = tuple(sorted({
        'symbol': 'BTCUSDT', 'side': values['side'].capitalize(), 'ordType': 'Limit',
        'posSide': values['position_side'].capitalize(), 'reduceOnly': True,
        'timeInForce': 'ImmediateOrCancel', 'clOrdID': client_order_id,
        'orderQtyRq': _precision_string(values['quantity'], instrument.contract_step, ccxt.TRUNCATE),
        'priceRp': _precision_string(values['price'], instrument.price_precision_tick, ccxt.ROUND),
    }.items()))
    return CompiledRequest(plan, context, client_order_id, parameters)


def check_compiled(request):
    """Reject missing/extra/changed fields, even when a hash was recomputed.

    Rechecks the original evidence window; consumption additionally checks the
    current journal clock. Still only checks assertions, never their origin.
    """
    if (type(request) is not CompiledRequest or type(request.plan) is not ValidatedIntent
            or type(request.context) is not tuple or len(request.context) != 6
            or type(request.parameters) is not tuple
            or any(type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str
                   or type(pair[1]) not in (str, bool) for pair in request.parameters)):
        raise CompilationError('Invalid offline compiled request')
    expected = compile_request(*request.context, now=request.plan.validated_at,
                               client_order_id=request.client_order_id)
    if request != expected or request.canonical_request != expected.canonical_request:
        raise CompilationError('Compiled request differs from the reviewed plan')
    return request
