"""Serializers for the Club app."""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from apps.club.models.club import Club
from apps.player.models.player import Player
from apps.player.models.player_club_membership import PlayerClubMembership


class ClubSerializer(serializers.ModelSerializer):
    """Serializer for Club model."""

    logo_url = serializers.SerializerMethodField()

    class Meta:
        """Meta configuration."""

        model = Club
        fields: ClassVar[list[str]] = ["id_uuid", "name", "logo", "logo_url"]
        read_only_fields: ClassVar[list[str]] = ["id_uuid"]

    def get_logo_url(self, obj: Club) -> str | None:
        """Return the URL of the club logo.

        Returns:
            str | None: The URL of the club logo.

        """
        return obj.get_club_logo()


class ClubAdminPlayerSerializer(serializers.Serializer):
    """Small player representation for club admin tools."""

    id_uuid = serializers.UUIDField()
    username = serializers.CharField()

    def to_representation(self, instance: Player) -> dict[str, object]:  # type: ignore[override]
        """Return a minimal player payload for club admin tooling."""
        return {
            "id_uuid": str(instance.id_uuid),
            "username": instance.user.username,
        }


class ClubMembershipSerializer(serializers.Serializer):
    """Representation of a player's membership in a club."""

    id_uuid = serializers.UUIDField()
    player = ClubAdminPlayerSerializer()
    start_date = serializers.DateField()
    end_date = serializers.DateField(allow_null=True)

    def to_representation(self, instance: PlayerClubMembership) -> dict[str, object]:  # type: ignore[override]
        """Serialize a membership and nested player summary."""
        return {
            "id_uuid": str(instance.id_uuid),
            "player": ClubAdminPlayerSerializer().to_representation(instance.player),
            "start_date": instance.start_date.isoformat(),
            "end_date": instance.end_date.isoformat() if instance.end_date else None,
        }


class ClubAdminSettingsSerializer(serializers.Serializer):
    """Response serializer for the club admin settings screen."""

    club = ClubSerializer()
    admins = ClubAdminPlayerSerializer(many=True)
    members = ClubMembershipSerializer(many=True)


class ClubMembershipAddSerializer(serializers.Serializer):
    """Input serializer for adding a player to a club."""

    player_id = serializers.UUIDField(required=False)
    user_id = serializers.IntegerField(required=False)
    username = serializers.CharField(required=False)
    start_date = serializers.DateField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Ensure at least one identifier is provided.

        Raises:
            ValidationError: If no identifier (player_id/user_id/username) was given.

        """
        if not (
            attrs.get("player_id") or attrs.get("user_id") or attrs.get("username")
        ):
            raise serializers.ValidationError(
                "Provide one of: player_id, user_id, username."
            )
        return attrs
