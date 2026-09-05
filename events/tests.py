from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Event

User = get_user_model()


class EventListFilterTests(APITestCase):
    def setUp(self):
        now = timezone.now()
        self.past_full = Event.objects.create(
            name='Past Full', venue='Hall A', start_time=now - timezone.timedelta(days=1),
            capacity=10, seats_remaining=0,
        )
        self.future_available = Event.objects.create(
            name='Future Available', venue='Hall B', start_time=now + timezone.timedelta(days=1),
            capacity=10, seats_remaining=5,
        )
        self.future_full = Event.objects.create(
            name='Future Full', venue='Hall C', start_time=now + timezone.timedelta(days=2),
            capacity=10, seats_remaining=0,
        )

    def test_list_events_unauthenticated_allowed(self):
        response = self.client.get('/events/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 3)

    def test_filter_available_only(self):
        response = self.client.get('/events/', {'available': 'true'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {e['name'] for e in response.data['results']}
        self.assertEqual(names, {'Future Available'})

    def test_filter_date_range(self):
        now = timezone.now()
        response = self.client.get('/events/', {
            'start_after': now.isoformat(),
        })
        names = {e['name'] for e in response.data['results']}
        self.assertEqual(names, {'Future Available', 'Future Full'})

    def test_filter_invalid_date_returns_400(self):
        response = self.client.get('/events/', {'start_after': 'not-a-date'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_event_detail_includes_seats_remaining(self):
        response = self.client.get(f'/events/{self.future_available.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['seats_remaining'], 5)
        self.assertEqual(response.data['capacity'], 10)

    def test_event_detail_not_found(self):
        response = self.client.get('/events/999999/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class EventCreateTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345')

    def test_create_event_requires_auth(self):
        response = self.client.post('/events/', {
            'name': 'New Event', 'venue': 'Hall A',
            'start_time': timezone.now().isoformat(), 'capacity': 10,
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_event_sets_seats_remaining_to_capacity(self):
        self.client.force_authenticate(self.user)
        response = self.client.post('/events/', {
            'name': 'New Event', 'venue': 'Hall A',
            'start_time': timezone.now().isoformat(), 'capacity': 10,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['seats_remaining'], 10)

    def test_create_event_invalid_capacity_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.post('/events/', {
            'name': 'Bad Event', 'venue': 'Hall A',
            'start_time': timezone.now().isoformat(), 'capacity': 0,
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
