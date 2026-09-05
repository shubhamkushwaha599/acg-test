from django.contrib import admin

from .models import Booking


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('id', 'event', 'user', 'seats', 'status', 'created_at')
    list_filter = ('status',)
