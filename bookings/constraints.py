"""PostgreSQL constraint introspection.

Django wraps psycopg errors in IntegrityError; the psycopg cause carries
a `diag` with the constraint name. Centralizing that unwrapping means
every service translates the same database verdict the same way, and new
callers (sessions, webhooks) do not copy subtle error-handling code.
"""


def violated_constraint(exc: BaseException) -> str | None:
    """Name of the constraint a wrapped IntegrityError violated, if any."""
    cause = exc.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None


def is_exclusion_violation(exc: BaseException) -> bool:
    """True only for the booking/resource overlap constraint.

    Any other IntegrityError must surface untouched — swallowing them
    would hide real bugs behind a friendly "slot taken".
    """
    return violated_constraint(exc) == "no_overlapping_active_bookings"
