"""Shared base classes for Korfbal REST endpoints."""

from rest_framework import serializers
from rest_framework.views import APIView


class KorfbalAPIResponseSerializer(serializers.Serializer):
    """Fallback object contract for dynamic API responses."""


class KorfbalAPIView(APIView):
    """APIView with an explicit fallback OpenAPI response contract.

    Subclasses should set ``serializer_class`` when an endpoint maps to a
    concrete serializer. Dynamic aggregation endpoints inherit the generic
    object contract instead of being omitted from the generated schema.
    """

    serializer_class = KorfbalAPIResponseSerializer
