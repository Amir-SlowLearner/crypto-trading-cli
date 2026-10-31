"""Compatibility entry point for the consolidated, safety-gated CCXT CLI.

The original native terminal is recoverable from Git. Importing this module
never loads credentials, creates a client, starts a prompt or contacts Phemex.
"""
from trade_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
