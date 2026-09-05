from rest_framework import serializers

from .models import Booking


class BookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = ['id', 'event', 'user', 'seats', 'status', 'created_at']
        read_only_fields = ['event', 'user', 'status', 'created_at']


class BookingCreateSerializer(serializers.Serializer):
    seats = serializers.IntegerField(min_value=1)
