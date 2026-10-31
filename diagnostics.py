"""Bounded diagnostic identifiers only; never inspect exception text or retain payloads."""
from dataclasses import dataclass
import re

import ccxt
from requests.exceptions import SSLError, TooManyRedirects

from safety import SafetyError


def exchange_code(payload):
    if not isinstance(payload, dict):
        return None
    error = payload.get('error')
    row = error if isinstance(error, dict) else payload
    value = row.get('code')
    if type(value) not in (int, str):
        return None
    value = str(value)
    return value if re.fullmatch(r'-?[0-9]{1,10}', value) else None


def response_reason(payload):
    """Translate known error phrases to fixed labels; never return response text.

    Phemex reuses code 10500 for several unrelated failures. Error descriptions
    can contain API keys, so only the labels below may leave this function.
    """
    if exchange_code(payload) in (None, '0'):
        return 'unclassified'
    error = payload.get('error')
    row = error if isinstance(error, dict) else payload
    phrases = (
        ('Failed to find api-key', 'api_key_unrecognized'),
        ('Api key not found', 'api_key_unrecognized'),
        ('401 Failed to load API KEY', 'api_key_unrecognized'),
        ('API Signature verification failed', 'signature_rejected'),
        ('401 Request IP mismatch', 'ip_restricted'),
        ('401 Insufficient privilege', 'permission_rejected'),
        ('Access forbidden', 'permission_rejected'),
        ('Access denied', 'permission_rejected'),
        ('Too many requests', 'rate_limited'),
        ('Missing required parameter', 'missing_parameter'),
        ('Missing parameter -', 'missing_parameter'),
        ('Please try again later', 'retry_later'),
    )
    reasons = {reason for name in ('msg', 'message') for phrase, reason in phrases
               if isinstance(row.get(name), str) and phrase in row[name]}
    if len(reasons) > 1:
        return 'conflicting_error_evidence'
    return next(iter(reasons), 'unclassified')


@dataclass(frozen=True)
class ResponseEvidence:
    http_status: int = None
    exchange_code: str = None
    reason: str = 'unclassified'


MESSAGES = {
    'authentication': 'authentication failed',
    'permission': 'permission denied',
    'unsupported': 'endpoint unsupported by adapter',
    'http_not_found': 'HTTP endpoint not found; support not established',
    'method_not_allowed': 'HTTP method not allowed',
    'bad_request': 'request rejected as invalid',
    'rate_limit': 'rate limited; no retry',
    'server_error': 'HTTP server unavailable; no retry',
    'http_error': 'HTTP request rejected',
    'timeout': 'network timeout; no retry',
    'network': 'network outcome unavailable; no retry',
    'tls': 'TLS connection failed; no retry',
    'redirect': 'redirect failed; no retry',
    'exchange_unavailable': 'exchange unavailable; no retry',
    'exchange_error': 'exchange request failed',
    'invalid_response': 'invalid or unsuccessful response',
    'local_error': 'local request failure; exchange outcome unverified',
}


@dataclass(frozen=True)
class FailureDiagnostic:
    category: str
    http_status: int = None
    exchange_code: str = None
    error_type: str = None
    reason: str = 'unclassified'
    generic_exchange_error: bool = False

    @property
    def stop(self):
        # Preserve a global stop for every 5xx, even when its semantic category
        # is more specific. Only the fixed account-discovery probe may override it.
        return (self.http_status in (401, 429)
                or (self.http_status is not None and self.http_status >= 500)
                or self.category in {'authentication', 'rate_limit', 'server_error',
                                     'timeout', 'network', 'tls', 'redirect', 'exchange_unavailable'})

    def __str__(self):
        return (f'{MESSAGES[self.category]} [category={self.category}; '
                f'http={self.http_status if self.http_status is not None else "unknown"}; '
                f'code={self.exchange_code if self.exchange_code is not None else "unknown"}; '
                f'type={self.error_type or "unknown"}; reason={self.reason}]')


def diagnose(error=None, evidence=None, response=None):
    evidence = evidence if isinstance(evidence, ResponseEvidence) else ResponseEvidence()
    status = evidence.http_status
    code = exchange_code(response) if response is not None else evidence.exchange_code
    reason = response_reason(response) if response is not None else evidence.reason
    # Use fixed class names, never an arbitrary subclass name or exception message.
    kinds = ((ccxt.PermissionDenied, 'permission'),
             (ccxt.AuthenticationError, 'authentication'),
             (ccxt.NotSupported, 'unsupported'),
             (ccxt.RateLimitExceeded, 'rate_limit'),
             (ccxt.RequestTimeout, 'timeout'),
             (ccxt.ExchangeNotAvailable, 'exchange_unavailable'),
             (ccxt.NetworkError, 'network'),
             (ccxt.BadRequest, 'bad_request'),
             (ccxt.ExchangeError, 'exchange_error'))
    category, error_type = ('invalid_response', None) if error is None else ('local_error', 'Exception')
    for cls, kind in kinds:
        if isinstance(error, cls):
            category, error_type = kind, cls.__name__
            break
    # Pinned CCXT wraps these transport failures in generic ExchangeError. Inspect
    # only the direct cause's type, never its text, URL, headers or response.
    cause = getattr(error, '__cause__', None)
    if isinstance(cause, SSLError):
        category, error_type = 'tls', 'SSLError'
    elif isinstance(cause, TooManyRedirects):
        category, error_type = 'redirect', 'TooManyRedirects'
    elif category == 'local_error':
        for cls in (SafetyError, TypeError, ValueError, AttributeError, KeyError, RuntimeError):
            if isinstance(error, cls):
                error_type = cls.__name__
                break
    # Keep explicit semantic evidence ahead of a generic HTTP 500. In particular,
    # 10500 alone proves neither authentication failure nor endpoint support.
    reason_categories = {'api_key_unrecognized': 'authentication',
                         'signature_rejected': 'authentication', 'ip_restricted': 'permission',
                         'permission_rejected': 'permission', 'rate_limited': 'rate_limit',
                         'missing_parameter': 'bad_request'}
    code_categories = {'401': 'authentication', '34003': 'permission',
                       '39996': 'permission', '39995': 'rate_limit'}
    if category in {'authentication', 'permission', 'rate_limit', 'timeout', 'network', 'tls', 'redirect'}:
        pass
    elif status in (401, 403, 429):
        category = {401: 'authentication', 403: 'permission', 429: 'rate_limit'}[status]
    elif reason in reason_categories:
        category = reason_categories[reason]
    elif code in code_categories:
        category = code_categories[code]
    # HTTP failures must not be mislabeled transport failures by CCXT's hierarchy.
    # A 404 alone does not prove that an authenticated endpoint is unsupported.
    elif status is not None and status >= 300:
        category = {400: 'bad_request', 401: 'authentication', 403: 'permission',
                    404: 'http_not_found', 405: 'method_not_allowed',
                    429: 'rate_limit'}.get(status, 'server_error' if status >= 500 else 'http_error')
    elif error is None and code not in (None, '0'):
        category = 'exchange_error'
    return FailureDiagnostic(category, status, code, error_type, reason,
                             type(error) is ccxt.ExchangeError)
