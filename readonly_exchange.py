"""Transport guard: only explicitly reviewed GET routes can reach CCXT transport."""
import json
import logging

import ccxt

from diagnostics import ResponseEvidence, exchange_code, response_reason
from safety import SafetyError


READ_ROUTES = frozenset({
    ('public', 'cfg/v2/products'), ('public', 'public/products'),
    ('public', 'public/products-plus'),
    ('v2', 'public/products'),
    ('v1', 'exchange/public/products'),
    ('v2', 'md/v2/orderbook'),
    ('private', 'g-orders/activeList'),
    ('private', 'g-accounts/accountPositions'), ('private', 'g-accounts/positions'),
    ('private', 'g-accounts/risk-unit'),
    ('private', 'uta-api/risk/risk-mode'), ('private', 'uta-api/risk/risk-units'),
    ('private', 'uta-biz/assets'),
})


class ReadOnlyPhemex(ccxt.phemex):
    def __init__(self, config=None):
        super().__init__(config or {})
        # CCXT logs signed headers and raw private responses at DEBUG even when
        # verbose=False. Use a disabled, instance-local logger, never a global one.
        self.logger = logging.Logger('phemex.readonly.private')
        self.logger.disabled = True
        self.logger.propagate = False
        self.reset_read_diagnostic()
        self._private_output_off()

    def _private_output_off(self):
        self.verbose = False
        self.enableLastHttpResponse = False
        self.enableLastJsonResponse = False
        self.enableLastResponseHeaders = False
        self.last_http_response = None
        self.last_json_response = None
        self.last_response_headers = None

    def reset_read_diagnostic(self):
        self.last_read_diagnostic = ResponseEvidence()

    def on_rest_response(self, code, reason, url, method, response_headers, response_body,
                         request_headers, request_body):
        # Called before CCXT raises for HTTP or exchange errors. Keep no response
        # text, headers, URLs, error messages or account data in diagnostic state.
        status = code if type(code) is int and 100 <= code <= 599 else None
        try:
            payload = json.loads(response_body)
        except (ValueError, TypeError):
            payload = None
        self.last_read_diagnostic = ResponseEvidence(status, exchange_code(payload), response_reason(payload))
        return super().on_rest_response(code, reason, url, method, response_headers,
                                        response_body, request_headers, request_body)

    def request(self, path, api='public', method='GET', params=None, config=None):
        self.reset_read_diagnostic()
        self._private_output_off()
        # The pinned generated implicit methods pass a single-element API list.
        # Accept the explicit string form too, then pass CCXT its expected list.
        api = [api] if isinstance(api, str) else api
        if (not isinstance(api, (list, tuple)) or len(api) != 1 or not isinstance(api[0], str)
                or not isinstance(path, str) or method != 'GET' or (api[0], path) not in READ_ROUTES):
            raise SafetyError('Read-only transport blocked an unapproved API route or method')
        # CCXT consumes this option before signing. Caller/global retry settings
        # must not turn one reviewed GET into several attempts.
        request_params = dict(params or {})
        request_params['maxRetriesOnFailure'] = 0
        # A redirect must not create another HTTP request outside the reviewed route.
        self.session.max_redirects = 0
        return super().request(path, list(api), method, request_params, config or {})
