from datetime import datetime

from django.db import transaction
from rest_framework import serializers

from .models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item,
    SuccessionAudit, Title, TitleSuccession,
    correct_succession, create_succession,
    revoke_succession,
)


def _coerce_month(value):
    """前端 <input type=month> 传 'YYYY-MM' 或 'YYYY-MM-DD'，统一成当月 1 日。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().replace(day=1)
    text = str(value)
    try:
        if len(text) == 7:  # YYYY-MM
            return datetime.strptime(text, "%Y-%m").date()
        return datetime.strptime(text[:10], "%Y-%m-%d").date().replace(day=1)
    except ValueError:
        raise serializers.ValidationError("月份格式应为 YYYY-MM。")


class TitleSerializer(serializers.ModelSerializer):
    # 沿革摘要：始终只描述「书目层关系」，不携带任何馆藏数据
    current_successor = serializers.SerializerMethodField()
    current_predecessor = serializers.SerializerMethodField()

    class Meta:
        model = Title
        fields = [
            "id", "title", "issn", "publisher", "status",
            "ceased_month", "created_at",
            "current_successor", "current_predecessor",
        ]

    def _active_out(self, obj):
        cached = getattr(obj, "_active_out_cache", None)
        if cached is not None:
            return cached.get(obj.id)
        return obj.successions_out.filter(
            state=TitleSuccession.State.ACTIVE,
        ).select_related("successor").first()

    def get_current_successor(self, obj):
        row = self._active_out(obj)
        if not row:
            return None
        return {
            "succession_id": row.id,
            "title_id": row.successor_id,
            "title": row.successor.title,
            "issn": row.successor.issn,
            "effective_month": row.effective_month,
            "predecessor_title_snapshot": row.predecessor_title_snapshot,
            "predecessor_issn_snapshot": row.predecessor_issn_snapshot,
        }

    def get_current_predecessor(self, obj):
        cached = getattr(obj, "_active_in_cache", None)
        if cached is not None:
            return cached.get(obj.id)
        row = obj.successions_in.filter(
            state=TitleSuccession.State.ACTIVE,
        ).select_related("predecessor").first()
        if not row:
            return None
        return {
            "succession_id": row.id,
            "title_id": row.predecessor_id,
            "title": row.predecessor.title,
            "issn": row.predecessor.issn,
            "effective_month": row.effective_month,
            "predecessor_title_snapshot": row.predecessor_title_snapshot,
            "predecessor_issn_snapshot": row.predecessor_issn_snapshot,
        }

    def validate(self, attrs):
        status = attrs.get("status", getattr(self.instance, "status", None))
        ceased_month = attrs.get(
            "ceased_month", getattr(self.instance, "ceased_month", None),
        )
        if status == Title.PublicationStatus.CEASED and not ceased_month:
            raise serializers.ValidationError(
                {"ceased_month": "停刊刊名必须填写停刊月份。"},
            )
        return attrs


class SuccessionSerializer(serializers.ModelSerializer):
    """刊名沿革读模型：始终带出原刊名/原 ISSN 快照。"""

    predecessor_title = serializers.CharField(
        source="predecessor.title", read_only=True)
    predecessor_issn_now = serializers.CharField(
        source="predecessor.issn", read_only=True)
    successor_title = serializers.CharField(
        source="successor.title", read_only=True)
    successor_issn = serializers.CharField(
        source="successor.issn", read_only=True)
    superseded_by_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = TitleSuccession
        fields = [
            "id", "predecessor", "successor",
            "predecessor_title", "predecessor_issn_now",
            "predecessor_title_snapshot", "predecessor_issn_snapshot",
            "successor_title", "successor_issn",
            "effective_month", "state", "superseded_by_id",
            "revoked_at", "revoked_reason", "created_at",
        ]
        read_only_fields = fields


class SuccessionCreateSerializer(serializers.Serializer):
    predecessor = serializers.PrimaryKeyRelatedField(
        queryset=Title.objects.all())
    successor = serializers.PrimaryKeyRelatedField(
        queryset=Title.objects.all())
    effective_month = serializers.CharField(help_text="YYYY-MM")
    detail = serializers.CharField(required=False, allow_blank=True)

    def validate_effective_month(self, value):
        return _coerce_month(value)

    def validate(self, attrs):
        if attrs["predecessor"] == attrs["successor"]:
            raise serializers.ValidationError(
                {"successor": "前身刊与后继刊不能是同一刊名。"})
        return attrs

    def create(self, validated_data):
        return create_succession(
            predecessor=validated_data["predecessor"],
            successor=validated_data["successor"],
            effective_month=validated_data["effective_month"],
            detail=validated_data.get("detail", ""),
        )


class SuccessionCorrectSerializer(serializers.Serializer):
    successor = serializers.PrimaryKeyRelatedField(
        queryset=Title.objects.all(), required=False)
    effective_month = serializers.CharField(required=False)
    detail = serializers.CharField(required=False, allow_blank=True)

    def validate_effective_month(self, value):
        return _coerce_month(value)

    def save_row(self, row):
        return correct_succession(
            row,
            successor=self.validated_data.get("successor"),
            effective_month=self.validated_data.get("effective_month"),
            detail=self.validated_data.get("detail", ""),
        )


class SuccessionRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)

    def save_row(self, row):
        return revoke_succession(row, reason=self.validated_data.get("reason", ""))


class SuccessionAuditSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    predecessor_title_now = serializers.CharField(
        source="predecessor.title", read_only=True)
    successor_title_now = serializers.CharField(
        source="successor.title", read_only=True)

    class Meta:
        model = SuccessionAudit
        fields = [
            "id", "action", "action_label", "succession", "new_succession",
            "predecessor", "successor", "effective_month",
            "predecessor_title_snapshot", "predecessor_issn_snapshot",
            "predecessor_title_now", "successor_title_now",
            "detail", "created_at",
        ]


class IssueNumberSerializer(serializers.ModelSerializer):
    class Meta:
        model = IssueNumber
        fields = ["id", "title", "volume", "number", "sort_key"]


class IssueNumberingSerializer(serializers.ModelSerializer):
    number_id = serializers.IntegerField(source="number.id", read_only=True)
    volume = serializers.CharField(source="number.volume", read_only=True)
    number = serializers.CharField(source="number.number", read_only=True)
    label = serializers.CharField()

    class Meta:
        model = IssueNumbering
        fields = ["number_id", "volume", "number", "label"]


class IssueSerializer(serializers.ModelSerializer):
    """发行期。numbers 为期号 id 列表：普通期 1 个，合刊 ≥2 个。"""

    number_ids = serializers.PrimaryKeyRelatedField(
        queryset=IssueNumber.objects.all(),
        many=True, write_only=True, source="numbers",
    )
    numberings = IssueNumberingSerializer(many=True, read_only=True)

    class Meta:
        model = Issue
        fields = [
            "id", "title", "kind", "issue_month", "issue_month_end",
            "note", "number_ids", "numberings", "created_at",
        ]

    def validate(self, attrs):
        title = attrs.get("title", getattr(self.instance, "title", None))
        numbers = attrs.get("numbers")
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if numbers is not None:
            if len(numbers) < 1:
                raise serializers.ValidationError(
                    {"number_ids": "至少关联一个期号。"},
                )
            wrong = [n.id for n in numbers if n.title_id != title.id]
            if wrong:
                raise serializers.ValidationError(
                    {"number_ids": f"期号 {wrong} 不属于该刊。"},
                )
            if len({n.id for n in numbers}) != len(numbers):
                raise serializers.ValidationError(
                    {"number_ids": "期号不能重复。"},
                )
            if kind == Issue.IssueKind.COMBINED and len(numbers) < 2:
                raise serializers.ValidationError(
                    {"number_ids": "合刊必须关联至少两个期号。"},
                )
            if kind == Issue.IssueKind.REGULAR and len(numbers) != 1:
                raise serializers.ValidationError(
                    {"number_ids": "普通期只能关联一个期号。"},
                )
            # 一个编号槽位只能被发行一次
            qs = IssueNumbering.objects.filter(number__in=numbers)
            if self.instance:
                qs = qs.exclude(issue=self.instance)
            if qs.exists():
                used = list(
                    qs.values_list("number__volume", "number__number", "issue_id"),
                )
                raise serializers.ValidationError(
                    {"number_ids": f"期号已被其他发行期占用：{used}"},
                )
        start = attrs.get("issue_month", getattr(self.instance, "issue_month", None))
        end = attrs.get(
            "issue_month_end", getattr(self.instance, "issue_month_end", None),
        )
        if start and end and end < start:
            raise serializers.ValidationError(
                {"issue_month_end": "截止年月不能早于起始年月。"},
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        numbers = validated_data.pop("numbers")
        issue = Issue.objects.create(**validated_data)
        labels = self._labels(issue, numbers)
        IssueNumbering.objects.bulk_create([
            IssueNumbering(issue=issue, number=n, label=labels.get(n.id, ""))
            for n in numbers
        ])
        return issue

    def _labels(self, issue, numbers):
        raw = self.initial_data.get("labels") or {}
        labels = {}
        if isinstance(raw, dict):
            for n in numbers:
                labels[n.id] = str(raw.get(str(n.id), raw.get(n.id, "")))
        if not any(labels.values()):
            joined = "-".join(n.number for n in numbers)
            labels = {n.id: f"no.{joined}" if len(numbers) > 1 else "" for n in numbers}
        return labels


class ItemSerializer(serializers.ModelSerializer):
    """入藏实物。current_location 在装订后取装订册位置。"""

    current_location = serializers.SerializerMethodField()
    bound = serializers.SerializerMethodField()
    binding_call_number = serializers.SerializerMethodField()
    number_ids = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            "id", "barcode", "title", "issue", "location", "status",
            "accessioned_at", "current_location", "bound",
            "binding_call_number", "number_ids",
        ]
        read_only_fields: list = []

    def validate_status(self, value):
        # status 可经 PATCH 修改（报失/找回）；bound 只能由装订/拆订流程设置
        if value == Item.ItemStatus.BOUND:
            raise serializers.ValidationError(
                "已装订状态只能通过装订/拆订操作变更。",
            )
        return value

    def get_current_location(self, obj):
        return obj.current_location()

    def get_bound(self, obj):
        return obj.is_bound

    def get_binding_call_number(self, obj):
        return obj.binding_entry.binding.call_number if obj.is_bound else None

    def get_number_ids(self, obj):
        return list(obj.issue.numbers.values_list("id", flat=True))

    def validate(self, attrs):
        title = attrs.get("title", getattr(self.instance, "title", None))
        issue = attrs.get("issue", getattr(self.instance, "issue", None))
        if issue and title and issue.title_id != title.id:
            raise serializers.ValidationError("实物所属刊与发行期不一致。")
        return attrs


class BindingSerializer(serializers.ModelSerializer):
    item_ids = serializers.PrimaryKeyRelatedField(
        queryset=Item.objects.all(), many=True, write_only=True,
    )
    items = serializers.SerializerMethodField()

    class Meta:
        model = Binding
        fields = [
            "id", "call_number", "title", "location", "bound_month",
            "item_ids", "items", "created_at",
        ]

    def get_items(self, obj):
        return [
            {
                "barcode": e.item.barcode,
                "previous_location": e.previous_location,
                "current_location": obj.location,
            }
            for e in obj.entries.select_related("item")
        ]

    def validate_item_ids(self, items):
        if not items:
            raise serializers.ValidationError("装订册至少包含一个实物。")
        # 合刊实物会同时挂在多个期号下，前端可能重复勾选：按主键去重
        deduped = list({it.id: it for it in items}.values())
        already = [it.barcode for it in deduped if it.is_bound]
        if already:
            raise serializers.ValidationError(
                f"实物已在装订册中：{already}，请先拆订。",
            )
        return deduped

    def validate(self, attrs):
        title = attrs["title"]
        bad = [it.barcode for it in attrs["item_ids"] if it.title_id != title.id]
        if bad:
            raise serializers.ValidationError(
                {"item_ids": f"以下实物不属于该刊，不能混装：{bad}"},
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop("item_ids")
        binding = Binding.objects.create(**validated_data)
        BindingEntry.objects.bulk_create([
            BindingEntry(
                item=it, binding=binding, previous_location=it.location or "",
            )
            for it in items
        ])
        Item.objects.filter(id__in=[it.id for it in items]).update(
            status=Item.ItemStatus.BOUND,
        )
        return binding


class UnbindSerializer(serializers.Serializer):
    """拆订：恢复每个实物装订前的位置与在馆状态。"""

    binding_id = serializers.PrimaryKeyRelatedField(
        queryset=Binding.objects.all(),
    )

    @transaction.atomic
    def save(self, **kwargs):
        binding = self.validated_data["binding_id"]
        entries = list(binding.entries.select_related("item"))
        for e in entries:
            e.item.location = e.previous_location
            e.item.status = Item.ItemStatus.AVAILABLE
            e.item.save(update_fields=["location", "status"])
        BindingEntry.objects.filter(binding=binding).delete()
        binding.delete()
        return [e.item for e in entries]
