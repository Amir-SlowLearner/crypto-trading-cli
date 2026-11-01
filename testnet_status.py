"""Explicit T1B mock status command. There is no authenticated/network mode."""
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status', allow_abbrev=False,
                                 help='render the synthetic offline four-GET status scenario')
    status.add_argument('--mock-demo', action='store_true', required=True,
                        help='explicitly select invented responses; never connects to Phemex')
    parser.parse_args(argv)
    # Imports and fixture construction happen only after strict argument parsing.
    try:
        from testnet_status_collection import collect_status, mock_demo
        provider, clock = mock_demo()
        report = collect_status(provider, clock=clock)
        print(report.render())
        return 0 if report.complete else 2
    except (KeyboardInterrupt, EOFError):
        print('Mock status collection aborted; no network requests were made.', file=sys.stderr)
        return 130
    except Exception:
        print('Mock status unavailable; internal details withheld; networking remains disabled.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
