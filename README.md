# Booking Service

A small Django + Django REST Framework booking service: create events with a fixed seat
capacity, book seats against them with JWT auth, and cancel bookings — with a guarantee that
concurrent bookings can never oversell an event's capacity. Includes a waitlist: bookings that
can't be fully seated are automatically (and partially) confirmed later as seats free up.

See [DESIGN.md](DESIGN.md) for the data-model rationale, exactly how overselling is prevented,
trade-offs, and scaling notes.

## Requirements

- Python 3.10+ (built against 3.14)
- SQLite (bundled with Python — no separate server needed). See [Switching to MySQL](#switching-to-mysql)
  below if you want to run against MySQL instead.

## Setup

```powershell
# from the project root, with your venv already created and activated
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser   # interactive - pick any username/password
python manage.py runserver
```

The API is now served at `http://127.0.0.1:8000/`.

## Authentication

Auth is JWT (`djangorestframework-simplejwt`). Get a token for the user you just created:

```powershell
curl -Method POST http://127.0.0.1:8000/api/token/ `
  -ContentType "application/json" `
  -Body '{"username": "<your-username>", "password": "<your-password>"}'
```

(PowerShell's `curl` is an alias for `Invoke-WebRequest`, which uses `-Method`/`-ContentType`/
`-Body` rather than curl's usual `-X`/`-H`/`-d` flags. If you have real `curl.exe` on your `PATH`,
use `curl.exe` explicitly instead and the standard `-X POST -H ... -d '...'` flags work as usual.)

This returns `{"access": "...", "refresh": "..."}`. Send the access token on every authenticated
request:

```
Authorization: Bearer <access token>
```

Refresh it with `POST /api/token/refresh/` and `{"refresh": "<refresh token>"}` once it expires.

There's no self-serve registration endpoint — create users via `createsuperuser`, the Django
admin (`/admin/`), or the ORM. This is a deliberate scope cut (see DESIGN.md).

## API reference

| Method | URL | Auth | Body | Notes |
|---|---|---|---|---|
| POST | `/api/token/` | No | `{"username", "password"}` | Returns `{"access", "refresh"}` |
| POST | `/api/token/refresh/` | No | `{"refresh"}` | Returns a new `{"access"}` |
| POST | `/events/` | Yes | `{"name", "venue", "start_time", "capacity"}` | `201`; `seats_remaining` auto-set to `capacity`. `400` if `capacity < 1` |
| GET | `/events/` | No | — | Paginated. Query params: `start_after`, `start_before` (ISO-8601 datetimes, filter on `start_time`), `available=true` (only events with `seats_remaining > 0`) |
| GET | `/events/{id}/` | No | — | `200` with `capacity` + `seats_remaining`; `404` if unknown |
| POST | `/events/{id}/book/` | Yes | `{"seats": N}` | See [Booking behavior](#booking-behavior) below |
| GET | `/bookings/` | Yes | — | Paginated list of **your own** bookings only |
| POST | `/bookings/{id}/cancel/` | Yes | `{"seats": N}` *(optional)* | See [Cancelling](#cancelling) below |

Base URL: `http://127.0.0.1:8000`. A ready-to-import Postman collection covering every case below
is at [booking-service.postman_collection.json](booking-service.postman_collection.json).

### Booking behavior

A `Booking` has `seats` (how many are currently being requested/held), `confirmed_seats` (how many
of those are actually seated right now), and a computed `status`:

| `status` | Meaning |
|---|---|
| `CONFIRMED` | `confirmed_seats == seats` — fully seated |
| `PARTIAL` | `0 < confirmed_seats < seats` — some seated, the rest waitlisted |
| `WAITLISTED` | `confirmed_seats == 0` — none seated yet |
| `CANCELLED` | the booking (or what remained of it) has been cancelled |

`POST /events/{id}/book/` always creates exactly **one** `Booking` row, whatever the outcome:

- Enough seats available → `201 Created`, `status: "CONFIRMED"`.
- Some but not all available → `201 Created`, `status: "PARTIAL"` (e.g. 2 seats left, you request
  4 → `seats: 4, confirmed_seats: 2`).
- None available → `202 Accepted`, `status: "WAITLISTED"`.
- Invalid `seats` (≤ 0) → `400`. Unknown event → `404`.

When someone else's booking is later cancelled and frees up capacity, waitlisted/partial bookings
for that event are promoted automatically, oldest first — on the *same* booking `id`, never a new
one. See DESIGN.md for exactly how promotion order works.

### Cancelling

`POST /bookings/{id}/cancel/` cancels a booking. The body is optional:

- **No body** (or omit `seats`) → cancels the booking's entire current `seats` count.
- **`{"seats": N}`** → cancels only `N` seats from this booking. The *waitlisted* portion is given
  up first (it wasn't holding real capacity), and only dips into the *confirmed* portion once that
  runs out — so cancelling exactly a `PARTIAL` booking's shortfall turns it into a clean
  `CONFIRMED` booking for what's left, without freeing anything back to the event.
- Cancelling more seats than the booking currently holds → `400`.
- Cancelling someone else's booking → `403`. Cancelling an already-fully-cancelled booking → `400`.

## Running tests

```powershell
python manage.py test events bookings
```

27 tests, including `bookings.tests.OverbookingConcurrencyTests` — fires 10 concurrent booking
requests at an event with 3 seats left from separate threads/DB connections and asserts exactly 3
succeed and the event never goes negative. This is the test that proves the no-oversell guarantee
described in DESIGN.md, not just documents it.

## Switching to MySQL

This submission runs on SQLite for setup speed (see DESIGN.md for why). To run it against MySQL
instead, no application code changes are needed — only `config/settings.py`:

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": "booking_service",
        "USER": "...",
        "PASSWORD": "...",
        "HOST": "localhost",
        "PORT": "3306",
    }
}
```

Install a MySQL driver (`mysqlclient` or `PyMySQL`), then re-run `python manage.py migrate`. Drop
the SQLite-specific `OPTIONS`/`TEST` keys under `DATABASES["default"]` — they only apply to SQLite.

## Project layout

```
config/     Django project settings, root URLconf, JWT token endpoints
events/     Event model, serializer, views (list/create/detail + filtering), tests
bookings/   Booking model, serializer, views (book/cancel/list), waitlist logic, tests
            (imports Event from events.models — bookings depends on events, not the reverse)
```
