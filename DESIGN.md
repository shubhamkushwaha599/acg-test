# DESIGN.md

## Data model rationale

`Event` (`name`, `venue`, `start_time`, `capacity`) lives in its own `events` app; `Booking`
(`event` FK, `user` FK, `seats`, `status`, `created_at`) lives in a separate `bookings` app that
depends on `events`. Splitting them keeps each app's responsibility narrow and mirrors how the
URLs are already partitioned in the spec (`/events/...` vs `/bookings/...`).

`Event` carries a **denormalized `seats_remaining`** column alongside the immutable `capacity`,
rather than computing remaining seats on the fly by summing active `Booking.seats`. This is the
one non-obvious modelling choice, and it is deliberate: it turns "check capacity and reserve
seats" into a single atomic conditional `UPDATE` (below) instead of a read-then-write pair that
needs its own locking/isolation reasoning. The cost is a second source of truth that must be kept
in sync with `Booking` rows — mitigated by only ever mutating it inside the same
`transaction.atomic()` block that creates/cancels the booking, so the two are always consistent.

`Booking.status` is `CONFIRMED`/`CANCELLED` (no soft-delete) so cancelled bookings stay auditable.

## How overselling is prevented

The booking view does not read `seats_remaining`, check it in Python, then write — that pattern
has a race window between the read and the write. Instead, inside `transaction.atomic()`:

```python
updated = Event.objects.filter(
    id=event_id, seats_remaining__gte=seats,
).update(seats_remaining=F('seats_remaining') - seats)

if updated == 0:
    return 409  # someone else got there first, or event doesn't exist
Booking.objects.create(event_id=event_id, user=request.user, seats=seats, status='CONFIRMED')
```

The `WHERE seats_remaining >= N` and the decrement happen as **one SQL statement**, so the
check-and-reserve is atomic regardless of how many requests arrive at the same instant:

- **On MySQL/InnoDB**, the `UPDATE` takes a row lock on the matching `Event` row for the
  statement's duration. Two concurrent transactions racing for the last seat serialize on that
  row: whichever commits first wins, the second re-evaluates the `WHERE` against the now-updated
  value and legitimately finds `updated == 0` if seats ran out. Different events don't contend
  with each other at all — the lock is per-row, not table- or DB-wide.
- **On SQLite** (what this submission actually runs — see trade-offs below), there is no
  row-level locking, but SQLite serializes all writers on a single database-level lock, so the
  same single-statement `UPDATE` still can't interleave with another writer's `UPDATE`. The
  guarantee holds for the identical reason (one atomic statement, no read/write gap) even though
  the underlying locking granularity is coarser.

Cancellation mirrors this: mark the booking `CANCELLED`, then `F('seats_remaining') + seats`,
both in one transaction. `select_for_update()` was deliberately **not** used — Django's support
for it on SQLite is unsupported, and the conditional-`UPDATE` pattern above needed no explicit
locking on either backend, so it also means the exact same code path is what gets tested.

**Proof, not just an argument:** `bookings/tests.py::OverbookingConcurrencyTests` creates an
event with 3 seats left, fires 10 concurrent `book` requests for 1 seat each from separate
threads/DB connections, and asserts exactly 3 succeed (201 `CONFIRMED`), 7 are cleanly waitlisted
(202 `WAITLISTED` — see below), and the event ends at `seats_remaining == 0` with exactly 3
`CONFIRMED` and 7 `WAITLISTED` bookings in the DB — never more than 3 seats consumed.

## Trade-offs

- **SQLite instead of MySQL.** The task's fallback clause was used: Python 3.14 is very new and
  `mysqlclient` had no prebuilt Windows wheel available, which risked a lengthy C-build detour
  inside the 2h budget. Concurrency-wise, this trades MySQL's per-row InnoDB locking (many events
  can be booked concurrently, only same-event bookings contend) for SQLite's single
  writer-at-a-time database lock (booking on *any* event blocks booking on *every other* event
  for the duration of that one `UPDATE`). Correctness — no overselling — holds on both; only
  concurrent *throughput* differs. The models/queries have no SQLite-specific code, so switching
  `DATABASES` to MySQL is a config change, not a rewrite.
