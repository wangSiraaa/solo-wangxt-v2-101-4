from django.db.models import Prefetch, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    Binding, Issue, IssueNumber, IssueNumbering, Item,
    SuccessionAudit, Title, TitleSuccession,
    lineage_chain, lineage_title_ids, locate_number, number_holding_status,
    replay_successions,
)
from .serializers import (
    BindingSerializer, IssueSerializer, ItemSerializer,
    IssueNumberSerializer, SuccessionAuditSerializer, SuccessionCreateSerializer,
    SuccessionCorrectSerializer, SuccessionRevokeSerializer,
    SuccessionSerializer, TitleSerializer, UnbindSerializer,
)


def _want_lineage(request):
    """lineage=1/true/include 表示「包含前身/后继」视图；缺省为仅当前刊名。"""
    return str(request.query_params.get("lineage", "")).lower() in ("1", "true", "include")


def _validation_error_response(exc):
    """服务层抛出的 django ValidationError 统一转成 DRF 400（事务已回滚）。"""
    from django.core.exceptions import ValidationError as _VE
    if not isinstance(exc, _VE):
        raise exc
    return Response(
        {"detail": exc.message_dict if hasattr(exc, "message_dict")
         else exc.messages},
        status=status.HTTP_400_BAD_REQUEST,
    )


