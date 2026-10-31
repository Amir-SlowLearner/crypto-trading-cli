"""Shared safe errors and the temporary increment-one mutation barrier."""


class SafetyError(ValueError):
    """An actionable error whose message contains no exchange response or secrets."""


def reject_mutation():
    # No flag or environment variable can bypass this barrier. Remove only when
    # account-mode discovery and durable reconciliation have been implemented.
    raise SafetyError(
        "Exchange mutations are disabled in this foundation increment. "
        "Use dry for a preview; account-mode verification and durable "
        "reconciliation are required before trading is enabled."
    )