- **No `django-filter` dependency.** The event list only needed two date-range params and one
  boolean flag, so this was implemented as plain `get_queryset()` filtering to keep the dependency
  surface small.
- **No self-serve registration endpoint.** JWT auth (`simplejwt`) issues tokens for existing
  Django users; creating users is via `createsuperuser`/admin/ORM. Out of scope for the time box.
- **No separate "organizer" role.** Any authenticated user can create events (`IsAuthenticated`
  on `POST /events`); reads are public (`IsAuthenticatedOrReadOnly`). A real product would likely
  gate event creation behind a staff/organizer permission.
- **Waitlist (stretch goal, implemented).** When a booking request can't fit
  (`seats_remaining < requested`), instead of a flat rejection the request is stored as a
  `WAITLISTED` booking (`202 Accepted`) rather than rejected outright. This intentionally
  supersedes plain "reject cleanly if capacity is insufficient" for the fully-booked case, per the
  stretch goal's description of the desired behavior — a request only gets a hard `409` when the
  event doesn't exist or seats are invalid, never merely for being full. On cancellation of a
  `CONFIRMED` booking, `_promote_waitlist()` walks `WAITLISTED` bookings for that event oldest
  first and promotes each whose seat count fits in the freed capacity, using the exact same
  atomic conditional-`UPDATE` as a normal booking (so promotion is subject to the same no-oversell
  guarantee). Promotion **stops** at the first waitlisted request that doesn't fit rather than
  skipping ahead to a smaller, later one — strict FIFO over utilization. Cancelling a `WAITLISTED`
  booking (leaving the queue) frees no seats and doesn't trigger promotion, since it never held any.

## Scaling

- **Reads to 100×:** event list/detail are read-heavy and change infrequently relative to reads,
  so they cache well — a short-TTL cache (or a read replica) keyed by the filter params, invalidated
  on booking/cancel for that event, removes almost all read load from the primary DB. Add indexes
  on `start_time` and `seats_remaining` (already the only two columns filtered on). Pagination is
  already in place so list responses stay bounded regardless of table size.
- **Bookings to high concurrency:** the conditional-`UPDATE` pattern already scales horizontally
  across *different* events on MySQL — row locks don't contend across rows. The remaining
  bottleneck is many concurrent bookers on the *same* hot event, which serializes on that one row
  no matter what; the next step there would be a per-event queue (e.g. a lightweight worker or
  message queue serializing writes for that specific event) so clients get fast, ordered
  accept/reject decisions instead of contending on a DB row directly, plus read replicas so the
  booking write path isn't competing with list/detail read traffic on the same primary.

## AI usage note

This service was built with AI assistance (Claude, via Claude Code) for scaffolding boilerplate
(model/serializer/view/url wiring) and for drafting the concurrency test's threading harness. I
verified the concurrency claim empirically rather than trusting it by inspection: I ran
`OverbookingConcurrencyTests` repeatedly, confirmed the exact 3-succeed/7-reject split and the
final `seats_remaining == 0` invariant, and manually exercised the full flow end-to-end with
`curl` against a running dev server (create event → book → over-book rejected with 409 → list own
bookings → cancel → seats restored) before treating any of it as done. The one thing I had to
actually debug rather than accept as-is: the first version of the concurrency test failed with
sqlite3 `"database table is locked"` under Django's default in-memory shared-cache test database,
because `SQLITE_LOCKED` (shared-cache table lock) isn't retried by the busy-timeout the way
`SQLITE_BUSY` is — fixed by pointing the test database at a real file
(`DATABASES['default']['TEST']['NAME']`) so threads get OS-level file locking with proper
busy-timeout retries instead.
