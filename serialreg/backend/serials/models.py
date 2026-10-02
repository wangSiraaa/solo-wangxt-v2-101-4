"""连续出版物登记领域模型。

四层结构：
  Title        连续出版物（书目层，可标记停刊）
  IssueNumber  卷期编号（一个编号槽位，跨发行月的跨年卷按卷+期唯一）
  Issue        发行实体（一次出版行为；普通期挂一个编号，合刊挂多个编号）
  Item         馆内实物（一条条码=一个实物，不允许一条条码代表多个合刊期号关系）
  Binding      装订册（多个 Item 装订在一起，拆订后 Item 恢复各自位置）

两条易混的业务规则分开表达：
  缺号 = IssueNumber 没有对应 Issue（没有发行记录），不自动等于缺藏；
  缺藏 = 该编号已发行（存在 Issue），但没有入库 Item 或 Item 丢失。
"""
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import ExtractDay
from django.core.exceptions import ValidationError
from django.utils import timezone


class Title(models.Model):
    """连续出版物刊名。"""

    class PublicationStatus(models.TextChoices):
        ACTIVE = "active", "在刊"
        CEASED = "ceased", "停刊"

    title = models.CharField("刊名", max_length=255)
    issn = models.CharField("ISSN", max_length=9, blank=True)
    publisher = models.CharField("出版者", max_length=255, blank=True)
    status = models.CharField(
        "出版状态", max_length=10,
        choices=PublicationStatus.choices, default=PublicationStatus.ACTIVE,
    )
    # 停刊月份：与卷期编号分开记录，只表示出版停止，不改变任何馆藏状态
    ceased_month = models.DateField("停刊月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.status == self.PublicationStatus.CEASED and not self.ceased_month:
            raise ValidationError({"ceased_month": "停刊刊名必须填写停刊月份。"})


class IssueNumber(models.Model):
    """卷期编号（编号槽位），与发行年月解耦。

    跨年卷：同一卷可以跨自然年，例如 v.60 no.3 印的是 2023-12、2024-01，
    编号仍只有一条 (volume=60, number=3)，发行时间记录在 Issue 上。
    """

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="numbers",
    )
    volume = models.CharField("卷", max_length=20, blank=True)
    number = models.CharField("期", max_length=20)
    sort_key = models.PositiveIntegerField(
        "排序键", default=0,
        help_text="馆员录入的编号顺序，跨年卷按编号顺序而非月份排列",
    )

    class Meta:
        verbose_name = "期号"
        unique_together = ("title", "volume", "number")
        ordering = ["sort_key", "id"]

    def __str__(self):
        return f"{self.volume}({self.number})" if self.volume else self.number


class Issue(models.Model):
    """一次发行。普通期关联一个 IssueNumber；两期合刊关联两个（或更多）。"""

    class IssueKind(models.TextChoices):
        REGULAR = "regular", "普通期"
        COMBINED = "combined", "合刊"

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="issues",
    )
    kind = models.CharField(
        "类型", max_length=10,
        choices=IssueKind.choices, default=IssueKind.REGULAR,
    )
    # 发行年月与卷期编号分开录入
    issue_month = models.DateField("发行年月", help_text="只取年月；合刊可只填起始月")
    issue_month_end = models.DateField(
        "发行截止年月", null=True, blank=True, help_text="合刊/跨年卷的覆盖结束月",
    )
    numbers = models.ManyToManyField(
        IssueNumber, through="IssueNumbering", related_name="issues",
    )
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "发行期"
        ordering = ["issue_month", "id"]

    def __str__(self):
        nums = "·".join(str(n) for n in self.numbers.all())
        return f"{self.title.title} {nums}"

    def clean(self):
        if self.issue_month_end and self.issue_month_end < self.issue_month:
            raise ValidationError({"issue_month_end": "截止年月不能早于起始年月。"})
        # 更名边界：发行归属由「起始发行月」决定，不看截止月——
        # 跨年卷/合刊即便覆盖到生效月之后，仍归原刊名，实体不随边界迁移。
        if self.title_id and self.issue_month:
            conflict = issue_month_conflict(self.title_id, self.issue_month)
            if conflict is not None:
                other_id, _direction = conflict
                t = Title.objects.get(pk=other_id)
                raise ValidationError(
                    {"issue_month": (
                        f"{self.issue_month:%Y-%m} 的发行应归属"
                        f"《{t.title}》（ISSN {t.issn or '无'}）。"
                        "更名只改书目关系、不迁移编号与馆藏：生效月之前"
                        "（含跨年卷/合刊）归原刊名，生效月之后归新刊名。"
                    )},
                )


