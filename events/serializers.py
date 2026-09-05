from rest_framework import serializers

from .models import Event


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = Event
        fields = ['id', 'name', 'venue', 'start_time', 'capacity', 'seats_remaining']
        read_only_fields = ['seats_remaining']

    def create(self, validated_data):
        validated_data['seats_remaining'] = validated_data['capacity']
        return super().create(validated_data)
