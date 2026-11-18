# Book — multi-industry booking + deposits

Appointment booking with Paystack deposits for salons, classes, tables,
pitches, rooms — anything that sells a time slot and loses money to
no-shows.

Correctness lives in the database, not application code:

- a PostgreSQL **exclusion constraint** makes overlapping bookings for the
  same resource impossible (including class blockers and buffers);
- a **CHECK constraint** makes overselling a class impossible;
- a **partial unique index** makes duplicate ledger entries and duplicate
  webhook processing impossible.

The parallel test suites prove these under real concurrency instead of
asserting them.

## Stack

Django 5.2 (LTS) · Django REST Framework · PostgreSQL 16 via psycopg 3 ·
Celery + Redis · Paystack (deposits, refunds, later Transfers payouts)

Python 3.13 is what the project was built and tested on.

## Setup

```sh
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Create `.env` in the repo root (it is git-ignored) with:

```dotenv
DJANGO_SECRET_KEY=replace-me
DJANGO_DEBUG=1
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DATABASE_URL=postgres://book_user:your-password@localhost:5432/book_db
PAYSTACK_SECRET_KEY=sk_test_...
# Optional; enables the newer timestamped signature scheme (whsec_...).
PAYSTACK_WEBHOOK_SECRET=
# Celery broker. Only needed when running worker/beat.
REDIS_URL=redis://localhost:6379/0
```

`DATABASE_URL` is enough for the app to boot; `PAYSTACK_SECRET_KEY` is
required before a deposit can be charged. `config/settings/base.py` loads
`.env` with `os.environ.setdefault`, so real environment variables always
win over the file — production never reads the file by accident.

```sh
.venv\Scripts\python.exe manage.py migrate
.venv\Scripts\python.exe -m pytest
```

Your database user needs `CREATEDB` so pytest can create its throwaway
test database (`ALTER ROLE book_user CREATEDB;` once, as a superuser).
CI (`.github/workflows/ci.yml`) runs the same suite against a real
PostgreSQL 16 service.

### Running the app

```sh
.venv\Scripts\python.exe manage.py runserver          # API + admin
.venv\Scripts\python.exe -m celery -A config worker -l info   # tasks
.venv\Scripts\python.exe -m celery -A config beat -l info     # schedule
```

`config/settings/prod.py` has no `DATABASE_URL` fallback on purpose: a
production boot without a database URL must fail loudly. Point
`DJANGO_SETTINGS_MODULE` at `config.settings.prod` there.

## API

JWT-authenticated unless stated otherwise. Obtain a token with
`POST /api/auth/token/` (`username` + `password`), refresh at
`/api/auth/token/refresh/`.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/services/<id>/availability/?date=YYYY-MM-DD[&resource=<id>]` | Advisory slots (one-to-one) or sessions with `seats_left` (group). `date` is interpreted in the business timezone. |
| GET | `/api/bookings/[?status=...]` | The caller's bookings only; blocker rows are never exposed. |
| POST | `/api/bookings/` | Creates an unpaid hold + deposit payment. Body: `service`, `start_at` (one-to-one), `session` (group), `resource`, `party_size`, `address`, `notes`. Returns the booking, `authorization_url` and the payment reference. |
| POST | `/api/bookings/<uuid>/cancel/` | Cancels the caller's booking and reports `refund_status` / `forfeited`. |
| POST | `/api/webhooks/paystack/` | **Unauthenticated**; verified by signature. |

Status codes are consistent: `400` bad request or broken business rule,
`404` not found / not yours, `409` slot or session conflict or invalid
state transition, `502` the payment provider failed to start a charge.

Availability is **advisory**. It mirrors working hours, time off, buffers
and live holds, but the database constraint is the only enforcement
point — a slot can disappear between listing and booking, and that
race is answered with `409`.

Throttles: `booking_create` 10/min per user (POST only — listing is not
throttled), `webhook` 300/min per IP.

## Payments, ledger and webhooks

- Money is **integer kobo** everywhere. No floats.
- One-to-one: `price_kobo` is the whole booking. Group: it is **per
  person**, and so is `deposit_kobo_override`; totals scale with
  `party_size`.
- A computed deposit of `0` confirms the booking immediately with no
  Paystack round trip and no `Payment` row.
- The platform holds deposits. `LedgerEntry` is append-only, one row per
  `(payment, entry_type)`:

  | entry | sign | meaning |
  | --- | --- | --- |
  | `deposit_received` | `+D` | money arrived, held by the platform |
  | `refund` | `-D` | refund completed at Paystack |
  | `deposit_forfeited` | `0` | audit marker: deposit now belongs to the business |
  | `payout` | `-X` | Paystack Transfer to the business (future) |

  A business's available balance is `SUM(amount_kobo)` — computed, never
  stored.

Webhook pipeline (`bookings/webhooks.py`):

1. Parse JSON for the event key only.
2. **Store the event first** — the unique `(provider, event_key)` row is
   the dedup gate.
