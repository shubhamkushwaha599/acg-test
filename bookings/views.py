from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from events.models import Event

from .models import Booking
from .serializers import BookingCreateSerializer, BookingSerializer


def _promote_waitlist(event_id):
    """
    FIFO promotion: walk waitlisted bookings oldest-first and promote each
    one whose seat count now fits, stopping at the first one that doesn't
    (so a later, smaller request can't jump ahead of an earlier, larger one).
    Must be called from inside the same transaction as the seat-freeing
    update so the capacity check below sees the freed seats.
    """
    for waitlisted in Booking.objects.filter(
        event_id=event_id, status=Booking.WAITLISTED,
    ).order_by('created_at'):
        updated = Event.objects.filter(
            id=event_id, seats_remaining__gte=waitlisted.seats,
        ).update(seats_remaining=F('seats_remaining') - waitlisted.seats)
        if updated == 0:
            break
        waitlisted.status = Booking.CONFIRMED
        waitlisted.save(update_fields=['status'])


class BookEventView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, event_id):
        get_object_or_404(Event, pk=event_id)

        input_serializer = BookingCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        seats = input_serializer.validated_data['seats']

        with transaction.atomic():
            updated = Event.objects.filter(
                id=event_id, seats_remaining__gte=seats,
            ).update(seats_remaining=F('seats_remaining') - seats)

            if updated == 0:
                booking = Booking.objects.create(
                    event_id=event_id, user=request.user, seats=seats, status=Booking.WAITLISTED,
                )
                return Response(BookingSerializer(booking).data, status=status.HTTP_202_ACCEPTED)

            booking = Booking.objects.create(
                event_id=event_id, user=request.user, seats=seats, status=Booking.CONFIRMED,
            )

        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)


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

        if booking.status == Booking.CANCELLED:
            return Response(
                {'detail': 'This booking is already cancelled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            was_confirmed = booking.status == Booking.CONFIRMED
            booking.status = Booking.CANCELLED
            booking.save(update_fields=['status'])

            if was_confirmed:
                # Only a CONFIRMED booking was actually holding seats; a
                # WAITLISTED one never reserved capacity, so cancelling it
                # frees nothing and shouldn't trigger promotion.
                Event.objects.filter(id=booking.event_id).update(
                    seats_remaining=F('seats_remaining') + booking.seats,
                )
                _promote_waitlist(booking.event_id)

        return Response(BookingSerializer(booking).data, status=status.HTTP_200_OK)
