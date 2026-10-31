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
    options = parser.parse_args(argv)
    if not options.authorize_testnet_read:
        print('No request made. Explicit --authorize-testnet-read is required.')
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
