from django.contrib import admin

from .models import ReplayLog, ReplayRequest


class ReplayLogInline(admin.TabularInline):
    model = ReplayLog
    extra = 0
    can_delete = False
    readonly_fields = [field.name for field in ReplayLog._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ReplayRequest)
class ReplayRequestAdmin(admin.ModelAdmin):
    list_display = ('request_id', 'source', 'amount', 'status', 'maker', 'checker', 'created_at')
    list_filter = ('source', 'status')
    search_fields = ('request_id',)
    inlines = [ReplayLogInline]
