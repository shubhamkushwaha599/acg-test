from rest_framework import serializers

from .models import Booking


class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = ['id', 'event', 'user', 'seats', 'confirmed_seats', 'status', 'created_at']
        read_only_fields = ['event', 'user', 'confirmed_seats', 'created_at']


class BookingCreateSerializer(serializers.Serializer):
    seats = serializers.IntegerField(min_value=1)
