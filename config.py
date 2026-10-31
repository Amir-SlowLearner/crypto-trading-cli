"""Explicit environment profiles. Loading configuration never changes os.environ."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values

from safety import SafetyError

PROJECT_DIR = Path(__file__).resolve().parent
CREDENTIAL_KEYS = ("PHEMEX_API_KEY", "PHEMEX_API_SECRET")


class ConfigError(SafetyError):
    pass


@dataclass(frozen=True)
class RiskLimits:
    max_notional_usdt: Decimal = Decimal("200")
    max_leverage: Decimal = Decimal("5")


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    live: bool = False
    limits: RiskLimits = field(default_factory=RiskLimits)


def positive_limit(values, name, default):
    try:
        result = Decimal(values.get(name, default))
    except (InvalidOperation, ValueError, TypeError):
        raise ConfigError(f"{name} must be a finite positive number") from None
    if not result.is_finite() or result <= 0:
        raise ConfigError(f"{name} must be a finite positive number")
    return result


def load_settings(*, live=False, env_file: Optional[Path] = None,
                  environ: Optional[Mapping[str, str]] = None):
    path = Path(env_file) if env_file is not None else PROJECT_DIR / ("live.env" if live else "test.env")
    environment = dict(os.environ if environ is None else environ)
    file_values = {}
    if path.exists():
        # Do not interpolate secrets through unrelated environment variables.
        # Read errors are intentionally reported without path contents or values.
        try:
            file_values = dict(dotenv_values(path, interpolate=False))
            if os.name == "posix" and any(k in file_values for k in CREDENTIAL_KEYS):
                if path.stat().st_mode & 0o077:
                    raise ConfigError("Credential file permissions must be owner-only (chmod 600)")
        except OSError:
            raise ConfigError("Cannot read the selected environment file") from None
    elif env_file is not None:
        raise ConfigError("The selected environment file does not exist")

    # A complete exported pair takes precedence. Never silently combine a key
    # from one source with a secret from another source.
    exported = [k in environment for k in CREDENTIAL_KEYS]
    if any(exported) and not all(exported):
        raise ConfigError("Export both PHEMEX_API_KEY and PHEMEX_API_SECRET, or neither")
    credentials = environment if all(exported) else file_values
    key, secret = (credentials.get(k) for k in CREDENTIAL_KEYS)
    if not key or not secret or not key.strip() or not secret.strip():
        raise ConfigError("Missing PHEMEX_API_KEY / PHEMEX_API_SECRET in the selected profile")

    values = {**file_values, **environment}
    live_opt_in = values.get("PHEMEX_ALLOW_LIVE", "false")
    if not isinstance(live_opt_in, str) or live_opt_in.lower() not in ("true", "false"):
        raise ConfigError("PHEMEX_ALLOW_LIVE must be true or false")
    if live and live_opt_in.lower() != "true":
        raise ConfigError("Live connectivity requires PHEMEX_ALLOW_LIVE=true and --live")
    limits = RiskLimits(
        max_notional_usdt=positive_limit(values, "MAX_NOTIONAL_USDT", "200"),
        max_leverage=positive_limit(values, "MAX_LEVERAGE", "5"),
    )
    return Settings(api_key=key, api_secret=secret, live=live, limits=limits)
