"""Isolated SQLite simulation. No exchange, credentials, callbacks, or transport.

SIMU application-ID databases cannot be opened by OperationJournal. Conversely,
this module refuses ordinary journals before recovery can change any operation.
"""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from execution_policy import canonical_payload, fingerprint
from operation_journal import (Authorization, JournalError, OperationJournal, State)
from phemex_request_compiler import check_compiled, verify_installed_ccxt


@dataclass(frozen=True)
class SimulationAuthorization:
    authorization: Authorization
    request_fingerprint: str
    simulation_only: bool = True


class SimulationJournal(OperationJournal):
    APPLICATION_ID = 0x53494D55  # SIMU: never an execution journal.

    def _initialize(self):
        super()._initialize()
        self._connection.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE simulation_requests (
                operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                request TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                profile TEXT NOT NULL);
            CREATE TRIGGER immutable_simulation_request BEFORE UPDATE ON simulation_requests
                BEGIN SELECT RAISE(ABORT,'immutable simulation request'); END;
            CREATE TRIGGER retained_simulation_request BEFORE DELETE ON simulation_requests
                BEGIN SELECT RAISE(ABORT,'retained simulation request'); END;
            COMMIT;
        ''')

    def _recover(self):
        # A crash during creation leaves an unusable simulation DB, not a journal
        # that can be reinterpreted as execution data. No implicit migration.
        self._connection.execute('SELECT operation_id FROM simulation_requests LIMIT 0')
        super()._recover()

    def reserve(self, *args, **kwargs):
        raise JournalError('Simulation reservations require a compiled request')

    def reserve_compiled(self, request, *, operation_id=None):
        verify_installed_ccxt()
        check_compiled(request)
        operation = super().reserve(request.plan, operation_id=operation_id,
                                    client_order_id=request.client_order_id)
        # Two conservative commits: interruption here leaves the ID reserved and
        # PREPARED, with no binding and no way to authorize; reopening aborts it.
        # No replacement IDs or automatic retry are created.
        with self._transaction():
            self._connection.execute('INSERT INTO simulation_requests VALUES(?,?,?,?)',
                                     (operation.operation_id, request.canonical_request,
                                      request.request_fingerprint, request.profile))
        return operation

    def _bound(self, operation_id, request):
        check_compiled(request)
        operation = self._get(operation_id)
        row = self._connection.execute('SELECT * FROM simulation_requests WHERE operation_id=?',
                                       (operation_id,)).fetchone()
        if (row is None or row['request'] != request.canonical_request
                or row['request_fingerprint'] != request.request_fingerprint
                or fingerprint(row['request']) != row['request_fingerprint']
                or row['profile'] != request.profile
                or operation.client_order_id != request.client_order_id
                or operation.environment != request.plan.environment
                or operation.account_binding != request.plan.account_binding
                or operation.payload != canonical_payload(request.plan.payload)
                or operation.payload_fingerprint != request.plan.payload_fingerprint
                or operation.evidence_fingerprint != request.plan.evidence_fingerprint):
            raise JournalError('Simulation request binding mismatch or unavailable')
        return operation

    def bound_operation(self, operation_id, request):
        with self._transaction():
            return self._bound(operation_id, request)

    def authorize(self, operation_id, version, request, confirmation):
        self.bound_operation(operation_id, request)
        # Immutable binding and exclusive journal lock bridge this inspection to
        # the parent transaction. Parent retains expiry, version and stop checks.
        token = super().authorize(operation_id, version, request.plan.payload_fingerprint, confirmation)
        return SimulationAuthorization(token, request.request_fingerprint)

    def consume_authorization(self, authorization, request):
        if (type(authorization) is not SimulationAuthorization or authorization.simulation_only is not True
                or type(authorization.authorization) is not Authorization):
            raise JournalError('Only offline simulation authorization is accepted')
        self.bound_operation(authorization.authorization.operation_id, request)
        if authorization.request_fingerprint != request.request_fingerprint:
            raise JournalError('Simulation authorization request mismatch')
        return super().consume_authorization(authorization.authorization, request.plan.payload_fingerprint)


class Outcome(str, Enum):
    ACKNOWLEDGED = 'simulated_acknowledgment'
    REJECTED = 'simulated_rejection'
    PARTIAL_FILL = 'simulated_partial_fill_then_ioc_cancel'
    FULL_FILL = 'simulated_full_fill'
    TIMEOUT = 'simulated_timeout'
    SERVER_ERROR = 'simulated_http_5xx'
    UNCERTAIN = 'simulated_unknown_outcome'


@dataclass(frozen=True)
class SimulatedObservation:
    operation_id: str
    client_order_id: str
    request_fingerprint: str
    account_binding: str
    environment: str
    outcome: Outcome
    filled_quantity: Decimal
    exchange_order_id: str
    simulation_only: bool = True
    authenticated: bool = False


@dataclass(frozen=True)
class SimulationResult:
    operation_id: str
    state: State
    simulation_only: bool = True
    authenticated: bool = False
    submission_enabled: bool = False


def _journal(journal):
    # No transport/client argument or injectable response function is accepted.
    if type(journal) is not SimulationJournal:
        raise JournalError('Simulation requires its separately identified offline journal')


def _quantity(request, outcome, filled):
    quantity = Decimal(dict(request.plan.payload)['quantity'])
    if (type(filled) is not Decimal or not filled.is_finite() or filled < 0 or filled > quantity
            or len(filled.as_tuple().digits) > 40 or abs(filled.adjusted()) > 40):
        raise JournalError('Invalid simulated cumulative fill')
    if ((outcome is Outcome.FULL_FILL and filled != quantity)
            or (outcome is Outcome.REJECTED and filled != 0)
            or (outcome is Outcome.PARTIAL_FILL and not 0 < filled < quantity)):
        raise JournalError('Simulated outcome and cumulative fill disagree')
    # Exact rational grid check, independent of ambient Decimal precision.
    if (Fraction(filled) / Fraction(request.context[3].quantity_step)).denominator != 1:
        raise JournalError('Simulated fill is off the native quantity grid')


def simulate_submission(journal, authorization, request, outcome, *, partial_quantity=None):
    """One local transition sequence, NEVER an exchange submission or retry."""
    _journal(journal)
    check_compiled(request)
    if type(outcome) is not Outcome:
        raise JournalError('Unsupported simulated submission outcome')
    quantity = Decimal(dict(request.plan.payload)['quantity'])
    filled = quantity if outcome is Outcome.FULL_FILL else Decimal('0')
    if outcome is Outcome.PARTIAL_FILL:
        filled = partial_quantity
    elif partial_quantity is not None:
        raise JournalError('Partial quantity is only valid for a partial-fill simulation')
    _quantity(request, outcome, filled)
    operation = journal.consume_authorization(authorization, request)
    if outcome in (Outcome.TIMEOUT, Outcome.SERVER_ERROR, Outcome.UNCERTAIN):
        journal.mark_uncertain(operation.operation_id, operation.version)
    else:
        journal.acknowledge(operation.operation_id, operation.version, 'sim_' + request.client_order_id)
        if outcome is not Outcome.ACKNOWLEDGED:
            observation = SimulatedObservation(operation.operation_id, request.client_order_id,
                request.request_fingerprint, request.plan.account_binding, 'testnet', outcome, filled,
                'sim_' + request.client_order_id)
            return simulate_reconciliation(journal, request, observation)
    return SimulationResult(operation.operation_id, journal.get(operation.operation_id).state)


def simulate_reconciliation(journal, request, observation):
    """Invented observation may terminalize SIMU data only. Not exchange proof.

    Absence, timeout, 5xx, and an unknown result can never release a blocker.
    """
    _journal(journal)
    check_compiled(request)
    if (type(observation) is not SimulatedObservation or observation.simulation_only is not True
            or observation.authenticated is not False or observation.environment != 'testnet'
            or observation.account_binding != request.plan.account_binding
            or observation.client_order_id != request.client_order_id
            or observation.request_fingerprint != request.request_fingerprint
            or observation.exchange_order_id != 'sim_' + request.client_order_id
            or type(observation.outcome) is not Outcome
            or observation.outcome not in (Outcome.REJECTED, Outcome.PARTIAL_FILL, Outcome.FULL_FILL)):
        raise JournalError('Unverified or mismatched simulated reconciliation')
    operation = journal.bound_operation(observation.operation_id, request)
    _quantity(request, observation.outcome, observation.filled_quantity)
    if (operation.exchange_order_id is not None
            and operation.exchange_order_id != observation.exchange_order_id):
        raise JournalError('Simulated exchange identity mismatch')
    if operation.state not in (State.SUBMITTING, State.ACKNOWLEDGED, State.UNCERTAIN, State.RECONCILING):
        raise JournalError('Simulation operation is not reconcilable')
    if operation.state is not State.RECONCILING:
        journal.start_reconciliation(operation.operation_id, operation.version)
        operation = journal.get(operation.operation_id)
    target = {Outcome.REJECTED: State.REJECTED, Outcome.PARTIAL_FILL: State.CANCELED,
              Outcome.FULL_FILL: State.FILLED}[observation.outcome]
    journal.resolve(operation.operation_id, operation.version, target,
                    filled_quantity=observation.filled_quantity)
    return SimulationResult(operation.operation_id, journal.get(operation.operation_id).state)
