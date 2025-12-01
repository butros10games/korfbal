"""Serializers for hub API endpoints."""

from __future__ import annotations

from rest_framework import serializers


class UpdateSerializer(serializers.Serializer):
    """Serializer for lightweight update feed entries."""

    id = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    timestamp = serializers.DateTimeField()
