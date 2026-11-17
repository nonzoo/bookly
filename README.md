# Book — multi-industry booking + deposits

Appointment booking with Paystack deposits for salons, classes, tables,
pitches, rooms — anything that sells a time slot and loses money to
no-shows.

Correctness lives in the database, not application code: a PostgreSQL
exclusion constraint makes overlapping bookings for the same resource
impossible, a CHECK constraint makes overselling a class impossible, and
unique constraints make duplicate webhook processing and double ledger
credits impossible. The parallel-booking and seat-oversell test suites
prove these instead of asserting them.

## Stack

Django 5.2 (LTS) · Django REST Framework · PostgreSQL via psycopg 3 ·
Celery + Redis · Paystack (deposits, refunds, Transfers payouts)

## Setup

```sh
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy .env.example .env   # fill in DATABASE_URL + Paystack credentials
.venv\Scripts\python.exe manage.py migrate
.venv\Scripts\python.exe -m pytest
```

Note: your database user needs the `CREATEDB` privilege so pytest can
create its throwaway test database (grant once as a superuser:
`ALTER ROLE your_user CREATEDB;`). CI's postgres user already has it.

CI (`.github/workflows/ci.yml`) runs the same suite against a real
PostgreSQL 16 service.

## Status

Scaffold, schema with DB-level guarantees, schema guard tests, CI.
Next: availability service, hold service, parallel booking tests.
