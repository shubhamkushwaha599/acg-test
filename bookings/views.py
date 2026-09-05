from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from events.models import Event

from .models import Booking
from .serializers import BookingCancelSerializer, BookingCreateSerializer, BookingSerializer


def _grab_seats(event_id, count):
    """
    Atomically grab up to `count` seats, one at a time, returning how many
    were actually obtained. Each iteration is the same atomic conditional
    decrement used everywhere in this service, so it carries the same
    no-oversell guarantee under concurrency - this just repeats it to find
    out exactly how many seats were available, up to `count`.
    """
    gained = 0
    for _ in range(count):
        updated = Event.objects.filter(
            id=event_id, seats_remaining__gte=1,
        ).update(seats_remaining=F('seats_remaining') - 1)
        if updated == 0:
            break
        gained += 1
    return gained


def _promote_waitlist(event_id):
    """
    FIFO promotion: walk active bookings that still have a shortfall
    (confirmed_seats < seats), oldest first, and grant each one as many of
    its outstanding seats as are available. Stops as soon as one can't be
    fully closed, so a later, smaller booking can't jump ahead of an
    earlier, larger one - it can only benefit from capacity already left
    over after the front of the queue takes what it can.
    Must be called from inside the same transaction as the seat-freeing
    update so the capacity check sees the freed seats.
    """
    candidates = Booking.objects.filter(
        event_id=event_id, cancelled_at__isnull=True,
    ).exclude(confirmed_seats=F('seats')).order_by('created_at')

    for booking in candidates:
        shortfall = booking.seats - booking.confirmed_seats
        gained = _grab_seats(event_id, shortfall)
        if gained:
            booking.confirmed_seats += gained
            booking.save(update_fields=['confirmed_seats'])
        if gained < shortfall:
            break


class BookEventView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, event_id):
        get_object_or_404(Event, pk=event_id)

        input_serializer = BookingCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        seats = input_serializer.validated_data['seats']

        with transaction.atomic():
            confirmed_count = _grab_seats(event_id, seats)
            booking = Booking.objects.create(
                event_id=event_id, user=request.user, seats=seats,
                confirmed_seats=confirmed_count,
            )

        response_status = status.HTTP_201_CREATED if confirmed_count > 0 else status.HTTP_202_ACCEPTED
        return Response(BookingSerializer(booking).data, status=response_status)


class BookingListView(generics.ListAPIView):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Booking.objects.filter(user=self.request.user)


class BookingCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        booking = get_object_or_404(Booking, pk=pk)

        if booking.user_id != request.user.id:
            return Response(
                {'detail': 'You do not have permission to cancel this booking.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if booking.cancelled_at is not None:
            return Response(
                {'detail': 'This booking is already cancelled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        input_serializer = BookingCancelSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        cancel_seats = input_serializer.validated_data.get('seats', booking.seats)

        if cancel_seats > booking.seats:
            return Response(
                {'detail': f'This booking only holds {booking.seats} seat(s), cannot cancel {cancel_seats}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            # Give up the still-waitlisted portion of the request first (it
            # isn't holding any real capacity yet), and only dip into the
            # confirmed portion once the waitlisted shortfall is exhausted.
            # Cancelling exactly a booking's shortfall turns a PARTIAL
            # booking into a clean CONFIRMED one for what's left.
            shortfall = booking.seats - booking.confirmed_seats
            from_waitlisted = min(cancel_seats, shortfall)
            from_confirmed = cancel_seats - from_waitlisted

            booking.seats -= cancel_seats
            booking.confirmed_seats -= from_confirmed
            if booking.seats == 0:
                booking.cancelled_at = timezone.now()
            booking.save(update_fields=['seats', 'confirmed_seats', 'cancelled_at'])

            if from_confirmed > 0:
                Event.objects.filter(id=booking.event_id).update(
                    seats_remaining=F('seats_remaining') + from_confirmed,
                )
                _promote_waitlist(booking.event_id)

        return Response(BookingSerializer(booking).data, status=status.HTTP_200_OK)
