from django.contrib import admin

from ..models import GroupType


class GroupTypeAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "name"]
    show_full_result_count = False

    class Meta:
        model = GroupType


admin.site.register(GroupType, GroupTypeAdmin)
