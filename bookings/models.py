from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from events.models import Event


class Booking(models.Model):
    CONFIRMED = 'CONFIRMED'
    CANCELLED = 'CANCELLED'
    WAITLISTED = 'WAITLISTED'
    PARTIAL = 'PARTIAL'

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='bookings')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bookings')
    seats = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    confirmed_seats = models.PositiveIntegerField(default=0)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def status(self):
        if self.cancelled_at is not None:
            return self.CANCELLED
        if self.confirmed_seats == 0:
            return self.WAITLISTED
        if self.confirmed_seats == self.seats:
            return self.CONFIRMED
        return self.PARTIAL

    def __str__(self):
        return f'Booking#{self.id} {self.user} x{self.seats} ({self.status})'
