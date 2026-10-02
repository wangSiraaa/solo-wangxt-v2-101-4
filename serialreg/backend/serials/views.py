from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import Prefetch, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    Binding, Issue, IssueNumber, IssueNumbering, Item, Title,
    TitleSuccession, TitleSuccessionEvent,
    lineage_chain_ids, lineage_context, locate_number,
    number_holding_status, replay_succession_events,
)
from .serializers import (
    BindingSerializer, IssueSerializer, ItemSerializer,
    IssueNumberSerializer, SuccessionCreateSerializer, SuccessionEventSerializer,
    SuccessionRevokeSerializer, SuccessionSerializer, TitleSerializer,
    UnbindSerializer,
)

TRUTHY = {"1", "true", "yes", "on"}


def _want_lineage(request):
    """include_history / lineage：包含前身/后继视图；缺省=仅当前刊名。"""
    return request.query_params.get("include_lineage", "").lower() in TRUTHY or \
        request.query_params.get("lineage", "").lower() in TRUTHY


def build_slots(title):
    """单个刊名的时间轴槽位（编号 × 发行 × 实物）。"""
    numbers = (
        IssueNumber.objects.filter(title=title)
        .prefetch_related(
            Prefetch(
                "issues",
                queryset=Issue.objects.prefetch_related(
                    Prefetch(
                        "numberings",
                        queryset=IssueNumbering.objects.select_related("number"),
                    ),
                    Prefetch(
                        "items",
                        queryset=Item.objects.select_related(
                            "binding_entry__binding",
                        ),
                    ),
                ),
            ),
        )
        .order_by("sort_key", "id")
    )
    slots = []
    for n in numbers:
        issues = list(n.issues.all())
        slots.append({
            "number_id": n.id,
            "volume": n.volume,
            "number": n.number,
            "holding_status": number_holding_status(title, n),
            "issues": [
                {
                    "issue_id": iss.id,
                    "kind": iss.kind,
                    "issue_month": iss.issue_month,
                    "issue_month_end": iss.issue_month_end,
                    "label": "·".join(
                        f"{nn.number.volume}({nn.number.number})"
                        for nn in iss.numberings.all()
                    ),
                    "combined_numbers": [
                        {"volume": nn.number.volume, "number": nn.number.number}
                        for nn in iss.numberings.all()
                    ],
                    "items": [
                        {
                            "item_id": it.id,
                            "barcode": it.barcode,
                            "status": it.status,
                            "location": it.current_location(),
                            "bound": it.is_bound,
                            "binding": it.binding_entry.binding.call_number
                            if it.is_bound else None,
                        }
                        for it in iss.items.all()
                    ],
                }
                for iss in issues
            ],
        })
    return slots


def timeline_groups(anchor, include_lineage):
    """构造时间轴分组：仅当前刊名 → [anchor]；包含沿革 → 全链从早到晚。"""
    if include_lineage:
        ids = lineage_chain_ids(anchor.id)
        context = {c["title_id"]: c for c in lineage_context(anchor.id)}
        titles = list(Title.objects.filter(id__in=ids))
        titles.sort(key=lambda t: ids.index(t.id))
    else:
        titles = [anchor]
        context = {anchor.id: {"role": "self", "effective_from": None}}
    groups = []
    for t in titles:
        groups.append({
            "title_id": t.id,
            "title": TitleSerializer(t).data,
            "lineage_role": context[t.id]["role"],
            "effective_from": context[t.id]["effective_from"],
            "slots": build_slots(t),
        })
    return groups


class TitleViewSet(viewsets.ModelViewSet):
    queryset = Title.objects.all()
    serializer_class = TitleSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["lineage"] = getattr(self, "_lineage_context", {})
        return ctx

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        keyword = request.query_params.get("search", "").strip()
        if keyword:
            qs = qs.filter(
                Q(title__icontains=keyword)
                | Q(issn__icontains=keyword)
                | Q(publisher__icontains=keyword)
            )
        anchors = list(qs)

        include_lineage = _want_lineage(request)
        if not include_lineage:
            # 仅当前刊名视图：各自独立，不携带任何沿革信息
            page = self.paginate_queryset(anchors)
            data = self.get_serializer(page or anchors, many=True).data
            if page is not None:
                return self.get_paginated_response(data)
            return Response(data)

        # 包含前身/后继视图：把每个命中间的沿革链展开，链上各刊名连续展示，
        # 徽标相对命中刊名（前身/当前/后继）。多个命中链相交时去重。
        ordered, seen, context = [], set(), {}
        for anchor in anchors:
            chain = lineage_chain_ids(anchor.id)
            titles = list(Title.objects.filter(id__in=chain))
            titles.sort(key=lambda t: chain.index(t.id))
            ctx = {c["title_id"]: c for c in lineage_context(anchor.id)}
            for t in titles:
                if t.id in seen:
                    continue
                seen.add(t.id)
                ordered.append(t)
                context.setdefault(t.id, ctx[t.id])
        self._lineage_context = context
        data = self.get_serializer(ordered, many=True).data
        return Response(data)