class TitleViewSet(viewsets.ModelViewSet):
    serializer_class = TitleSerializer

    def get_queryset(self):
        qs = Title.objects.all()
        q = self.request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(issn__icontains=q))
        return qs

    def list(self, request, *args, **kwargs):
        if not _want_lineage(request):
            # 仅当前刊名：保持纯数组响应（与旧前端兼容）
            qs = self.filter_queryset(self.get_queryset())
            return Response(TitleSerializer(qs, many=True).data)

        # 「包含沿革」：以每条命中为锚点展开整条链（链上节点即使不匹配 q 也列出），
        # 但每个 Title 实体仍独立成行，馆藏/ISSN 不合并
        q = request.query_params.get("q", "").strip()
        anchor_ids = list(self.filter_queryset(self.get_queryset())
                          .order_by().values_list("id", flat=True))
        groups, seen_chains = [], set()
        for anchor_id in anchor_ids:
            chain = lineage_chain(anchor_id)
            chain_ids = [n["title_id"] for n in chain]
            key = tuple(chain_ids)
            if key in seen_chains:
                continue
            seen_chains.add(key)
            anchor_index = chain_ids.index(anchor_id)
            titles = {
                t.id: t for t in Title.objects.filter(
                    id__in=chain_ids).prefetch_related(
                        "successions_out__successor",
                        "successions_in__predecessor")
            }
            groups.append({
                "anchor_title_id": anchor_id,
                "matched_query": q,
                "titles": [
                    {
                        **TitleSerializer(titles[n["title_id"]]).data,
                        "lineage_role": (
                            "current" if n["title_id"] == anchor_id
                            else ("predecessor" if n["index"] < anchor_index
                                  else "successor")),
                        "effective_from": n["effective_from"],
                    }
                    for n in chain if n["title_id"] in titles
                ],
            })
        return Response({"view": "lineage", "lineage_groups": groups})

    @action(detail=True, methods=["get"])
    def lineage(self, request, pk=None):
        """某刊的完整沿革链（按更名时间排序，含生效月边界）。"""
        title = self.get_object()
        chain = lineage_chain(title.id)
        titles = {t.id: t for t in Title.objects.filter(
            id__in=[n["title_id"] for n in chain])}
        return Response({
            "anchor_title_id": title.id,
            "titles": [
                {
                    **TitleSerializer(titles[n["title_id"]]).data,
                    "index": n["index"],
                    "effective_from": n["effective_from"],
                }
                for n in chain
            ],
        })


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
            if _want_lineage(self.request):
                ids = lineage_title_ids(int(title_id))
                qs = qs.filter(title_id__in=ids)
            else:
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
        lineage=1 时，编号先在请求刊名中查找；未登记再沿沿革链在
        前身/后继刊中各自查找（实体仍按刊名分组成多条命中，绝不合并）。
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
                    "issue_id": it.issue_id,
                    "title_id": it.title_id,
                    "title": it.title.title,
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
            return Response({
                "query": {"barcode": barcode}, "view": "current",
                "matches": result,
            })

        if not (title_id and number):
            return Response(
                {"detail": "需要提供 barcode，或同时提供 title 与 number。"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        def _find_number(tid):
            qs = IssueNumber.objects.filter(title_id=tid, number=number)
            if volume != "":
                qs = qs.filter(volume=volume)
            return list(qs)

        searched = [int(title_id)]
        found = _find_number(title_id)
        searched_scope = "current"
        lineage_group = None
        if not found and include_lineage:
            chain = lineage_chain(int(title_id))
            lineage_group = [
                {"title_id": n["title_id"], "effective_from": n["effective_from"]}
                for n in chain
            ]
            for n in chain:
                if n["title_id"] == int(title_id):
                    continue
                rows = _find_number(n["title_id"])
                searched.append(n["title_id"])
                if rows:
                    found.extend(rows)
            searched_scope = "lineage"

        if not found:
            if searched_scope == "lineage":
                return Response({
                    "detail": "该卷期编号在当前刊名及其沿革链中均未登记。",
                    "holding_status": "unregistered",
                    "view": "lineage",
                    "searched_title_ids": searched,
                    "lineage": lineage_group,
                    "matches": [],
                }, status=status.HTTP_404_NOT_FOUND)
            return Response({
                "detail": "该卷期编号未在馆藏系统登记。",
                "holding_status": "unregistered",
                "view": "current",
                "matches": [],
            }, status=status.HTTP_404_NOT_FOUND)

        if len({n.id for n in found}) > 1:
            return Response(
                {"detail": "卷/期在沿革链中定位到多条编号，请限定具体刊名或补全卷号。"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        issue_number = found[0]
        matches = locate_number(issue_number)
        titles = {t.id: t for t in Title.objects.filter(
            id__in={m["title_id"] for m in matches} | {issue_number.title_id})}
        for m in matches:
            t = titles.get(m["title_id"])
            m["title"] = t.title if t else None
            m["lineage_role"] = (
                "current" if m["title_id"] == int(title_id) else "lineage")
        payload = {
            "query": {"title": title_id, "volume": volume, "number": number},
            "view": searched_scope,
            "resolved_title_id": issue_number.title_id,
            "holding_status": number_holding_status(
                issue_number.title, issue_number),
            "searched_title_ids": searched,
            "matches": matches,
        }
        if lineage_group is not None:
            payload["lineage"] = lineage_group
        return Response(payload)


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


class TitleSuccessionViewSet(viewsets.ReadOnlyModelViewSet):
    """刊名沿革（版本化）。写入只允许 create/correct/revoke 三个审计动作。"""

    queryset = TitleSuccession.objects.select_related(
        "predecessor", "successor", "superseded_by").order_by("id")
    serializer_class = SuccessionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        state = self.request.query_params.get("state")
        if title_id:
            qs = qs.filter(
                Q(predecessor_id=title_id) | Q(successor_id=title_id))
        if state:
            qs = qs.filter(state=state)
        return qs

    def create(self, request):
        serializer = SuccessionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            row = serializer.save()
        except Exception as exc:
            return _validation_error_response(exc)
        return Response(SuccessionSerializer(row).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        row = self.get_object()
        serializer = SuccessionCorrectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            new_row = serializer.save_row(row)
        except Exception as exc:
            return _validation_error_response(exc)
        return Response(SuccessionSerializer(new_row).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        row = self.get_object()
        serializer = SuccessionRevokeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            row = serializer.save_row(row)
        except Exception as exc:
            return _validation_error_response(exc)
        return Response(SuccessionSerializer(row).data)

    @action(detail=False, methods=["get"])
    def audits(self, request):
        """审计事件流，可按刊过滤；?replay=1 额外返回重放后的当前版本映射。"""
        qs = SuccessionAudit.objects.select_related(
            "predecessor", "successor", "succession", "new_succession",
        ).order_by("id")
        title_id = request.query_params.get("title")
        if title_id:
            qs = qs.filter(
                Q(predecessor_id=title_id) | Q(successor_id=title_id))
        if request.query_params.get("replay"):
            current, events = replay_successions(qs)
            return Response({
                "events": events,
                "replayed_current": current,
            })
        return Response({
            "events": SuccessionAuditSerializer(qs, many=True).data,
        })


class TimelineViewSet(viewsets.ViewSet):
    """前端时间轴数据源：编号 × 发行 × 实物三层，外加停刊标记与沿革分组。"""

    def list(self, request):
        title_id = request.query_params.get("title")
        if not title_id:
            return Response(
                {"detail": "需要提供 title 参数。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        include_lineage = _want_lineage(request)
        title = Title.objects.get(pk=title_id)

        chain = lineage_chain(title.id)
        chain_titles = {
            t.id: t for t in Title.objects.filter(
                id__in=[n["title_id"] for n in chain])
        }
        own_node = next((n for n in chain if n["title_id"] == title.id), None)
        own_index = own_node["index"] if own_node else 0
        lineage = [
            {
                "title_id": n["title_id"],
                "title": getattr(chain_titles.get(n["title_id"]), "title", None),
                "issn": getattr(chain_titles.get(n["title_id"]), "issn", None),
                "index": n["index"],
                "effective_from": n["effective_from"],
                "role": (
                    "current" if n["title_id"] == title.id
                    else ("predecessor" if n["index"] < own_index
                          else "successor")),
            }
            for n in chain
        ]

        if include_lineage:
            scope_ids = [n["title_id"] for n in chain]
        else:
            scope_ids = [title.id]

        numbers = (
            IssueNumber.objects.filter(title_id__in=scope_ids)
            .select_related("title")
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
                    ).select_related("title"),
                ),
            )
            .order_by("title_id", "sort_key", "id")
        )

        def serialize_number(n):
            issues = list(n.issues.all())
            return {
                "number_id": n.id,
                "title_id": n.title_id,
                "title": n.title.title,
                "volume": n.volume,
                "number": n.number,
                "holding_status": number_holding_status(n.title, n),
                "issues": [
                    {
                        "issue_id": iss.id,
                        "title_id": iss.title_id,
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
                                "title_id": it.title_id,
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
            }

        groups = []
        own_slots = []
        by_title = {}
        for n in numbers:
            by_title.setdefault(n.title_id, []).append(n)
        for node in chain:
            slots = [serialize_number(n) for n in by_title.get(node["title_id"], [])]
            groups.append({
                "title_id": node["title_id"],
                "title": getattr(chain_titles.get(node["title_id"]), "title", None),
                "issn": getattr(chain_titles.get(node["title_id"]), "issn", None),
                "status": getattr(
                    chain_titles.get(node["title_id"]), "status", None),
                "ceased_month": getattr(
                    chain_titles.get(node["title_id"]), "ceased_month", None),
                "effective_from": node["effective_from"],
                "slots": slots,
            })
            if node["title_id"] == title.id:
                own_slots = slots

        return Response({
            "title": TitleSerializer(title).data,
            "view": "lineage" if include_lineage else "current",
            "lineage": lineage,
            # slots 始终是「请求刊名自身」的槽位，保证旧前端/旧测试语义不被沿革改写
            "slots": own_slots,
            # lineage 视图额外提供按刊名分组的全部槽位（实体归属不串刊）
            "groups": groups,
        })
