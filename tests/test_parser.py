from command_parser import CommandError, SCHEMAS, parse
from tests.helpers import OfflineTestCase


class ParserTests(OfflineTestCase):
    def test_valid_commands(self):
        examples = [
            'buy symbol=BTCUSDT qty=0.002 dry',
            'SELL symbol=ETHUSDT usdt=50 price=2000 type=LIMIT position=short dry',
            'buy symbol=SOLUSDT qty=1 sl=100 tp=200 cl_id=valid-id dry',
            'amend symbol=XRPUSDT ord_id=order-1 qty=20 dry',
            'cancel symbol=BTCUSDT cl_id=client-1 dry',
            'cancelall symbol=BTCUSDT dry', 'close symbol=BTCUSDT position=long dry',
            'leverage symbol=BTCUSDT x=2 dry', 'positions', 'positions symbol=BTCUSDT',
            'orders symbol=BTCUSDT', 'info symbol=BTCUSDT', 'balance', 'help', 'exit', 'quit',
            'account', 'modes', 'modes symbol=ETHUSDT',
        ]
        for line in examples:
            with self.subTest(line=line): parse(line.split())

    def test_rejects_dangerous_ambiguous_or_ignored_arguments(self):
        examples = [
            '', 'unknown', 'buy qty=1', 'buy symbol=BTCUSDT',
            'buy symbol=BTCUSDT qty=1 usdt=100 dry',
            'buy symbol=BTCUSDT qty=1 qty=2 dry',
            'buy symbol=BTCUSDT qty=1 SYMBOL=ETHUSDT dry',
            'buy symbol=BTCUSDT qty=1 stoploss=90 dry',
            'buy symbol=BTCUSDT qty=1 dyr',
            'buy symbol=BTCUSDT qty=1 dry dry', 'buy symbol=BTCUSDT qty=1 dry=true',
            'buy symbol=BTCUSDT qty=', 'buy symbol=BTCUSDT qty=1=2',
            'buy symbol=BTCUSDT qty=1 type=market price=100',
            'buy symbol=BTCUSDT qty=1 type=limit',
            'buy symbol=BTCUSDT qty=1 type=stop',
            'buy symbol=BTCUSDT qty=1 position=both',
            'amend symbol=BTCUSDT ord_id=id',
            'amend symbol=BTCUSDT qty=1',
            'cancel symbol=BTCUSDT ord_id=id cl_id=other',
            'cancel symbol=BTCUSDT',
            'cancelall', 'close symbol=BTCUSDT qty=0.1 dry',
            'close symbol=BTCUSDT usdt=100 dry', 'close symbol=BTCUSDT position=Merged dry',
            'leverage symbol=BTCUSDT', 'balance symbol=BTCUSDT',
            'orders symbol=BTCUSDT dry', 'positions dry', 'help extra', 'exit extra',
            'quit confirm=yes', 'buy symbol=BTCUSDT qty=1 cl_id=' + 'a' * 41,
            'account symbol=BTCUSDT', 'modes dry',
            'buy symbol=BTCUSDT qty=1 cl_id=unsafe/id',
        ]
        for line in examples:
            with self.subTest(line=line), self.assertRaises(CommandError): parse(line.split())

    def test_all_numeric_fields_require_finite_positive_values(self):
        for name in ('qty', 'usdt', 'price', 'sl', 'tp', 'x'):
            for value in ('NaN', 'sNaN', 'Infinity', '-Infinity', '0', '-1', 'secret'):
                if name == 'x': line = f'leverage symbol=BTCUSDT x={value}'
                elif name in {'qty', 'usdt'}: line = f'buy symbol=BTCUSDT {name}={value}'
                else: line = f'buy symbol=BTCUSDT qty=1 {name}={value}'
                with self.subTest(name=name, value=value), self.assertRaises(CommandError): parse(line.split())

    def test_every_schema_rejects_unknown_arguments_and_flags(self):
        for cmd in SCHEMAS:
            for suffix in ('secret=DO_NOT_ECHO', 'DO_NOT_ECHO'):
                with self.subTest(cmd=cmd, suffix=suffix), self.assertRaises(CommandError) as caught:
                    parse([cmd, suffix])
                self.assertNotIn('DO_NOT_ECHO', str(caught.exception))