class IssueNumberViewSet(viewsets.ModelViewSet):
    queryset = IssueNumber.objects.all()
    serializer_class = IssueNumberSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class IssueViewSet(viewsets.ModelViewSet):
    queryset = Issue.objects.prefetch_related(
        "numberings__number", "items",
    ).select_related("title")
    serializer_class = IssueSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class ItemViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.select_related(
        "title", "issue", "binding_entry__binding",
    ).prefetch_related("issue__numbers")
    serializer_class = ItemSerializer

    @action(detail=False, methods=["get"])
    def locate(self, request):
        """按 (title, volume, number) 或 barcode 定位实物。

        合刊的任一期号都必须能找到同一实物；装订后返回装订册位置。
        include_lineage=1 时，期号检索沿刊名沿革链连续定位，
        但每个命中仍带所属刊名，不混成同一本刊物。
        """
        title_id = request.query_params.get("title")
        volume = request.query_params.get("volume", "")
        number = request.query_params.get("number")
        barcode = request.query_params.get("barcode")
        include_lineage = _want_lineage(request)

        if barcode:
            items = self.get_queryset().filter(barcode=barcode)
            result = []
            for it in items:
                result.append({
                    "barcode": it.barcode,
                    "title_id": it.title_id,
                    "title": it.title.title,
                    "issn": it.title.issn,
                    "issue_id": it.issue_id,
                    "numbers": [
                        {"volume": n.volume, "number": n.number}
                        for n in it.issue.numbers.all()
                    ],
                    "location": it.current_location(),
                    "bound": it.is_bound,
                    "binding": it.binding_entry.binding.call_number
                    if it.is_bound else None,
                    "status": it.status,
                })
            body = {"query": {"barcode": barcode}, "matches": result}
            if include_lineage and result:
                body["lineage"] = [
                    {
                        "title_id": c["title_id"],
                        "role": c["role"],
                        "effective_from": c["effective_from"],
                    }
                    for c in lineage_context(result[0]["title_id"])
                ]
            return Response(body)

        if not (title_id and number):
            return Response(
                {"detail": "需要提供 barcode，或同时提供 title 与 number。"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if include_lineage:
            return self._locate_lineage(title_id, volume, number)

        qs = IssueNumber.objects.filter(
            title_id=title_id, number=number,
        )
        if volume != "":
            qs = qs.filter(volume=volume)
        try:
            issue_number = qs.get()
        except IssueNumber.DoesNotExist:
            # 编号本身未登记：区别于「已登记但无发行」的缺号
            return Response({
                "detail": "该卷期编号未在馆藏系统登记。",
                "holding_status": "unregistered",
                "matches": [],
            }, status=status.HTTP_404_NOT_FOUND)
        except IssueNumber.MultipleObjectsReturned:
            return Response(
                {"detail": "卷/期定位到多条编号，请补全卷号。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        matches = locate_number(issue_number)
        # 缺号（无发行记录）是正常业务状态，返回 200，不自动等同缺藏
        return Response({
            "query": {"title": title_id, "volume": volume, "number": number},
            "include_lineage": False,
            "holding_status": number_holding_status(issue_number.title, issue_number),
            "matches": matches,
        })

    def _locate_lineage(self, anchor_id, volume, number):
        """沿沿革链定位：前身与后继的同号分别成组，保留各自刊名归属。"""
        chain_ids = lineage_chain_ids(anchor_id)
        context = {c["title_id"]: c for c in lineage_context(anchor_id)}
        groups = []
        anchor_status = "unregistered"
        anchor_matches = []
        for tid in chain_ids:
            qs = IssueNumber.objects.filter(title_id=tid, number=number)
            if volume != "":
                qs = qs.filter(volume=volume)
            try:
                issue_number = qs.get()
            except IssueNumber.DoesNotExist:
                continue
            except IssueNumber.MultipleObjectsReturned:
                return Response(
                    {"detail": f"刊名 {tid} 下卷/期定位到多条编号，请补全卷号。"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            t = issue_number.title
            matches = locate_number(issue_number)
            hs = number_holding_status(t, issue_number)
            if tid == int(anchor_id):
                anchor_status, anchor_matches = hs, matches
            groups.append({
                "title_id": tid,
                "title": t.title,
                "issn": t.issn,
                "lineage_role": context[tid]["role"],
                "effective_from": context[tid]["effective_from"],
                "volume": issue_number.volume,
                "number": issue_number.number,
                "holding_status": hs,
                "matches": matches,
            })
        if not groups:
            return Response({
                "detail": "该卷期编号在沿革链上的任一刊名均未登记。",
                "holding_status": "unregistered",
                "matches": [],
                "groups": [],
            }, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "query": {"title": anchor_id, "volume": volume, "number": number},
            "include_lineage": True,
            # 顶层字段仍对应当前刊名，旧客户端行为不变
            "holding_status": anchor_status
            if any(g["title_id"] == int(anchor_id) for g in groups)
            else groups[-1]["holding_status"],
            "matches": anchor_matches
            if any(g["title_id"] == int(anchor_id) for g in groups)
            else groups[-1]["matches"],
            "groups": groups,
        })


class BindingViewSet(viewsets.ModelViewSet):
    queryset = Binding.objects.prefetch_related(
        "entries__item",
    ).select_related("title")
    serializer_class = BindingSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs

    @action(detail=False, methods=["post"])
    def unbind(self, request):
        serializer = UnbindSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.save()
        return Response({
            "detail": "拆订完成，各实物已恢复原位置。",
            "restored": [
                {"barcode": it.barcode, "location": it.location,
                 "status": it.status}
                for it in items
            ],
        })


class SuccessionViewSet(viewsets.ModelViewSet):
    """刊名沿革：版本化关系 + 审计；禁止 PUT/PATCH/DELETE 覆盖旧记录。"""

    queryset = TitleSuccession.objects.select_related(
        "predecessor", "successor",
    ).prefetch_related("events")
    serializer_class = SuccessionSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        active = self.request.query_params.get("active")
        if active is not None:
            if active.lower() in TRUTHY:
                qs = qs.filter(revoked_at__isnull=True)
            elif active.lower() in {"0", "false"}:
                qs = qs.filter(revoked_at__isnull=False)
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(
                Q(predecessor_id=title_id) | Q(successor_id=title_id),
            )
        return qs

    def create(self, request, *args, **kwargs):
        serializer = SuccessionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            edge = serializer.save()
        except DjangoValidationError as exc:
            return Response(
                {"detail": getattr(exc, "messages", [str(exc)])},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except IntegrityError:
            return Response(
                {"detail": "与现有生效沿革冲突（矛盾后继/前身或重复关系），已拒绝。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(SuccessionSerializer(edge).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def revoke(self, request):
        serializer = SuccessionRevokeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            edge = serializer.save()
        except DjangoValidationError as exc:
            return Response(
                {"detail": getattr(exc, "messages", [str(exc)])},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(SuccessionSerializer(edge).data)

    @action(detail=False, methods=["get"])
    def audit(self, request):
        """审计事件流：默认全量；可按 ?title= 或 ?succession= 过滤。"""
        qs = TitleSuccessionEvent.objects.select_related(
            "succession",
        ).order_by("id")
        title_id = request.query_params.get("title")
        if title_id:
            qs = qs.filter(
                Q(succession__predecessor_id=title_id)
                | Q(succession__successor_id=title_id),
            )
        succession_id = request.query_params.get("succession")
        if succession_id:
            qs = qs.filter(succession_id=succession_id)
        return Response(SuccessionEventSerializer(qs, many=True).data)

    @action(detail=False, methods=["get"])
    def replay(self, request):
        """仅凭审计事件重放，折叠出每对刊名沿革的当前状态。"""
        qs = TitleSuccessionEvent.objects.all().order_by("id")
        title_id = request.query_params.get("title")
        if title_id:
            qs = qs.filter(
                Q(succession__predecessor_id=title_id)
                | Q(succession__successor_id=title_id),
            )
        return Response({"states": replay_succession_events(qs)})


class TimelineViewSet(viewsets.ViewSet):
    """前端时间轴数据源：编号 × 发行 × 实物三层，外加停刊标记与刊名沿革。"""

    def list(self, request):
        title_id = request.query_params.get("title")
        if not title_id:
            return Response(
                {"detail": "需要提供 title 参数。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        include_lineage = _want_lineage(request)
        try:
            title = Title.objects.get(pk=title_id)
        except Title.DoesNotExist:
            return Response(
                {"detail": "刊名不存在。"},
                status=status.HTTP_404_NOT_FOUND,
            )
        groups = timeline_groups(title, include_lineage)
        anchor = next(g for g in groups if g["title"]["id"] == int(title_id))
        # 顶层保留旧字段（对应当前刊名）；沿革视图额外用 groups 分组展示
        return Response({
            "title": anchor["title"],
            "slots": anchor["slots"],
            "include_lineage": include_lineage,
            "groups": groups,
        })