class IssueNumbering(models.Model):
    """Issue ↔ IssueNumber 关联表。

    合刊的两个期号必须是两条独立关联记录，而不是把 3-4 塞进一个条码字段。
    """

    issue = models.ForeignKey(
        Issue, on_delete=models.CASCADE, related_name="numberings",
    )
    number = models.ForeignKey(
        IssueNumber, on_delete=models.CASCADE, related_name="numberings",
    )
    label = models.CharField("封面标识", max_length=40, blank=True,
                             help_text="如 no.3-4，仅作展示")

    class Meta:
        unique_together = ("issue", "number")


class Item(models.Model):
    """馆内实物（册）。一个条码 = 一个实物。"""

    class ItemStatus(models.TextChoices):
        AVAILABLE = "available", "在馆"
        CHECKED_OUT = "checked_out", "借出"
        LOST = "lost", "丢失"
        BOUND = "bound", "已装订"

    barcode = models.CharField("条码", max_length=40, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="items",
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT, related_name="items",
        help_text="实物对应的发行期；合刊实物只指向这一个 Issue，"
                  "对多个期号的覆盖由 IssueNumbering 表达",
    )
    # 未装订时的实际位置；装订后以 binding 的 location 为准
    location = models.CharField("馆藏位置", max_length=100, blank=True)
    status = models.CharField(
        "馆藏状态", max_length=12,
        choices=ItemStatus.choices, default=ItemStatus.AVAILABLE,
    )
    accessioned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["barcode"]

    @property
    def is_bound(self):
        return hasattr(self, "binding_entry")

    def current_location(self):
        """装订后返回装订册位置，否则返回自身位置。"""
        entry = getattr(self, "binding_entry", None)
        if entry is not None:
            return entry.binding.location
        return self.location

    def __str__(self):
        return self.barcode


