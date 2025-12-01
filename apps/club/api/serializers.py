"""Serializers for the Club app."""

from typing import ClassVar

from rest_framework import serializers

from apps.club.models.club import Club


class ClubSerializer(serializers.ModelSerializer):
    """Serializer for Club model."""

    logo_url = serializers.SerializerMethodField()

    class Meta:
        """Meta configuration."""

        model = Club
        fields: ClassVar[list[str]] = ["id_uuid", "name", "logo", "logo_url"]
        read_only_fields: ClassVar[list[str]] = ["id_uuid"]

    def get_logo_url(self, obj: Club) -> str | None:  # noqa: PLR6301
        """Return the URL of the club logo.

        Returns:
            str | None: The URL of the club logo.

        """
        return obj.get_club_logo()
