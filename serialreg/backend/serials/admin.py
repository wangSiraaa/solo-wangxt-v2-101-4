from django.contrib import admin

from .models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item,
    SuccessionAudit, Title, TitleSuccession,
)

admin.site.register(Title)
admin.site.register(IssueNumber)
admin.site.register(Issue)
admin.site.register(IssueNumbering)
admin.site.register(Item)
admin.site.register(Binding)
admin.site.register(BindingEntry)


@admin.register(TitleSuccession)
class TitleSuccessionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "predecessor", "successor", "effective_month", "state",
        "predecessor_title_snapshot", "predecessor_issn_snapshot",
    )
    list_filter = ("state",)
    readonly_fields = [f.name for f in TitleSuccession._meta.fields]


@admin.register(SuccessionAudit)
class SuccessionAuditAdmin(admin.ModelAdmin):
    list_display = (
        "id", "action", "predecessor", "successor",
        "effective_month", "created_at",
    )
    list_filter = ("action",)
    readonly_fields = [f.name for f in SuccessionAudit._meta.fields]
