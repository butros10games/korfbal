"""Local presentation for Django and shared authentication model admins."""

from bg_auth.admin.user_profile_admin import PasskeyCredentialAdmin, UserProfileAdmin
from bg_auth.models import PasskeyCredential, UserProfile
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.kwt_common.admin_filters import relation_filter


class KorfbalUserAdmin(KorfbalModelAdmin, UserAdmin):
    """Retain Django's password and permissions forms with bounded list defaults."""

    search_help_text = "Search username, name or email."
    list_filter = (
        "is_staff",
        "is_superuser",
        "is_active",
        relation_filter("groups", "Group"),
    )


class KorfbalGroupAdmin(GroupAdmin, KorfbalModelAdmin):
    """Preserve group permission management and searchable group labels."""

    search_help_text = "Search group name."


class KorfbalUserProfileAdmin(KorfbalModelAdmin, UserProfileAdmin):
    """Show authentication state with user search and no secret material in forms."""

    list_display = ("user", "email_2fa", "totp_enabled", "trusted_device_set_at")
    search_fields = ("id_uuid", "user__username", "user__email")
    list_filter = ("email_2fa", "totp_enabled")
    list_select_related = ("user",)
    exclude = (
        "totp_secret",
        "jwt_two_factor_challenge_hash",
        "trusted_device_token_hash",
    )


class KorfbalPasskeyAdmin(KorfbalModelAdmin, PasskeyCredentialAdmin):
    """Keep the shared key-material exclusions while making metadata searchable."""

    search_fields = ("id_uuid", "name", "user__username", "user__email")
    list_filter = ("device_type", "backed_up", "last_used_at")
    list_select_related = ("user",)


def configure_auth_admins() -> None:
    """Register local subclasses after shared apps have registered their defaults."""
    for model, screen in (
        (get_user_model(), KorfbalUserAdmin),
        (Group, KorfbalGroupAdmin),
        (UserProfile, KorfbalUserProfileAdmin),
        (PasskeyCredential, KorfbalPasskeyAdmin),
    ):
        admin.site.unregister(model)
        admin.site.register(model, screen)