class Binding(models.Model):
    """装订册：把若干已入藏实物装订在一起，实物身份与条码不变。"""

    call_number = models.CharField("装订索书号", max_length=60, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="bindings",
    )
    location = models.CharField("装订后位置", max_length=100)
    bound_month = models.DateField("装订月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    items = models.ManyToManyField(Item, through="BindingEntry", related_name="bindings")

    class Meta:
        verbose_name = "装订册"
        ordering = ["call_number"]

    def __str__(self):
        return self.call_number


class BindingEntry(models.Model):
    item = models.OneToOneField(
        Item, on_delete=models.CASCADE, related_name="binding_entry",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.CASCADE, related_name="entries",
    )
    # 装订时封存该实物原位置，拆订后恢复
    previous_location = models.CharField("装订前位置", max_length=100, blank=True)
    bound_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("item", "binding")

    def clean(self):
        if self.item_id and self.item.title_id != self.binding.title_id:
            raise ValidationError("装订册内的实物必须属于同一种刊。")


def number_holding_status(title, number):
    """计算某个期号的馆藏视图状态。

    issued+held       已发行且有在馆实物（含装订）
    issued+missing    已发行但缺藏（无实物或全部丢失/借出按调用方再细分）
    not_published     缺号：没有任何发行记录，不自动等同缺藏
    ceased_gap        停刊后出现的编号（永远不会有发行）
    """
    issues = list(number.issues.prefetch_related("items"))
    if not issues:
        ceased = title.ceased_month
        if title.status == Title.PublicationStatus.CEASED and ceased:
            return "ceased_gap"
        return "not_published"
    items = [it for iss in issues for it in iss.items.all()]
    held = any(it.status != Item.ItemStatus.LOST for it in items)
    return "issued+held" if held else "issued+missing"


def locate_number(number):
    """从任一期号找到其所在实物与实际位置（合刊、装订都可命中）。"""
    rows = []
    for issue in number.issues.all():
        for item in issue.items.select_related("title"):
            rows.append({
                "issue_id": issue.id,
                "barcode": item.barcode,
                "status": item.status,
                "location": item.current_location(),
                "bound": item.is_bound,
                "binding": item.binding_entry.binding.call_number if item.is_bound else None,
            })
    return rows


# ==========================================================================
# 刊名沿革（更名）
# --------------------------------------------------------------------------
# 沿革是刊名之间带生效月的有向边：predecessor（原刊名/原 ISSN）
# → successor（新刊名/新 ISSN），effective_month 为第一个归属新刊名的月份。
#
# 关键不变量：
#   1. 沿革只增加「书目关系」，不迁移任何实体——生效月之前的 Issue/Item/
#      Binding 永远挂在原 Title 上（跨年卷、合刊也不例外），生效月之后的
#      新发行归新 Title；见 Issue.clean 与 IssueSerializer 的边界校验。
#   2. 图是无环的（一种刊不能经更名回到自身）；
#   3. 任一刊名在任一时期最多一条有效后继、最多一条有效前身——
#      同一起点的矛盾后继会直接拒绝；
#   4. 撤销/纠正不覆盖旧数据：关系行版本化、全部动作写入事件审计，
#      审计可重放出当前视图。
# ==========================================================================

def _month_first_day(value):
    """生效月份统一归一到该月 1 日。"""
    return value.replace(day=1)


class TitleSuccession(models.Model):
    """一条生效中的刊名沿革边：原刊名 → 后继刊名，自生效月起。"""

    predecessor = models.ForeignKey(
        Title, on_delete=models.PROTECT,
        related_name="successions_out", verbose_name="原刊名",
    )
    successor = models.ForeignKey(
        Title, on_delete=models.PROTECT,
        related_name="successions_in", verbose_name="后继刊名",
    )
    effective_month = models.DateField(
        "更名生效月份",
        help_text="该月（含）起的新发行归后继刊名；统一按当月 1 日存储",
    )
    # 建边时封存原刊名与原 ISSN：更名不重写书目身份，审计与展示都以此为准
    predecessor_title_snapshot = models.CharField("原刊名快照", max_length=255)
    predecessor_issn_snapshot = models.CharField("原ISSN快照", max_length=9, blank=True)
    successor_title_snapshot = models.CharField("新刊名快照", max_length=255)
    successor_issn_snapshot = models.CharField("新ISSN快照", max_length=9, blank=True)
    # effective_day 由 effective_month 派生，仅用于 CheckConstraint 强制「月」粒度
    effective_day = models.GeneratedField(
        expression=ExtractDay("effective_month"),
        output_field=models.PositiveSmallIntegerField(),
        db_persist=True,
    )
    note = models.CharField("备注", max_length=255, blank=True)
    version = models.PositiveIntegerField(
        "版本", default=1,
        help_text="同一对刊名第几次建立沿革；撤销后重走为新版本",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField("撤销时间", null=True, blank=True)

    class Meta:
        verbose_name = "刊名沿革"
        ordering = ["effective_month", "id"]
        constraints = [
            # 同一对刊名最多一条「在效」边（纠正=撤销后按新版本重建）
            models.UniqueConstraint(
                fields=["predecessor", "successor"],
                condition=Q(revoked_at__isnull=True),
                name="succ_unique_active_pair",
            ),
            # 一个刊名同一时期只能有一个有效后继（禁止矛盾后继）
            models.UniqueConstraint(
                fields=["predecessor"],
                condition=Q(revoked_at__isnull=True),
                name="succ_unique_active_predecessor",
            ),
            # 一个刊名同一时期只能有一个有效前身
            models.UniqueConstraint(
                fields=["successor"],
                condition=Q(revoked_at__isnull=True),
                name="succ_unique_active_successor",
            ),
            # 生效月必须落在月份第一天（「带生效月份」而非任意日期）
            models.CheckConstraint(
                condition=Q(effective_day=1),
                name="succ_effective_month_is_first_day",
            ),
        ]

    def __str__(self):
        return (
            f"{self.predecessor_title_snapshot} → "
            f"{self.successor_title_snapshot}@{self.effective_month:%Y-%m}"
        )

    @property
    def is_active(self):
        return self.revoked_at is None


class TitleSuccessionEvent(models.Model):
    """沿革审计事件（append-only）：建立与撤销都记账，旧检索结果永不被覆盖。"""

    class Action(models.TextChoices):
        ESTABLISH = "establish", "建立"
        REVOKE = "revoke", "撤销"

    succession = models.ForeignKey(
        TitleSuccession, on_delete=models.PROTECT,
        related_name="events", verbose_name="沿革",
    )
    action = models.CharField("动作", max_length=10, choices=Action.choices)
    version = models.PositiveIntegerField("沿革版本快照")
    actor = models.CharField("操作人", max_length=80, blank=True)
    reason = models.CharField("原因/说明", max_length=255, blank=True)
    payload = models.JSONField(
        "事件载荷", default=dict,
        help_text="建立时记录生效月与刊名/ISSN 快照；撤销时记录撤销时刻",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "沿革审计事件"
        ordering = ["id"]

    def __str__(self):
        return f"{self.get_action_display()} v{self.version} #{self.succession_id}"


def active_succession_after(title_id, month):
    """返回使 title 在 month 当天「已不是该月发行归属刊名」的有效沿革（出边）。

    即：若 title 自某生效月起更名为后继刊，且 month >= 生效月，则返回该边。
    """
    return (
        TitleSuccession.objects
        .filter(predecessor_id=title_id, revoked_at__isnull=True,
                effective_month__lte=month)
        .order_by("-effective_month", "-id")
        .first()
    )


def active_succession_before(title_id, month):
    """返回使 title 在 month 当天「由前身更名而来」的有效沿革（入边）。

    即：若 title 是 successor，生效月 <= month，则返回该边。
    """
    return (
        TitleSuccession.objects
        .filter(successor_id=title_id, revoked_at__isnull=True,
                effective_month__lte=month)
        .order_by("-effective_month", "-id")
        .first()
    )


def title_owning_month(title_id, month):
    """按更名边界判定 month 月的新发行应归属哪个 Title。

    边界严格按「月」比较：有效沿革的生效月 <= month 即归后继刊名。
    跨年卷/合刊的 issue_month_end 不参与判定（实体不迁移，见 Issue.clean）。
    无边界时返回 title_id 自身。
    """
    edge = active_succession_after(title_id, month)
    return edge.successor_id if edge else title_id


def issue_month_conflict(title_id, month):
    """若 month 月的「新发行」不能登记在 title 名下，返回冲突信息。

    双向边界：
      - title 已有生效出边且生效月 <= month：该月起归后继刊名；
      - title 已有生效入边但生效月 > month：该月之前仍归前身刊名。
    命中返回 (other_title_id, 入边or出边)，否则 None。
    """
    out = active_succession_after(title_id, month)
    if out:
        return out.successor_id, "outgoing"
    incoming = (
        TitleSuccession.objects
        .filter(successor_id=title_id, revoked_at__isnull=True,
                effective_month__gt=month)
        .order_by("effective_month", "id")
        .first()
    )
    if incoming:
        return incoming.predecessor_id, "incoming"
    return None


def lineage_chain_ids(title_id):
    """返回该刊名沿革链上的全部刊名 id（含自身），按时间从早到晚排列。

    有效边是 1:1 的链；为防历史脏数据成环，访问计数超过节点数即停止。
    """
    ids = [int(title_id)]
    prev_map = dict(
        TitleSuccession.objects.filter(revoked_at__isnull=True)
        .values_list("successor_id", "predecessor_id")
    )
    next_map = dict(
        TitleSuccession.objects.filter(revoked_at__isnull=True)
        .values_list("predecessor_id", "successor_id")
    )
    cur = int(title_id)
    guard = len(prev_map) + 2
    while cur in prev_map and guard > 0:
        p = prev_map[cur]
        if p in ids:
            break
        ids.insert(0, p)
        cur = p
        guard -= 1
    cur = int(title_id)
    guard = len(next_map) + 2
    while cur in next_map and guard > 0:
        n = next_map[cur]
        if n in ids:
            break
        ids.append(n)
        cur = n
        guard -= 1
    return ids


def lineage_context(anchor_id):
    """以 anchor 为视角构造沿革链上下文（从早到晚）。

    返回 [{"title_id", "effective_from"(该段刊名的首月),"role"}]，
    role 为 self/predecessor/successor，全部相对 anchor；链起点
    effective_from 为 None。
    """
    chain_ids = lineage_chain_ids(anchor_id)
    edges = TitleSuccession.objects.filter(
        revoked_at__isnull=True,
        predecessor_id__in=chain_ids, successor_id__in=chain_ids,
    )
    effective_from = {}  # successor_id -> 入边生效月
    for e in edges:
        effective_from[e.successor_id] = e.effective_month
    anchor_index = chain_ids.index(int(anchor_id))
    out = []
    for index, tid in enumerate(chain_ids):
        if tid == int(anchor_id):
            role = "self"
        elif index < anchor_index:
            role = "predecessor"
        else:
            role = "successor"
        out.append({
            "title_id": tid,
            "role": role,
            "effective_from": effective_from.get(tid),
        })
    return out


def _would_create_cycle(predecessor_id, successor_id):
    """若 successor 已能沿有效边到达 predecessor，则新增边将成环。"""
    if predecessor_id == successor_id:
        return True
    next_map = dict(
        TitleSuccession.objects.filter(revoked_at__isnull=True)
        .values_list("predecessor_id", "successor_id")
    )
    seen = {successor_id}
    cur = successor_id
    guard = len(next_map) + 1
    while cur in next_map and guard > 0:
        nxt = next_map[cur]
        if nxt == predecessor_id:
            return True
        if nxt in seen:
            return True
        seen.add(nxt)
        cur = nxt
        guard -= 1
    return False


def _next_pair_version(predecessor_id, successor_id):
    agg = TitleSuccession.objects.filter(
        predecessor_id=predecessor_id, successor_id=successor_id,
    ).aggregate(m=models.Max("version"))
    return (agg["m"] or 0) + 1


def establish_succession(*, predecessor, successor, effective_month,
                         note="", actor="", reason=""):
    """建立一条生效沿革并写审计；任何不变量违例都在写库前拒绝。

    - 自环/成环拒绝；
    - 同一刊名已有有效前身或后继（矛盾）拒绝；
    - 生效月必须为月粒度；
    - 边界两侧的既有发行归属不得与新生效月冲突——更名不迁移实体，
      因此新边界不能把已经登记在「错的一侧」的发行划到另一个刊名。
    整个过程在事务内，失败不留半成品。
    """
    effective_month = _month_first_day(effective_month)
    if predecessor.pk == successor.pk:
        raise ValidationError("原刊名与后继刊名不能是同一种刊。")

    with transaction.atomic():
        # 锁住两端，挡住并发建边造成的矛盾关系
        list(Title.objects.select_for_update().filter(
            pk__in=[predecessor.pk, successor.pk]))

        # 任一刊名在同一时期最多一条有效前身 / 一条有效后继。
        # 注意只看与两端点直接相关的边；链中间的合法延伸（A→B 再建 B→C）
        # 不算矛盾，交给成环检测。
        if TitleSuccession.objects.filter(
                predecessor=predecessor, revoked_at__isnull=True).exists():
            raise ValidationError(
                f"原刊名《{predecessor.title}》已有生效中的后继刊名；"
                "同一刊名同一时期不能有互相矛盾的后继。请先撤销既有沿革"
                "（会保留旧版本与审计）。",
            )
        if TitleSuccession.objects.filter(
                successor=successor, revoked_at__isnull=True).exists():
            raise ValidationError(
                f"后继刊名《{successor.title}》已有生效中的前身刊名；"
                "同一刊名同一时期不能有互相矛盾的前身。请先撤销既有沿革"
                "（会保留旧版本与审计）。",
            )
        if _would_create_cycle(predecessor.pk, successor.pk):
            raise ValidationError(
                "该沿革会形成循环：后继刊名最终又回到了原刊名，已拒绝。",
            )

        # 实体不迁移：校验已有发行与新边界不冲突。
        # 原刊名名下不允许存在「生效月及以后」的发行（那种发行本该归新刊名）。
        future = Issue.objects.filter(
            title=predecessor, issue_month__gte=effective_month,
        ).order_by("issue_month").first()
        if future:
            raise ValidationError(
                f"原刊名《{predecessor.title}》已有 {future.issue_month:%Y-%m}"
                "（生效月或之后）的发行记录。生效月之后的发行归新刊名，"
                "更名不会迁移既有发行；请核对生效月份或先在新刊名下登记。",
            )
        # 新刊名名下不允许存在「生效月之前」的发行（那种发行仍归原刊名）
        past = Issue.objects.filter(
            title=successor, issue_month__lt=effective_month,
        ).order_by("-issue_month").first()
        if past:
            raise ValidationError(
                f"后继刊名《{successor.title}》已有 {past.issue_month:%Y-%m}"
                "（生效月之前）的发行记录。生效月之前的发行仍归原刊名，"
                "更名不会迁移既有发行；请核对生效月份。",
            )

        version = _next_pair_version(predecessor.pk, successor.pk)
        edge = TitleSuccession.objects.create(
            predecessor=predecessor, successor=successor,
            effective_month=effective_month,
            predecessor_title_snapshot=predecessor.title,
            predecessor_issn_snapshot=predecessor.issn or "",
            successor_title_snapshot=successor.title,
            successor_issn_snapshot=successor.issn or "",
            note=note, version=version,
        )
        TitleSuccessionEvent.objects.create(
            succession=edge, action=TitleSuccessionEvent.Action.ESTABLISH,
            version=version, actor=actor, reason=reason,
            payload={
                "predecessor_id": predecessor.pk,
                "successor_id": successor.pk,
                "effective_month": effective_month.isoformat(),
                "predecessor_title": predecessor.title,
                "predecessor_issn": predecessor.issn or "",
                "successor_title": successor.title,
                "successor_issn": successor.issn or "",
                "note": note,
            },
        )
        return edge


def revoke_succession(*, succession, actor="", reason=""):
    """撤销一条生效沿革（版本化撤销，不删除旧记录），并写审计。

    撤销只解除书目关系：两侧 Issue/Item/Binding 的归属一律不动，
    已发布的旧检索结果仍可由审计重放。
    """
    with transaction.atomic():
        edge = TitleSuccession.objects.select_for_update().get(pk=succession.pk)
        if edge.revoked_at is not None:
            raise ValidationError("该沿革已经撤销，不能重复撤销。")
        edge.revoked_at = timezone.now()
        edge.save(update_fields=["revoked_at"])
        TitleSuccessionEvent.objects.create(
            succession=edge, action=TitleSuccessionEvent.Action.REVOKE,
            version=edge.version, actor=actor, reason=reason,
            payload={
                "revoked_at": edge.revoked_at.isoformat(),
                "predecessor_title": edge.predecessor_title_snapshot,
                "successor_title": edge.successor_title_snapshot,
            },
        )
        return edge


def replay_succession_events(events):
    """按事件顺序重放审计，折叠出每对刊名的最终状态。

    供「旧审计可重放」：不依赖当前 TitleSuccession 行，只凭事件流还原。
    返回 [{"key","predecessor_id","successor_id","version",
           "effective_month","status","establish_event","revoke_event"}]。
    """
    folded = {}
    for ev in events.select_related("succession").order_by("id"):
        key = (ev.succession.predecessor_id, ev.succession.successor_id)
        cur = folded.setdefault(key, {
            "predecessor_id": key[0], "successor_id": key[1],
            "version": ev.version, "effective_month": None,
            "status": "revoked",
            "establish_event": None, "revoke_event": None,
        })
        cur["version"] = ev.version
        if ev.action == TitleSuccessionEvent.Action.ESTABLISH:
            cur["effective_month"] = ev.payload.get("effective_month")
            cur["status"] = "active"
            cur["establish_event"] = ev.id
        else:
            cur["status"] = "revoked"
            cur["revoke_event"] = ev.id
    out = []
    for (pid, sid), cur in folded.items():
        cur["key"] = f"{pid}->{sid}"
        out.append(cur)
    out.sort(key=lambda c: (c["effective_month"] or "", c["predecessor_id"]))
    return out
