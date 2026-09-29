"""Serializers for hub API endpoints."""

from __future__ import annotations

from rest_framework import serializers


class UpdateSerializer(serializers.Serializer):
    """Serializer for lightweight update feed entries."""

    id = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    timestamp = serializers.DateTimeField()


CONTACT_TOPICS: tuple[tuple[str, str], ...] = (
    ("account", "Account en inloggen"),
    ("notifications", "Meldingen"),
    ("tracking", "Wedstrijden bijhouden"),
    ("display", "Club-tv en casten"),
    ("bug", "Een probleem melden"),
    ("privacy", "Gegevens en privacy"),
    ("other", "Iets anders"),
)
CONTACT_TOPIC_LABELS = dict(CONTACT_TOPICS)
CONTACT_MESSAGE_MIN_LENGTH = 10
CONTACT_MESSAGE_MAX_LENGTH = 4000


class ContactContextSerializer(serializers.Serializer):
    """Optional diagnostics the client attaches to a contact request."""

    platform = serializers.ChoiceField(
        choices=("web", "ios", "android"),
        required=False,
    )
    app_version = serializers.CharField(max_length=64, required=False, allow_blank=True)
    page = serializers.CharField(max_length=512, required=False, allow_blank=True)
    user_agent = serializers.CharField(
        max_length=512,
        required=False,
        allow_blank=True,
    )


class ContactRequestSerializer(serializers.Serializer):
    """A message from a visitor or signed-in player to the KorfConnect team."""

    topic = serializers.ChoiceField(choices=CONTACT_TOPICS)
    message = serializers.CharField(
        min_length=CONTACT_MESSAGE_MIN_LENGTH,
        max_length=CONTACT_MESSAGE_MAX_LENGTH,
        trim_whitespace=True,
    )
    name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    context = ContactContextSerializer(required=False)
