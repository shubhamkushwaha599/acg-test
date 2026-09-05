# DESIGN.md

## Data model rationale

`Event` (`name`, `venue`, `start_time`, `capacity`) lives in its own `events` app; `Booking`
(`event` FK, `user` FK, `seats`, `confirmed_seats`, `cancelled_at`, `created_at`) lives in a
separate `bookings` app that depends on it — mirrors the URL split in the spec (`/events/...` vs
`/bookings/...`).

`Event` carries a **denormalized `seats_remaining`** alongside the immutable `capacity`, rather
than computing remaining seats by summing active bookings. This is the key modelling choice: it
turns "check capacity and reserve" into a single atomic conditional `UPDATE` (below) instead of a
read-then-write pair with its own locking/isolation reasoning. The cost — a second source of
truth — is mitigated by only ever mutating it inside the same `transaction.atomic()` block that
creates/cancels the booking.

`Booking.status` (`CONFIRMED`/`PARTIAL`/`WAITLISTED`/`CANCELLED`) is a derived property, not a
stored column, so it can never drift from the two counters it's computed from:
```python
@property
def status(self):
    if self.cancelled_at is not None: return 'CANCELLED'
    if self.confirmed_seats == 0: return 'WAITLISTED'
    if self.confirmed_seats == self.seats: return 'CONFIRMED'
    return 'PARTIAL'
```

## How overselling is prevented

The booking view never reads `seats_remaining`, checks it in Python, then writes — that has a
race window between read and write. Instead, every seat is claimed with one atomic statement:

```python
updated = Event.objects.filter(id=event_id, seats_remaining__gte=1) \
                        .update(seats_remaining=F('seats_remaining') - 1)
```

The `WHERE` and the decrement happen as **one SQL statement**, so the check-and-reserve can't be
interleaved by a concurrent request: on **MySQL/InnoDB** the `UPDATE` row-locks the matching
`Event` for its duration, so two requests racing for the last seat serialize on that row (per-row,
so different events never contend); on **SQLite** (what this submission runs) there's no
row-level lock, but SQLite serializes *all* writers on one database-level lock, so the same
single-statement `UPDATE` still can't interleave — safe for the same reason, just not concurrent
across events.

**Partial fulfillment** is built on the exact same primitive, repeated: a helper (`_grab_seats`)
calls that one-seat decrement in a loop, up to the number requested, and stops at the first
failure — so a request for more seats than remain confirms as many as are available and records
the rest as a shortfall on the *same* `Booking` row (`seats=4, confirmed_seats=2`,
`status="PARTIAL"`) rather than rejecting outright or creating a second row. **Cancellation**
mirrors this in reverse, and supports partial cancellation too (`{"seats": N}`, optional): it gives
up a booking's still-waitlisted seats first (they hold no real capacity) before dipping into
confirmed ones, only freeing `seats_remaining` (and running waitlist promotion) for whatever was
actually confirmed. Promotion (`_promote_waitlist`) walks other bookings with a shortfall, oldest
first, granting each as much of the freed capacity as it can via the same primitive, and stops as
soon as one can't be fully closed — so a later, smaller request can never jump the queue.

A bulk `UPDATE ... - LEAST(seats_remaining, N)` would be more efficient than the per-seat loop,
but can't portably tell Python how many seats it actually granted without an extra locked read
(`select_for_update()`, unsupported on SQLite). Repeating the proven primitive keeps one
correctness argument instead of two, at the cost of O(seats) statements — acceptable given
realistic booking sizes.

**Proof, not argument:** `bookings/tests.py::OverbookingConcurrencyTests` fires 10 concurrent
1-seat booking requests (separate threads/DB connections) at an event with only 3 seats left, and
asserts exactly 3 succeed, 7 are cleanly waitlisted, and `seats_remaining` ends at exactly `0`.

## Trade-offs

- **SQLite, not MySQL.** Python 3.14 is new enough that `mysqlclient` had no prebuilt Windows
  wheel, risking a build detour inside the time box (task's stated fallback). Correctness holds on
  both; only concurrent *throughput* differs (SQLite serializes across *all* events, MySQL only
  per-row). No SQLite-specific code exists, so switching is a settings change (see README).
- **`Booking.seats` tracks the currently-active total, not the original request** — supporting
  partial cancellation by shrinking it directly avoids a 4th counter, at the cost of losing the
  original requested count once partially cancelled.
- **No `django-filter`, no self-serve registration, no organizer role** — each is a small,
  deliberate scope cut for the time box, not an oversight.
- **Waitlist (stretch goal, implemented)**, described above, intentionally replaces plain
  rejection with waitlisting/partial-fulfillment whenever an event is full — a hard `409`/`400`
  only occurs for an unknown event or invalid seat count.

## Scaling

- **Reads to 100×:** event list/detail change far less often than they're read — a short-TTL
  cache or read replica keyed on the filter params (invalidated on booking/cancel) removes most
  read load from the primary; add indexes on `start_time` and `seats_remaining`. Pagination is
  already in place.
- **Bookings to high concurrency:** the conditional-`UPDATE` pattern already scales across
  *different* events on MySQL (row locks don't cross rows); the remaining bottleneck is many
  bookers on the *same* hot event, which serializes on that one row regardless. Next step there:
  a per-event queue/worker to serialize writes for that event without contending on the DB row
  directly, plus read replicas to keep booking writes off the same primary as list/detail reads.

## AI usage note

Built with AI assistance (Claude, via Claude Code) end to end, iterating through three revisions
of the booking/cancellation logic as requirements were refined. Nothing was accepted by
inspection: the concurrency test was re-run after every revision to confirm the same 3-succeed/
7-waitlisted split and `seats_remaining == 0` invariant, and every behavior change was exercised
live against a running server with `curl`/Postman, checking actual response bodies (e.g. that a
booking's `id` stays the same across a `PARTIAL → CONFIRMED` promotion). One real bug was found
and fixed, not worked around: the concurrency test initially failed with sqlite3 `"database table
is locked"` under Django's in-memory shared-cache test DB, because `SQLITE_LOCKED` isn't retried
by the busy-timeout the way `SQLITE_BUSY` is — fixed by pointing the test DB at a real file.
