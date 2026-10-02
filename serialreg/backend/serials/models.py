"""连续出版物登记领域模型。

四层结构：
  Title        连续出版物（书目层，可标记停刊）
  IssueNumber  卷期编号（一个编号槽位，跨发行月的跨年卷按卷+期唯一）
  Issue        发行实体（一次出版行为；普通期挂一个编号，合刊挂多个编号）
  Item         馆内实物（一条条码=一个实物，不允许一条条码代表多个合刊期号关系）
  Binding      装订册（多个 Item 装订在一起，拆订后 Item 恢复各自位置）

刊名沿革（书目层之上的版本化关系）：
  TitleSuccession  前身刊 → 后继刊的更名关系，带生效月份；
                   行不可变，纠正/撤销一律写新版本与审计，绝不覆盖。
  SuccessionAudit  沿革关系的创建/纠正/撤销事件流，旧检索结果可据此重放。

两条易混的业务规则分开表达：
  缺号 = IssueNumber 没有对应 Issue（没有发行记录），不自动等于缺藏；
  缺藏 = 该编号已发行（存在 Issue），但没有入库 Item 或 Item 丢失。
"""
from datetime import date

from django.db import models, transaction
from django.db.models import Q
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


class TitleSuccession(models.Model):
    """刊名沿革：前身刊 predecessor 在 effective_month 起更名为后继刊 successor。

    设计要点：
      * 只表达「书目层的连续关系」——馆藏、ISSN、装订、历史期号都留在原 Title 上，
        生效日不迁移任何实体；生效月前的发行归前身刊，之后的新发行归后继刊。
      * predecessor_issn 是关系建立瞬间对前身刊名/ISSN 的快照，
        之后即使书目层改写刊名/ISSN，沿革上保存的原名仍可重放。
      * 行不可变。纠正（换后继刊或调整生效月）= superseded 旧行 + 新建 active 行；
        撤销 = active 行置 revoked。两个动作都另写 SuccessionAudit。
    """

    class State(models.TextChoices):
        ACTIVE = "active", "生效中"
        SUPERSEDED = "superseded", "已被新版本纠正"
        REVOKED = "revoked", "已撤销"

    predecessor = models.ForeignKey(
        Title, on_delete=models.PROTECT, related_name="successions_out",
    )
    successor = models.ForeignKey(
        Title, on_delete=models.PROTECT, related_name="successions_in",
    )
    effective_month = models.DateField(
        "更名生效月份", help_text="只取年月（统一存为该月 1 日）；含当月",
    )
    predecessor_title_snapshot = models.CharField(
        "前身刊名快照", max_length=255,
        help_text="建立关系时的原刊名，审计/重放时不再依赖 Title 现值",
    )
    predecessor_issn_snapshot = models.CharField(
        "前身ISSN快照", max_length=9, blank=True,
    )
    state = models.CharField(
        max_length=12, choices=State.choices, default=State.ACTIVE,
    )
    superseded_by = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="supersedes",
        help_text="纠正本版本的新版本行",
    )
    revoked_at = models.DateTimeField("撤销时间", null=True, blank=True)
    revoked_reason = models.CharField("撤销原因", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "刊名沿革"
        ordering = ["effective_month", "id"]
        constraints = [
            # 同一前身刊在同一时期只能有一个生效中的后继，禁止互相矛盾的后继关系
            models.UniqueConstraint(
                fields=["predecessor"],
                condition=Q(state="active"),
                name="uniq_active_successor_per_predecessor",
            ),
            # 生效月份是「月」粒度：PostgreSQL 上强制为当月 1 日
            models.CheckConstraint(
                condition=Q(effective_month__day=1),
                name="succession_effective_month_is_month_start",
            ),
        ]

    def __str__(self):
        return (
            f"{self.predecessor_title_snapshot} → {self.successor.title}"
            f"@{self.effective_month:%Y-%m}[{self.state}]"
        )

    @property
    def is_active(self):
        return self.state == self.State.ACTIVE


class SuccessionAudit(models.Model):
    """刊名沿革的事件流（append-only）：创建 / 纠正 / 撤销。

    旧检索结果不被覆盖：任何时刻都能按版本链重放出当时的沿革图。
    """

    class Action(models.TextChoices):
        CREATE = "create", "建立沿革"
        CORRECT = "correct", "纠正沿革"
        REVOKE = "revoke", "撤销沿革"

    action = models.CharField(max_length=10, choices=Action.choices)
    succession = models.ForeignKey(
        TitleSuccession, on_delete=models.PROTECT,
        related_name="audits",
        help_text="事件所针对的（旧）关系版本行",
    )
    new_succession = models.ForeignKey(
        TitleSuccession, on_delete=models.PROTECT, null=True, blank=True,
        related_name="born_from_audits",
        help_text="纠正事件产生的新版本行",
    )
    predecessor = models.ForeignKey(
        Title, on_delete=models.PROTECT, related_name="succession_audits",
    )
    successor = models.ForeignKey(
        Title, on_delete=models.PROTECT, related_name="incoming_succession_audits",
    )
    effective_month = models.DateField("事件时的生效月份")
    predecessor_title_snapshot = models.CharField("前身刊名快照", max_length=255)
    predecessor_issn_snapshot = models.CharField("前身ISSN快照", max_length=9, blank=True)
    detail = models.CharField("说明", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "刊名沿革审计"
        ordering = ["id"]


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
                "title_id": item.title_id,
                "barcode": item.barcode,
                "status": item.status,
                "location": item.current_location(),
                "bound": item.is_bound,
                "binding": item.binding_entry.binding.call_number if item.is_bound else None,
            })
    return rows


