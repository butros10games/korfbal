from django.contrib import admin

from ..models import PageConnectRegistration


class PageConnectRegistrationAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "player", "page", "registration_date"]
    show_full_result_count = False

    class Meta:
        model = PageConnectRegistration


admin.site.register(PageConnectRegistration, PageConnectRegistrationAdmin)
