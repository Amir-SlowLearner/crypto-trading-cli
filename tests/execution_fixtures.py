"""Invented evidence assertions for offline tests, never exchange verification."""
from datetime import datetime, timezone
from decimal import Decimal

from execution_policy import (AccountEvidence, CapabilityEvidence, EvidenceStamp, InstrumentEvidence,
                              OrderIntent, OrdersEvidence, PositionEvidence, Provenance, Source, evaluate)

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
BINDING = 'a' * 64


def evidence():
    def stamp(source, provenance=Provenance.AUTHENTICATED_TESTNET):
        return EvidenceStamp(source, provenance, NOW, BINDING, complete=True)
    return (
        OrderIntent('BTC/USDT:USDT', 'sell', 'reduce', 'long', Decimal('0.001'), Decimal('60000')),
        AccountEvidence(stamp(Source.ACCOUNT), True, 'classic', 'normal'),
        PositionEvidence(stamp(Source.POSITIONS), 'BTC/USDT:USDT', 'long', Decimal('0.002'),
                         'hedge', 'cross', Decimal('15'), 'normal'),
        InstrumentEvidence(stamp(Source.INSTRUMENTS, Provenance.PUBLIC_TESTNET), 'BTC/USDT:USDT', 'BTCUSDT',
                           True, 'linear_perpetual', 'USDT', Decimal('1'), Decimal('0.001'), Decimal('0.001'),
                           Decimal('0.001'), Decimal('100'), Decimal('100'), Decimal('1'), Decimal('0.1'),
                           Decimal('0.1'), Decimal('0.1'), Decimal('1000000')),
        OrdersEvidence(stamp(Source.OPEN_ORDERS), 'BTC/USDT:USDT', 0, 0),
        CapabilityEvidence(stamp(Source.COMPATIBILITY_REVIEW, Provenance.REVIEWED_COMPATIBILITY),
                           'classic', True, True, True),
    )


def validated():
    decision = evaluate(*evidence(), now=NOW)
    assert decision.allowed, decision.reasons
    return decision.validated
