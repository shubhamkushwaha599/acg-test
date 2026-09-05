from django.contrib import admin

from .models import Booking


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('id', 'event', 'user', 'seats', 'confirmed_seats', 'status', 'created_at')
    list_filter = ('cancelled_at',)
