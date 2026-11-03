"""Optional explicitly authorized integration check; never run by offline discovery."""
import argparse
from pathlib import Path
from urllib.parse import urlparse

from config import ConfigError, load_settings
from exchange_client import PhemexClient
from safety import SafetyError
from trade_cli import print_balances, print_positions, print_report_header, report_exchange_error


def main(argv=None, *, client_factory=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--authorize-testnet-read', action='store_true',
                        help='authorize authenticated GET-only inspection of the selected TESTNET account')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--status', action='store_true', help='manual USDT linear perpetual Testnet status; no risk probes')
    def market_selector(value):
        from manual_market_catalog import valid_selector
        if not valid_selector(value):
            raise argparse.ArgumentTypeError('expected a native or unified perpetual symbol')
        return value
    parser.add_argument('--symbol', type=market_selector, help='native or unified symbol; default BTCUSDT')
    parser.add_argument('--markets', action='store_true', help='status only: refresh and list public market support')
    parser.add_argument('--credentials-env', action='store_true',
                        help='status only: explicitly select PHEMEX_TESTNET_API_KEY / PHEMEX_TESTNET_API_SECRET')
    options = parser.parse_args(argv)
    if not options.status and (options.symbol is not None or options.credentials_env or options.markets):
        parser.error('--symbol, --markets and --credentials-env require --status')
    if options.markets and options.symbol is not None:
        parser.error('--markets and --symbol are mutually exclusive')
    if options.status and (bool(options.env_file) == options.credentials_env):
        parser.error('--status requires exactly one of --env-file or --credentials-env')
    if not options.authorize_testnet_read:
        print('No request made. Explicit --authorize-testnet-read is required.')
        return 2
    if options.status:
        # Separate manual development path. Never calls the protected T1C worker
        # or its bootstrap, and never promotes a local flag to trading authority.
        try:
            from manual_testnet_status import load_credentials, collect_manual_status
            credentials = load_credentials(options.env_file, use_environment=options.credentials_env)
            report = collect_manual_status(credentials, symbol=options.symbol or 'BTCUSDT', list_markets=options.markets)
            print(report.render())
            return 0 if report.complete else 2
        except (KeyboardInterrupt, EOFError):
            print('Manual Testnet status aborted; no retry or exchange mutations.')
            return 130
        except Exception:
            print('Manual Testnet status unavailable; private details withheld; no retry.')
            return 2
    try:
        settings = load_settings(live=False, env_file=options.env_file)
        factory = client_factory or PhemexClient
        client = factory(settings.api_key, settings.api_secret, live=False, limits=settings.limits)
        if client.live or not client.ex.isSandboxModeEnabled:
            raise SafetyError('Integration check requires testnet sandbox mode')
        if any(urlparse(url).hostname != 'testnet-api.phemex.com' for url in client.ex.urls['api'].values()):
            raise SafetyError('Integration check rejected a non-testnet API endpoint')
        report = client.account()
        print_report_header(report)
        print_balances(report)
        print_positions(report, report.positions, modes_only=True)
        print('This GET-only snapshot does not establish trading safety; no raw responses were saved.')
        return 0 if report.complete else 2
    except (ConfigError, SafetyError) as error:
        print(f'Configuration/safety error: {error}')
        return 2
    except (KeyboardInterrupt, EOFError):
        print('Aborted; no exchange mutations are enabled.')
        return 2
    except Exception as error:
        report_exchange_error(error)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