# ---------------------------------------------------------------------------
# 刊名沿革：图遍历、时间边界、版本化写入
# ---------------------------------------------------------------------------

def month_start(value):
    """任意日期归一化为该月 1 日（沿革统一按月粒度存储）。接受 YYYY-MM。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value.replace(day=1)
    text = str(value).strip()
    if len(text) == 7:  # YYYY-MM
        text = f"{text}-01"
    return date.fromisoformat(text[:10]).replace(day=1)


def active_successions():
    """当前生效中的全部沿革边。"""
    return TitleSuccession.objects.filter(
        state=TitleSuccession.State.ACTIVE,
    ).select_related("predecessor", "successor")


def lineage_chain(anchor_title_id):
    """返回 anchor 所属沿革链上的全部 Title（含自身），按更名时间从早到晚排序。

    沿革图是「每刊至多一个 active 后继」的有向链；此处同时沿前向
    （successor）与后向（predecessor）遍历，并用 visited 集合兜底防环。
    """
    edges = list(active_successions())
    forward = {e.predecessor_id: e for e in edges}
    backward = {e.successor_id: e for e in edges}

    def walk(start_id, step_map, key):
        seen, cur, out = set(), start_id, []
        while cur in step_map and cur not in seen:
            seen.add(cur)
            edge = step_map[cur]
            nxt = key(edge)
            out.append(edge)
            cur = nxt
        return out

    older = list(reversed(walk(
        anchor_title_id, backward, lambda e: e.predecessor_id)))
    newer = walk(anchor_title_id, forward, lambda e: e.successor_id)

    ordered_edges = older + newer
    ids = []
    if ordered_edges:
        ids = [ordered_edges[0].predecessor_id]
        for e in ordered_edges:
            if e.successor_id not in ids:
                ids.append(e.successor_id)
    if anchor_title_id not in ids:
        ids = [anchor_title_id]

    titles_by_id = {
        t.id: t for t in Title.objects.filter(id__in=ids)
    }
    edges_by_boundary = {
        (e.predecessor_id, e.successor_id): e for e in ordered_edges
    }
    chain = []
    for index, tid in enumerate(ids):
        boundary = None
        if index > 0:
            edge = edges_by_boundary.get((ids[index - 1], tid))
            boundary = edge.effective_month if edge else None
        chain.append({
            "title": titles_by_id.get(tid),
            "title_id": tid,
            "index": index,
            "effective_from": boundary,
        })
    return chain


def lineage_title_ids(anchor_title_id):
    return [node["title_id"] for node in lineage_chain(anchor_title_id)]


def title_at_month(anchor_title_id, month):
    """给定链上任一刊与某个月，返回该月发行在沿革上应归属的刊。

    沿革不改写既有数据归属（生效日前的旧发行永远留在前身刊）；
    本函数只回答「这个月的新发行该登在哪本刊名下」。
    """
    month = month_start(month)
    chain = lineage_chain(anchor_title_id)
    current = chain[0]["title_id"]
    for node in chain:
        boundary = node["effective_from"]
        if boundary is None:
            continue
        if month >= month_start(boundary):
            current = node["title_id"]
        else:
            break
    return current


def _would_create_cycle(predecessor_id, successor_id):
    """若新增 predecessor → successor 后是否成环（含自环）。

    沿 successor 的前向链若能走回 predecessor，即构成环。
    """
    if predecessor_id == successor_id:
        return True
    forward = {e.predecessor_id: e.successor_id for e in active_successions()}
    cur = successor_id
    seen = set()
    while cur in forward and cur not in seen:
        seen.add(cur)
        if forward[cur] == predecessor_id:
            return True
        cur = forward[cur]
    return False


def validate_succession(predecessor, successor, effective, current_row=None):
    """沿革关系的跨表校验，返回归一化的生效月。失败抛 ValidationError。"""
    if predecessor.pk is None or successor.pk is None:
        raise ValidationError("前身刊与后继刊必须是已存在的刊名。")
    if predecessor.pk == successor.pk:
        raise ValidationError({"successor": "前身刊与后继刊不能是同一刊名。"})
    effective = month_start(effective)
    if not effective:
        raise ValidationError({"effective_month": "更名生效月份必填。"})

    # 同一时期互相矛盾的后继：该前身刊已有另一条 active 边
    clash = TitleSuccession.objects.filter(
        predecessor=predecessor, state=TitleSuccession.State.ACTIVE,
    ).exclude(pk=current_row.pk if current_row else None).first()
    if clash:
        raise ValidationError({
            "successor": (
                f"该刊在 {clash.effective_month:%Y-%m} 已有生效中的后继"
                f"《{clash.successor.title}》，同一时期不能有两个互相矛盾的后继；"
                "请先纠正或撤销原沿革。"
            ),
        })

    # 同一对刊名的沿革若已生效，不允许再建一条并行 active 版本
    # （已撤销的边不阻挡——撤销后允许重新建立）
    duplicate = TitleSuccession.objects.filter(
        predecessor=predecessor, successor=successor,
        state=TitleSuccession.State.ACTIVE,
    ).exclude(pk=current_row.pk if current_row else None).first()
    if duplicate:
        raise ValidationError({"successor": "该沿革关系已经存在且生效中。"})

    if _would_create_cycle(predecessor.pk, successor.pk):
        raise ValidationError({"successor": "沿革关系不能形成循环（A→B→…→A）。"})
    return effective


@transaction.atomic
def create_succession(*, predecessor, successor, effective_month, detail=""):
    """建立一条新沿革：快照原刊名/原 ISSN，写 active 行 + create 审计。"""
    effective = validate_succession(predecessor, successor, effective_month)
    # SELECT ... FOR UPDATE 锁住该前身刊的沿革集合，防并发插入矛盾边
    list(TitleSuccession.objects.select_for_update().filter(predecessor=predecessor))
    row = TitleSuccession.objects.create(
        predecessor=predecessor,
        successor=successor,
        effective_month=effective,
        predecessor_title_snapshot=predecessor.title,
        predecessor_issn_snapshot=predecessor.issn or "",
    )
    SuccessionAudit.objects.create(
        action=SuccessionAudit.Action.CREATE,
        succession=row, new_succession=row,
        predecessor=predecessor, successor=successor,
        effective_month=effective,
        predecessor_title_snapshot=predecessor.title,
        predecessor_issn_snapshot=predecessor.issn or "",
        detail=detail or "建立刊名沿革",
    )
    return row


@transaction.atomic
def correct_succession(row, *, successor=None, effective_month=None, detail=""):
    """纠正一条沿革：旧行置 superseded（不覆盖），新建 active 行并写审计。"""
    row = TitleSuccession.objects.select_for_update().get(pk=row.pk)
    if row.state != TitleSuccession.State.ACTIVE:
        raise ValidationError("只有生效中的沿革才能被纠正。")
    new_successor = successor or row.successor
    new_effective = month_start(effective_month) if effective_month else row.effective_month

    # 旧后继自己还有 active 出边时，纠正会把沿革链截成两段（下游成孤儿），
    # 必须先处理下游，避免同一时期出现互相矛盾的链形态
    if successor is not None and successor != row.successor:
        downstream = TitleSuccession.objects.filter(
            predecessor=row.successor, state=TitleSuccession.State.ACTIVE,
        ).exclude(pk=row.pk).first()
        if downstream:
            raise ValidationError({
                "successor": (
                    f"旧后继《{row.successor.title}》自身还有生效中的后继"
                    f"《{downstream.successor.title}》，直接纠正会截断沿革链；"
                    "请先纠正或撤销其下游沿革。"
                ),
            })

    # 先把旧边移出 active 集合，再在新形态下做成环/冲突校验；
    # 整个函数在 atomic 块内，校验失败整体回滚，已发布沿革不受任何影响
    old_successor, old_effective = row.successor, row.effective_month
    row.state = TitleSuccession.State.SUPERSEDED
    row.save(update_fields=["state"])
    new_effective = validate_succession(
        row.predecessor, new_successor, new_effective,
    )
    list(TitleSuccession.objects.select_for_update().filter(predecessor=row.predecessor))
    new_row = TitleSuccession.objects.create(
        predecessor=row.predecessor,
        successor=new_successor,
        effective_month=new_effective,
        predecessor_title_snapshot=row.predecessor_title_snapshot,
        predecessor_issn_snapshot=row.predecessor_issn_snapshot,
    )
    row.refresh_from_db()
    row.superseded_by = new_row
    row.save(update_fields=["superseded_by"])
    SuccessionAudit.objects.create(
        action=SuccessionAudit.Action.CORRECT,
        succession=row, new_succession=new_row,
        predecessor=row.predecessor, successor=old_successor,
        effective_month=old_effective,
        predecessor_title_snapshot=row.predecessor_title_snapshot,
        predecessor_issn_snapshot=row.predecessor_issn_snapshot,
        detail=(
            detail
            or f"纠正沿革：后继《{old_successor.title}》@{old_effective:%Y-%m}"
            f" → 《{new_successor.title}》@{new_effective:%Y-%m}"
        ),
    )
    return new_row


@transaction.atomic
def revoke_succession(row, *, reason=""):
    """撤销一条沿革：active 行置 revoked（行与审计均保留，可重放）。"""
    row = TitleSuccession.objects.select_for_update().get(pk=row.pk)
    if row.state != TitleSuccession.State.ACTIVE:
        raise ValidationError("只有生效中的沿革才能撤销。")
    row.state = TitleSuccession.State.REVOKED
    row.revoked_at = timezone.now()
    row.revoked_reason = reason or ""
    row.save(update_fields=["state", "revoked_at", "revoked_reason"])
    SuccessionAudit.objects.create(
        action=SuccessionAudit.Action.REVOKE,
        succession=row,
        predecessor=row.predecessor, successor=row.successor,
        effective_month=row.effective_month,
        predecessor_title_snapshot=row.predecessor_title_snapshot,
        predecessor_issn_snapshot=row.predecessor_issn_snapshot,
        detail=reason or "撤销沿革",
    )
    return row


def replay_successions(audit_qs=None):
    """按审计顺序重放沿革事件，返回 (当前版本映射, 完整事件)。

    返回的 current 是 {predecessor_id: 版本行 dict}，可还原任一历史时刻的沿革图。
    """
    qs = (audit_qs or SuccessionAudit.objects.all()).order_by("id")
    current = {}
    events = []
    for a in qs.select_related("succession", "new_succession",
                               "predecessor", "successor"):
        if a.action == SuccessionAudit.Action.CREATE:
            current[a.predecessor_id] = a.new_succession_id
        elif a.action == SuccessionAudit.Action.CORRECT:
            current[a.predecessor_id] = a.new_succession_id
        elif a.action == SuccessionAudit.Action.REVOKE:
            current.pop(a.predecessor_id, None)
        events.append({
            "id": a.id,
            "action": a.action,
            "action_label": dict(SuccessionAudit.Action.choices).get(
                a.action, a.action),
            "predecessor_id": a.predecessor_id,
            "successor_id": a.successor_id,
            "effective_month": a.effective_month,
            "predecessor_title": a.predecessor_title_snapshot,
            "predecessor_issn": a.predecessor_issn_snapshot,
            "detail": a.detail,
            "created_at": a.created_at,
            "state_after": dict(current),
        })
    return current, events
