from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from events.models import Event


class Booking(models.Model):
    CONFIRMED = 'CONFIRMED'
    CANCELLED = 'CANCELLED'
    WAITLISTED = 'WAITLISTED'
    STATUS_CHOICES = [
        (CONFIRMED, 'Confirmed'),
        (CANCELLED, 'Cancelled'),
        (WAITLISTED, 'Waitlisted'),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='bookings')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bookings')
    seats = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=CONFIRMED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Booking#{self.id} {self.user} x{self.seats} ({self.status})'
