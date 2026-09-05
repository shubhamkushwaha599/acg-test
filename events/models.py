from django.core.validators import MinValueValidator
from django.db import models


class Event(models.Model):
    name = models.CharField(max_length=255)
    venue = models.CharField(max_length=255)
    start_time = models.DateTimeField()
    capacity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    seats_remaining = models.PositiveIntegerField()

    class Meta:
        ordering = ['start_time']

    def __str__(self):
        return f'{self.name} @ {self.start_time:%Y-%m-%d %H:%M}'