3. Verify the signature over the **raw bytes**. Invalid signatures are
   stored for audit but never processed.
4. Process inline under `SELECT ... FOR UPDATE` on the event row, so two
   simultaneous deliveries serialize.

Supported signatures:

- legacy `x-paystack-signature` =
  `HMAC-SHA512(raw_body, PAYSTACK_SECRET_KEY)`;
- new `Paystack-Signature: t=<unix>,v1=<kid>:<hex>` =
  `HMAC-SHA512(f"{t}." + raw_body, PAYSTACK_WEBHOOK_SECRET)`, with a
  300-second replay window.

Late payments are handled explicitly: if a hold expired but the slot is
still free, the booking is **restored**; if someone else took it, the
deposit is refunded automatically. Refund rows (`Payment(kind=refund)`)
are created in the same transaction and sent after commit, so no HTTP
call ever runs while row locks are held. A refund stuck in `initiated`
after a provider failure is safe to retry — its reference is unique.

## Lifecycle

`bookings/lifecycle.py` owns every transition:

- customer cancels with at least `Business.free_cancellation_hours` notice
  → full deposit refund;
- late customer cancellation → deposit forfeited (zero-kobo ledger
  marker; the original credit stays, because the money is now the
  business's);
- business cancels → refund regardless of timing;
- no-show (only after `start_at`) → forfeit + group seats released;
- completed (only after `end_at`) → no money movement.

Cancelling a class uses `cancel_session`, which cancels the blocker and
every active customer booking, releases all seats and refunds paid
deposits exactly once.

`bookings/tasks.py` runs `expire_overdue_holds` every 60 s via Celery
beat: it expires lapsed unpaid holds and releases group seats. It locks
`session → booking` (the same global order as every other writer), skips
rows locked by a live transaction (`FOR UPDATE SKIP LOCKED`), and only
releases seats on the transition out of `pending_payment`, so re-running
is harmless.

`create_hold` additionally performs a per-resource sweep inside its own
transaction, which closes the window where a just-expired hold would
still block the exclusion index.

## Where things live

| File | Responsibility |
| --- | --- |
| `bookings/models.py` | Schema + hard guarantees (exclusion constraint, seat CHECK, partial uniques), derived booking fields. |
| `bookings/money.py` | Pure integer-kobo pricing; the only place deposit math exists. |
| `bookings/availability.py` | Read-only advisory slots/sessions, business-timezone day math, buffer-aware busy intervals. |
| `bookings/holds.py` | The only way customer bookings are created: validate, sweep, insert, then request the deposit after commit. |
| `bookings/sessions.py` | Creates a `ClassSession` and its exclusive blocker `Booking` in one transaction. |
| `bookings/payments.py` / `bookings/paystack.py` | Payment-row creation and the stdlib Paystack client (no DB, no models). |
| `bookings/webhooks.py` | Signature verification, store-first dedup, charge/refund handlers. |
| `bookings/ledger.py` / `bookings/refunds.py` | Append-only money truth and the refund state machine. |
| `bookings/lifecycle.py` | Cancellation, forfeiture, no-show, completion, session cancellation. |
| `bookings/tasks.py` | Global expiry sweep (Celery beat). |
| `bookings/api.py` / `bookings/serializers.py` | Function-based DRF views and shape-only serializers over the services. |
| `bookings/throttles.py` | Fixed-scope throttle classes (function-based views cannot carry `throttle_scope`). |
| `bookings/constraints.py` | One place that reads PostgreSQL constraint names from wrapped `IntegrityError`s. |

## Tests

`.venv\Scripts\python.exe -m pytest` runs everything against a real
PostgreSQL test database. Highlights:

- `test_parallel_booking.py` — 8 threads race one slot; exactly one row
  survives.
- `test_parallel_seats.py` — 8 threads grab seats; exactly `capacity`
  succeed.
- `test_schema_guards.py` — pins the extension, exclusion constraint and
  partial unique indexes after migrations.
- `test_webhooks.py` — both signature schemes, replay window, dedup,
  exactly-once ledger credit, late-payment restore/refund.
- `test_tasks.py` — expiry, seat release idempotency, and `SKIP LOCKED`
  (fails if the sweep ever waits on a locked row).

## Known limitations / next steps

- Group sessions are created manually (admin/shell); there is no
  recurring-schedule model or owner-facing session API yet.
- No-show/completion are service functions without HTTP endpoints in v1;
  run them from the admin shell. Do not flip `Booking.status` by hand —
  it bypasses seat release and ledger markers. Cancel a class through the
  admin's **“Cancel sessions”** action (or `lifecycle.cancel_session`);
  `is_cancelled` and `seats_taken` are read-only in admin for that
  reason.
- Payouts (`payout` ledger type, Paystack Transfers, subaccounts) are
  modelled but not implemented.
- A refund left in `initiated` after a provider outage needs a
  reconciliation run; the row and its unique reference are ready for a
  retry.
