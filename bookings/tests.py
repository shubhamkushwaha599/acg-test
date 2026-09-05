import threading

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from events.models import Event

from .models import Booking

User = get_user_model()


def make_event(capacity, seats_remaining=None):
    return Event.objects.create(
        name='Concert', venue='Main Hall',
        start_time=timezone.now() + timezone.timedelta(days=1),
        capacity=capacity,
        seats_remaining=capacity if seats_remaining is None else seats_remaining,
    )


def auth_header_for(user):
    token = RefreshToken.for_user(user)
    return f'Bearer {token.access_token}'


class BookingFlowTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='bob', password='pw12345')
        self.other_user = User.objects.create_user(username='carol', password='pw12345')
        self.event = make_event(capacity=5)

    def _auth(self, user=None):
        self.client.credentials(HTTP_AUTHORIZATION=auth_header_for(user or self.user))

    def test_book_requires_auth(self):
        response = self.client.post(f'/events/{self.event.id}/book/', {'seats': 1})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_book_success_decrements_seats_remaining(self):
        self._auth()
        response = self.client.post(f'/events/{self.event.id}/book/', {'seats': 2})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.event.refresh_from_db()
        self.assertEqual(self.event.seats_remaining, 3)
        self.assertEqual(Booking.objects.get(id=response.data['id']).status, Booking.CONFIRMED)

    def test_book_invalid_seats_rejected(self):
        self._auth()
        response = self.client.post(f'/events/{self.event.id}/book/', {'seats': 0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_book_unknown_event_404(self):
        self._auth()
        response = self.client.post('/events/999999/book/', {'seats': 1})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_book_more_than_available_is_waitlisted_cleanly(self):
        self._auth()
        response = self.client.post(f'/events/{self.event.id}/book/', {'seats': 6})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['status'], Booking.WAITLISTED)
        self.event.refresh_from_db()
        self.assertEqual(self.event.seats_remaining, 5)

    def test_waitlisted_booking_promoted_on_cancel(self):
        self._auth(self.user)
        full_booking = self.client.post(f'/events/{self.event.id}/book/', {'seats': 5}).data
        self.assertEqual(Event.objects.get(id=self.event.id).seats_remaining, 0)

        self._auth(self.other_user)
        waitlisted = self.client.post(f'/events/{self.event.id}/book/', {'seats': 2}).data
        self.assertEqual(waitlisted['status'], Booking.WAITLISTED)

        self._auth(self.user)
        cancel_response = self.client.post(f"/bookings/{full_booking['id']}/cancel/")
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)

        promoted = Booking.objects.get(id=waitlisted['id'])
        self.assertEqual(promoted.status, Booking.CONFIRMED)
        self.event.refresh_from_db()
        self.assertEqual(self.event.seats_remaining, 3)

    def test_waitlist_promotion_does_not_skip_ahead_in_fifo_order(self):
        self._auth(self.user)
        booking_a = self.client.post(f'/events/{self.event.id}/book/', {'seats': 3}).data
        booking_b = self.client.post(f'/events/{self.event.id}/book/', {'seats': 2}).data
        self.assertEqual(Event.objects.get(id=self.event.id).seats_remaining, 0)

        self._auth(self.other_user)
        big_waitlisted = self.client.post(f'/events/{self.event.id}/book/', {'seats': 3}).data
        small_waitlisted = self.client.post(f'/events/{self.event.id}/book/', {'seats': 1}).data

        self._auth(self.user)
        # Frees only 2 seats - not enough for the earlier 3-seat waitlisted
        # request, even though it *would* be enough for the later 1-seat one.
        self.client.post(f"/bookings/{booking_b['id']}/cancel/")

        self.assertEqual(Booking.objects.get(id=big_waitlisted['id']).status, Booking.WAITLISTED)
        self.assertEqual(Booking.objects.get(id=small_waitlisted['id']).status, Booking.WAITLISTED)
        self.event.refresh_from_db()
        self.assertEqual(self.event.seats_remaining, 2)

    def test_cancelling_a_waitlisted_booking_frees_no_seats(self):
        self._auth(self.user)
        self.client.post(f'/events/{self.event.id}/book/', {'seats': 5})

        self._auth(self.other_user)
        waitlisted = self.client.post(f'/events/{self.event.id}/book/', {'seats': 1}).data
        cancel_response = self.client.post(f"/bookings/{waitlisted['id']}/cancel/")

        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        self.event.refresh_from_db()
        self.assertEqual(self.event.seats_remaining, 0)

    def test_cancel_frees_seats(self):
        self._auth()
        book_response = self.client.post(f'/events/{self.event.id}/book/', {'seats': 3})
        booking_id = book_response.data['id']

        cancel_response = self.client.post(f'/bookings/{booking_id}/cancel/')
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)

        self.event.refresh_from_db()
        self.assertEqual(self.event.seats_remaining, 5)
        self.assertEqual(Booking.objects.get(id=booking_id).status, Booking.CANCELLED)

    def test_cancel_twice_rejected(self):
        self._auth()
        book_response = self.client.post(f'/events/{self.event.id}/book/', {'seats': 1})
        booking_id = book_response.data['id']
        self.client.post(f'/bookings/{booking_id}/cancel/')

        response = self.client.post(f'/bookings/{booking_id}/cancel/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancel_by_non_owner_forbidden(self):
        self._auth(self.user)
        book_response = self.client.post(f'/events/{self.event.id}/book/', {'seats': 1})
        booking_id = book_response.data['id']

        self._auth(self.other_user)
        response = self.client.post(f'/bookings/{booking_id}/cancel/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_bookings_only_returns_own(self):
        self._auth(self.user)
        self.client.post(f'/events/{self.event.id}/book/', {'seats': 1})

        self._auth(self.other_user)
        self.client.post(f'/events/{self.event.id}/book/', {'seats': 1})

        self._auth(self.user)
        response = self.client.get('/bookings/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['user'], self.user.id)


class OverbookingConcurrencyTests(APITransactionTestCase):
    """
    Proves the no-oversell guarantee: with an event that has only 3 seats
    left, 10 concurrent booking requests for 1 seat each must result in
    exactly 3 confirmed bookings and 7 cleanly waitlisted - never more seats
    booked than existed.
    """

    def test_concurrent_bookings_never_oversell(self):
        event = make_event(capacity=3)
        users = [User.objects.create_user(username=f'racer{i}', password='pw12345') for i in range(10)]
        tokens = [auth_header_for(u) for u in users]

        results = [None] * len(users)

        def worker(index, auth_header):
            connection.close()
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=auth_header)
            response = client.post(f'/events/{event.id}/book/', {'seats': 1})
            results[index] = response.status_code
            connection.close()

        threads = [
            threading.Thread(target=worker, args=(i, tokens[i]))
            for i in range(len(users))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        success_count = results.count(status.HTTP_201_CREATED)
        waitlisted_count = results.count(status.HTTP_202_ACCEPTED)

        self.assertEqual(success_count, 3)
        self.assertEqual(waitlisted_count, 7)

        event.refresh_from_db()
        self.assertEqual(event.seats_remaining, 0)
        self.assertEqual(
            Booking.objects.filter(event=event, status=Booking.CONFIRMED).count(), 3,
        )
        self.assertEqual(
            Booking.objects.filter(event=event, status=Booking.WAITLISTED).count(), 7,
        )
