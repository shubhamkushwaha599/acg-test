# DESIGN.md

## Data model rationale

`Event` (`name`, `venue`, `start_time`, `capacity`) lives in its own `events` app; `Booking`
(`event` FK, `user` FK, `seats`, `confirmed_seats`, `cancelled_at`, `created_at`) lives in a
separate `bookings` app that depends on `events`. Splitting them keeps each app's responsibility
narrow and mirrors how the URLs are already partitioned in the spec (`/events/...` vs
`/bookings/...`).

`Event` carries a **denormalized `seats_remaining`** column alongside the immutable `capacity`,
rather than computing remaining seats on the fly by summing active `Booking.seats`. This is the
one non-obvious modelling choice, and it is deliberate: it turns "check capacity and reserve
seats" into a single atomic conditional `UPDATE` (below) instead of a read-then-write pair that
needs its own locking/isolation reasoning. The cost is a second source of truth that must be kept
in sync with `Booking` rows — mitigated by only ever mutating it inside the same
`transaction.atomic()` block that creates/cancels the booking, so the two are always consistent.

`Booking.status` (`CONFIRMED`/`PARTIAL`/`WAITLISTED`/`CANCELLED`) is **not a stored column** — it's
a read-only Python property derived from `seats`, `confirmed_seats`, and `cancelled_at`:
```python
@property
def status(self):
    if self.cancelled_at is not None:
        return 'CANCELLED'
    if self.confirmed_seats == 0:
        return 'WAITLISTED'
    if self.confirmed_seats == self.seats:
        return 'CONFIRMED'
    return 'PARTIAL'
```
Storing it as a separate enum column would just be a second source of truth to keep in sync every
time `confirmed_seats` changes (at booking time, at promotion time); deriving it removes that
entirely. `cancelled_at` is a nullable timestamp rather than a boolean/enum value so cancelled
bookings stay auditable (when, not just whether) without a soft-delete.

## How overselling is prevented

The booking view does not read `seats_remaining`, check it in Python, then write — that pattern
has a race window between the read and the write. Instead, inside `transaction.atomic()`:

```python
updated = Event.objects.filter(
    id=event_id, seats_remaining__gte=1,
).update(seats_remaining=F('seats_remaining') - 1)
```

The `WHERE seats_remaining >= 1` and the decrement happen as **one SQL statement**, so the
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

**Partial fulfillment, one `Booking` row per request:** a request for more seats than remain
doesn't waitlist the *whole* request, and doesn't fragment it into separate rows either — it
confirms as many seats as are available and records the shortfall on the *same* booking (e.g. 2
seats left, someone requests 4 → one `Booking` with `seats=4, confirmed_seats=2`, `status`
`"PARTIAL"`). This is done by calling the single-seat conditional decrement above up to `seats`
times inside one transaction (helper `_grab_seats`), counting how many succeed before the first
failure:
```python
def _grab_seats(event_id, count):
    gained = 0
    for _ in range(count):
        updated = Event.objects.filter(id=event_id, seats_remaining__gte=1).update(
            seats_remaining=F('seats_remaining') - 1)
        if updated == 0:
            break
        gained += 1
    return gained

confirmed_count = _grab_seats(event_id, seats)
booking = Booking.objects.create(
    event_id=event_id, user=request.user, seats=seats, confirmed_seats=confirmed_count)
```
A single bulk `UPDATE ... SET seats_remaining = seats_remaining - LEAST(seats_remaining, N)` would
be more efficient (one statement instead of up to N), but it doesn't tell Python how many seats
were actually granted — and there's no portable, race-free way to also read that count without
either `select_for_update()` (unsupported on SQLite) or a plain read that could go stale against a
concurrent transaction. Repeating the already-proven atomic primitive keeps the exact same
correctness argument instead of introducing a new one, at the cost of O(seats) statements — an
acceptable trade-off given booking requests are realistically single/low-double-digit seat counts.
The response is always a single booking object: `201` if `confirmed_count > 0` (fully or
partially granted), `202` if `confirmed_count == 0` (fully waitlisted).

Cancellation mirrors this: record `freed = booking.confirmed_seats`, set `cancelled_at = now()`,
then `F('seats_remaining') + freed`, all in one transaction, followed by waitlist promotion (see
below) if anything was actually freed. `select_for_update()` was deliberately **not** used —
Django's support for it on SQLite is unsupported, and the conditional-`UPDATE` pattern above
needed no explicit locking on either backend, so it also means the exact same code path is what
gets tested.

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
- **Waitlist (stretch goal, implemented).** When a booking request can't be fully satisfied
  (`seats_remaining < requested`), instead of a flat rejection it's partially or fully waitlisted
  on the *same* `Booking` row (`202 Accepted` if nothing was confirmed, `201 Created` if at least
  one seat was — see "Partial fulfillment" above). This intentionally supersedes plain "reject
  cleanly if capacity is insufficient" for the fully/partially-booked case, per the stretch goal's
  description of the desired behavior — a request only gets a hard `409`/`400` for an unknown
  event or invalid seat count, never merely for being full. On cancellation of a booking that held
  seats, `_promote_waitlist()` walks other active bookings for that event that still have a
  shortfall (`confirmed_seats < seats`), oldest first, and grants each as many of its outstanding
  seats as are available, using the exact same atomic conditional decrement as a normal booking (so
  promotion is subject to the same no-oversell guarantee). It **stops** as soon as one can't be
  fully closed — the oldest booking with a shortfall gets first claim on whatever capacity exists,
  even if that only partially fills it, and a later, smaller booking can never jump the queue ahead
  of it. Cancelling a fully-`WAITLISTED` booking (`confirmed_seats == 0`) frees no seats and
  doesn't trigger promotion, since it never held any.

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
