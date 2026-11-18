"""Guard tests for the database-level guarantees.

These pin the STRUCTURAL side: after migrating, the extension, the
exclusion constraint and the dedup/constraint backstops must exist. The
behavioral side — N threads racing one slot, overselling attempts — comes
with the hold service and the parallel test suites.
"""
import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


def _constraint_exists(name, contype):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND contype = %s",
            [name, contype],
        )
        return cursor.fetchone() is not None


def _index_exists(name):
    """For conditional UniqueConstraints: Django emits a partial unique
    index, which lives in pg_indexes, not pg_constraint."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", [name])
        return cursor.fetchone() is not None


def test_btree_gist_extension_installed():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
        assert cursor.fetchone() is not None


def test_exclusion_constraint_on_booking():
    # 'x' = exclusion constraint in pg_constraint.contype
    assert _constraint_exists("no_overlapping_active_bookings", "x")


def test_seat_oversell_check_on_class_session():
    assert _constraint_exists("session_not_oversold", "c")


def test_webhook_dedup_unique_constraint():
    assert _constraint_exists("uniq_webhook_event", "u")


def test_ledger_idempotency_constraint():
    # Conditional UniqueConstraint -> partial unique index.
    assert _index_exists("uniq_ledger_entry_per_payment_type")
