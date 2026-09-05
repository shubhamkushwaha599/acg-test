from django.utils.dateparse import parse_datetime
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticatedOrReadOnly

from .models import Event
from .serializers import EventSerializer


def _parse_datetime_param(name, value):
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValidationError({name: f'"{value}" is not a valid ISO-8601 datetime.'})
    return parsed


class EventListCreateView(generics.ListCreateAPIView):
    serializer_class = EventSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = Event.objects.all()
        start_after = self.request.query_params.get('start_after')
        start_before = self.request.query_params.get('start_before')
        available = self.request.query_params.get('available')

        if start_after:
            qs = qs.filter(start_time__gte=_parse_datetime_param('start_after', start_after))
        if start_before:
            qs = qs.filter(start_time__lte=_parse_datetime_param('start_before', start_before))
        if available is not None and available.lower() in ('1', 'true', 'yes'):
            qs = qs.filter(seats_remaining__gt=0)

        return qs


class EventDetailView(generics.RetrieveAPIView):
    queryset = Event.objects.all()
    serializer_class = EventSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
