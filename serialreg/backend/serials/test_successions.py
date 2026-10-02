"""刊名沿革（更名）验收测试。

覆盖：
  1) 设置更名生效月后，包含沿革的检索连续展示前后刊，普通检索各自独立；
  2) 跨年卷历史期号、合刊条码、既有装订保留原刊名归属（实体不迁移）；
  3) 循环 / 矛盾后继被拒绝，且不影响已发布沿革；
  4) 撤销已使用沿革后当前视图更新、审计可重放，刷新后跨刊装订仍被禁止。

运行：SERIALREG_DB=sqlite pytest -q test_successions.py
"""
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, Issue, IssueNumber, Item, Title,
    TitleSuccession, TitleSuccessionEvent,
    establish_succession, lineage_chain_ids, replay_succession_events,
    revoke_succession,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def renamed_titles(db):
    """《老刊》ISSN 1000-0001 于 2024-01 更名为《新刊》ISSN 2000-0002。

    老刊名下：
      v.59 no.12 普通期 2023-12
      v.60 no.1 跨年卷，发行 2023-12 ~ 2024-02（截止月越过生效月，仍归老刊）
      v.60 no.1-2 合刊 2023-12 ~ 2024-01（同上，条码 CB-OLD）
    新刊名下：
      v.1 no.1 2024-01（生效月起的新发行）
    """
    old = Title.objects.create(title="老刊", issn="1000-0001")
    new = Title.objects.create(title="新刊", issn="2000-0002")

    # --- 老刊既有发行（生效月之前） ---
    n5912 = IssueNumber.objects.create(
        title=old, volume="59", number="12", sort_key=1)
    i5912 = Issue.objects.create(
        title=old, kind=Issue.IssueKind.REGULAR, issue_month="2023-12-01")
    i5912.numbers.add(n5912)
    Item.objects.create(barcode="OLD-5912", title=old, issue=i5912,
                        location="老现刊 A-01")

    # 跨年卷：一个编号，起始月在生效前，覆盖到生效后
    n601 = IssueNumber.objects.create(
        title=old, volume="60", number="1", sort_key=2)
    cross = Issue.objects.create(
        title=old, kind=Issue.IssueKind.REGULAR,
        issue_month="2023-12-01", issue_month_end="2024-02-01")
    cross.numbers.add(n601)
    Item.objects.create(barcode="OLD-601", title=old, issue=cross,
                        location="老现刊 A-01")

    # 合刊：条码覆盖两个期号，起始月在生效前
    n603 = IssueNumber.objects.create(
        title=old, volume="60", number="3", sort_key=3)
    n604 = IssueNumber.objects.create(
        title=old, volume="60", number="4", sort_key=4)
    combined = Issue.objects.create(
        title=old, kind=Issue.IssueKind.COMBINED,
        issue_month="2023-12-01", issue_month_end="2024-01-01",
        note="跨年合刊")
    combined.numbers.set([n603, n604])
    Item.objects.create(barcode="CB-OLD", title=old, issue=combined,
                        location="老现刊 A-02")

    # --- 新刊：生效月起的新发行 ---
    m11 = IssueNumber.objects.create(
        title=new, volume="1", number="1", sort_key=1)
    i_new = Issue.objects.create(
        title=new, kind=Issue.IssueKind.REGULAR, issue_month="2024-01-01")
    i_new.numbers.add(m11)
    Item.objects.create(barcode="NEW-101", title=new, issue=i_new,
                        location="新现刊 B-01")

    edge = establish_succession(
        predecessor=old, successor=new,
        effective_month=date(2024, 1, 1),
        note="期刊更名", actor="tester",
    )
    return {
        "old": old, "new": new, "edge": edge,
        "n5912": n5912, "n601": n601, "n603": n603, "n604": n604,
        "m11": m11, "combined": combined, "cross": cross,
    }


# ---------- 1) 两种检索视图 ----------

@pytest.mark.django_db
def test_search_with_lineage_shows_continuous_chain(renamed_titles, api):
    old, new = renamed_titles["old"], renamed_titles["new"]

    # 普通检索（仅当前刊名）：搜「老刊」只回老刊，彼此独立
    resp = api.get("/api/titles/?search=老刊")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == [old.id]
    assert "lineage_role" in rows[0]
    assert rows[0]["lineage_role"] is None  # 普通视图不带沿革徽标

    # 包含前身/后继：同一检索连续返回前刊与后刊，角色徽标正确
    resp = api.get("/api/titles/?search=老刊&include_lineage=1")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == [old.id, new.id]
    by_id = {r["id"]: r for r in rows}
    assert by_id[old.id]["lineage_role"] == "self"
    assert by_id[new.id]["lineage_role"] == "successor"
    # 原刊名与原 ISSN 保留
    assert by_id[old.id]["title"] == "老刊"
    assert by_id[old.id]["issn"] == "1000-0001"
    assert by_id[new.id]["lineage_effective_from"] == "2024-01-01"

    # 从新刊一侧查：前身是老刊
    resp = api.get("/api/titles/?search=新刊&include_lineage=1")
    rows = resp.json()
    assert [r["id"] for r in rows] == [old.id, new.id]
    by_id = {r["id"]: r for r in rows}
    assert by_id[old.id]["lineage_role"] == "predecessor"
    assert by_id[new.id]["lineage_role"] == "self"

    # ISSN 检索也能展开沿革
    resp = api.get("/api/titles/?search=2000-0002&include_lineage=1")
    assert {r["id"] for r in resp.json()} == {old.id, new.id}


@pytest.mark.django_db
def test_timeline_two_views(renamed_titles, api):
    old, new = renamed_titles["old"], renamed_titles["new"]

    # 仅当前刊名：顶层 slots 只有老刊自己的编号
    resp = api.get(f"/api/timeline/?title={old.id}")
    body = resp.json()
    assert body["title"]["id"] == old.id
    assert body["include_lineage"] is False
    assert {s["number"] for s in body["slots"]} == {"12", "1", "3", "4"}

    # 包含沿革：groups 里老/新两段分组，且实体仍各归各的刊名
    resp = api.get(f"/api/timeline/?title={old.id}&include_lineage=1")
    body = resp.json()
    groups = body["groups"]
    assert [g["title"]["id"] for g in groups] == [old.id, new.id]
    g_old, g_new = groups
    assert g_old["lineage_role"] == "self"
    assert g_new["lineage_role"] == "successor"
    old_barcodes = {
        it["barcode"]
        for s in g_old["slots"] for iss in s["issues"] for it in iss["items"]
    }
    new_barcodes = {
        it["barcode"]
        for s in g_new["slots"] for iss in s["issues"] for it in iss["items"]
    }
    assert old_barcodes == {"OLD-5912", "OLD-601", "CB-OLD"}
    assert new_barcodes == {"NEW-101"}
    # 顶层兼容字段仍只对应当前刊名
    assert body["title"]["id"] == old.id
    assert body["slots"] == g_old["slots"]


@pytest.mark.django_db
def test_locate_with_lineage_groups_by_title(renamed_titles, api):
    old, new = renamed_titles["old"], renamed_titles["new"]

    # 新刊的 no.1 与老刊跨年卷的 no.1 同号：不带卷号时沿沿革连续定位、分组归属
    resp = api.get(
        f"/api/items/locate/?title={new.id}&number=1&include_lineage=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["include_lineage"] is True
    # 老刊 v.60 no.1（跨年卷）与新刊 v.1 no.1 都命中，但分组不混合
    assert len(body["groups"]) == 2
    g_old = next(g for g in body["groups"] if g["title_id"] == old.id)
    g_new = next(g for g in body["groups"] if g["title_id"] == new.id)
    assert g_old["lineage_role"] == "predecessor"
    assert g_new["lineage_role"] == "self"
    assert [m["barcode"] for m in g_old["matches"]] == ["OLD-601"]
    assert [m["barcode"] for m in g_new["matches"]] == ["NEW-101"]
    assert g_old["title"] == "老刊" and g_old["issn"] == "1000-0001"

    # 带卷号精确定位时，只在沿革链上匹配该卷号的刊名段
    resp = api.get(
        f"/api/items/locate/?title={new.id}&volume=1&number=1&include_lineage=1")
    assert {g["title_id"] for g in resp.json()["groups"]} == {new.id}

    # 普通定位只看当前刊名：以新刊为锚只找新刊
    resp = api.get(f"/api/items/locate/?title={new.id}&volume=1&number=1")
    body = resp.json()
    assert [m["barcode"] for m in body["matches"]] == ["NEW-101"]

    # 条码反查：合刊条码带所属刊名，且可展开沿革
    resp = api.get("/api/items/locate/?barcode=CB-OLD&include_lineage=1")
    body = resp.json()
    m = body["matches"][0]
    assert m["title_id"] == old.id
    assert m["title"] == "老刊"
    assert {n["number"] for n in m["numbers"]} == {"3", "4"}
    assert {x["title_id"] for x in body["lineage"]} == {old.id, new.id}


# ---------- 2) 实体不随更名迁移：跨年卷 / 合刊 / 装订 ----------

@pytest.mark.django_db
def test_cross_year_and_combined_keep_original_title(renamed_titles):
    old, new = renamed_titles["old"], renamed_titles["new"]
    edge = renamed_titles["edge"]

    # 跨年卷 Issue 仍挂老刊，虽然 issue_month_end 越过了生效月
    cross = renamed_titles["cross"]
    cross.refresh_from_db()
    assert cross.title_id == old.id
    assert str(cross.issue_month_end) >= edge.effective_month.isoformat()
    # 编号槽位也没有被搬到新刊
    assert renamed_titles["n601"].title_id == old.id

    # 合刊条码仍属老刊
    cb = Item.objects.get(barcode="CB-OLD")
    assert cb.title_id == old.id
    assert cb.issue.title_id == old.id
    # 从合刊任一期号定位到的实物位置仍是老刊原位置（未装订时）
    from serials.models import locate_number
    barcodes = {r["barcode"] for r in locate_number(renamed_titles["n604"])}
    assert barcodes == {"CB-OLD"}

    # 新刊名下没有任何被迁过来的旧实体
    assert set(
        Item.objects.filter(title=new).values_list("barcode", flat=True)
    ) == {"NEW-101"}


@pytest.mark.django_db
def test_existing_binding_stays_on_original_title(renamed_titles, api):
    old, new = renamed_titles["old"], renamed_titles["new"]
    old_item = Item.objects.get(barcode="OLD-5912")
    cb_item = Item.objects.get(barcode="CB-OLD")

    # 更名前/后都允许在老刊内部装订（既有装订归属不变）
    binding = Binding.objects.create(
        call_number="Q/OLD-2023", title=old, location="老装订库 Z-1")
    BindingEntry.objects.bulk_create([
        BindingEntry(item=old_item, binding=binding,
                     previous_location="老现刊 A-01"),
        BindingEntry(item=cb_item, binding=binding,
                     previous_location="老现刊 A-02"),
    ])
    Item.objects.filter(barcode__in=["OLD-5912", "CB-OLD"]).update(
        status=Item.ItemStatus.BOUND)

    # 沿革视图的时间轴里，装订信息仍出现在老刊分组，新刊分组看不到
    resp = api.get(f"/api/timeline/?title={new.id}&include_lineage=1")
    groups = resp.json()["groups"]
    g_old = next(g for g in groups if g["title_id"] == old.id)
    bound = [
        it for s in g_old["slots"] for iss in s["issues"] for it in iss["items"]
        if it["bound"]
    ]
    assert {it["binding"] for it in bound} == {"Q/OLD-2023"}
    assert all(it["location"] == "老装订库 Z-1" for it in bound)

    # 沿革不放宽跨刊混装：新刊装订册不能加入老刊实物（取未装订的跨年卷实物）
    new_item = Item.objects.get(barcode="NEW-101")
    cross_item = Item.objects.get(barcode="OLD-601")
    resp = api.post("/api/bindings/", {
        "call_number": "Q/MIX-AFTER-RENAME", "title": new.id,
        "location": "X", "item_ids": [new_item.id, cross_item.id],
    }, format="json")
    assert resp.status_code == 400
    assert "混装" in str(resp.json())


@pytest.mark.django_db
def test_new_issue_boundary_assignment(renamed_titles, api):
    old, new = renamed_titles["old"], renamed_titles["new"]

    # 生效月起的新发行不能登记在老刊名下（即便编号是跨年卷延续）
    resp = api.post("/api/issues/", {
        "title": old.id, "kind": "regular",
        "issue_month": "2024-01-01",
        "number_ids": [
            IssueNumber.objects.create(
                title=old, volume="60", number="5", sort_key=5).id,
        ],
    }, format="json")
    assert resp.status_code == 400
    assert "更名" in str(resp.json())

    # 起始月在生效前、截止月跨到生效后的跨年卷仍可登记在老刊名下
    # （归属只看起始月，不看截止月，实体不迁移）
    n_reg = IssueNumber.objects.create(
        title=old, volume="60", number="6", sort_key=6)
    resp = api.post("/api/issues/", {
        "title": old.id, "kind": "regular",
        "issue_month": "2023-12-15",  # 日字段不影响：仍按 2023-12 月归属
        "issue_month_end": "2024-03-01",
        "number_ids": [n_reg.id],
    }, format="json")
    assert resp.status_code == 201, resp.json()

    # 生效月之前的发行不能登记在新刊名下
    m0 = IssueNumber.objects.create(
        title=new, volume="0", number="9", sort_key=0)
    resp = api.post("/api/issues/", {
        "title": new.id, "kind": "regular",
        "issue_month": "2023-12-01", "number_ids": [m0.id],
    }, format="json")
    assert resp.status_code == 400


# ---------- 3) 循环与矛盾后继拒绝，不影响已发布沿革 ----------

@pytest.mark.django_db
def test_cycle_and_conflict_rejected(renamed_titles, api):
    old, new, edge = renamed_titles["old"], renamed_titles["new"], renamed_titles["edge"]

    # 直接反向建边 new → old 会成环
    resp = api.post("/api/successions/", {
        "predecessor": new.id, "successor": old.id,
        "effective_month": "2025-01",
    }, format="json")
    assert resp.status_code == 400
    assert "循环" in str(resp.json())

    # 同一刊名不能有第二个矛盾后继：old → third
    third = Title.objects.create(title="第三刊", issn="3000-0003")
    resp = api.post("/api/successions/", {
        "predecessor": old.id, "successor": third.id,
        "effective_month": "2025-02",
    }, format="json")
    assert resp.status_code == 400
    assert "矛盾" in str(resp.json())

    # 一个新刊名不能有两个前身：fourth → new
    fourth = Title.objects.create(title="第四刊", issn="4000-0004")
    resp = api.post("/api/successions/", {
        "predecessor": fourth.id, "successor": new.id,
        "effective_month": "2025-02",
    }, format="json")
    assert resp.status_code == 400
    assert "矛盾" in str(resp.json())

    # 自环
    resp = api.post("/api/successions/", {
        "predecessor": third.id, "successor": third.id,
        "effective_month": "2025-02",
    }, format="json")
    assert resp.status_code == 400

    # 已发布沿革完全不受失败尝试影响
    edge.refresh_from_db()
    assert edge.is_active
    assert TitleSuccession.objects.filter(revoked_at__isnull=True).count() == 1
    assert TitleSuccessionEvent.objects.filter(
        action=TitleSuccessionEvent.Action.ESTABLISH).count() == 1
    # 检索视图仍然连续
    resp = api.get(f"/api/titles/?search=老刊&include_lineage=1")
    assert {r["id"] for r in resp.json()} == {old.id, new.id}


@pytest.mark.django_db
def test_long_cycle_rejected(db, api):
    """A→B→C 之后再建 C→A，应检测出多跳成环。"""
    t = [Title.objects.create(title=f"刊{i}") for i in range(3)]
    establish_succession(predecessor=t[0], successor=t[1],
                         effective_month=date(2024, 1, 1))
    establish_succession(predecessor=t[1], successor=t[2],
                         effective_month=date(2025, 1, 1))
    resp = api.post("/api/successions/", {
        "predecessor": t[2].id, "successor": t[0].id,
        "effective_month": "2026-01",
    }, format="json")
    assert resp.status_code == 400
    assert "循环" in str(resp.json())
    assert lineage_chain_ids(t[0].id) == [x.id for x in t]


@pytest.mark.django_db
def test_establish_rejects_boundary_conflict(db, api):
    """新边界不能把已有发行划到另一侧（实体不迁移的又一道闸）。"""
    old = Title.objects.create(title="边界老刊")
    new = Title.objects.create(title="边界新刊")
    n = IssueNumber.objects.create(title=old, volume="1", number="1")
    iss = Issue.objects.create(
        title=old, kind=Issue.IssueKind.REGULAR, issue_month="2024-03-01")
    iss.numbers.add(n)

    resp = api.post("/api/successions/", {
        "predecessor": old.id, "successor": new.id,
        "effective_month": "2024-01",  # 老刊已有 3 月发行，冲突
    }, format="json")
    assert resp.status_code == 400
    assert "生效月" in str(resp.json())
    assert TitleSuccession.objects.count() == 0


@pytest.mark.django_db
def test_effective_month_normalized_and_db_constraints(db):
    old = Title.objects.create(title="约束老刊")
    new = Title.objects.create(title="约束新刊")
    edge = establish_succession(
        predecessor=old, successor=new, effective_month=date(2024, 3, 18))
    assert edge.effective_month.day == 1
    assert edge.predecessor_title_snapshot == "约束老刊"
    assert edge.version == 1


@pytest.mark.django_db
def test_db_partial_unique_blocks_contradiction(db):
    """绕过服务层直接插库：部分唯一索引仍挡住矛盾后继（并发兜底）。"""
    from django.db import IntegrityError, transaction

    a = Title.objects.create(title="库A")
    b = Title.objects.create(title="库B")
    c = Title.objects.create(title="库C")
    establish_succession(predecessor=a, successor=b,
                         effective_month=date(2024, 1, 1))

    def raw_edge(p, s, month):
        return TitleSuccession(
            predecessor=p, successor=s, effective_month=month,
            predecessor_title_snapshot=p.title, successor_title_snapshot=s.title,
            predecessor_issn_snapshot=p.issn or "",
            successor_issn_snapshot=s.issn or "",
        )

    # 同一原刊名的第二条有效后继
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            raw_edge(a, c, date(2025, 1, 1)).save()
    # 同一后继刊名的第二条有效前身
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            raw_edge(c, b, date(2025, 1, 1)).save()
    # 已撤销后同对刊名可以有第二条（新版本行），唯一约束只针对在效边
    revoke_succession(succession=TitleSuccession.objects.get(
        predecessor=a, successor=b))
    raw_edge(a, b, date(2024, 1, 1), ).save()
    assert TitleSuccession.objects.filter(
        predecessor=a, successor=b).count() == 2
    assert TitleSuccession.objects.filter(
        predecessor=a, successor=b, revoked_at__isnull=True).count() == 1


# ---------- 4) 撤销：当前视图更新 + 审计可重放 + 跨刊混装仍禁止 ----------

@pytest.mark.django_db
def test_revoke_updates_current_view_and_replay(renamed_titles, api):
    old, new, edge = renamed_titles["old"], renamed_titles["new"], renamed_titles["edge"]

    # 撤销前沿革视图连续
    resp = api.get(f"/api/titles/?search=老刊&include_lineage=1")
    assert {r["id"] for r in resp.json()} == {old.id, new.id}

    resp = api.post("/api/successions/revoke/", {
        "succession": edge.id, "actor": "tester",
        "reason": "更名通知有误，撤销",
    }, format="json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is False
    assert body["revoked_at"] is not None
    # 行没有被覆盖删除，版本与快照仍在
    edge.refresh_from_db()
    assert edge.version == 1
    assert edge.predecessor_issn_snapshot == "1000-0001"

    # 当前视图立即更新：沿革检索回到各自独立
    resp = api.get(f"/api/titles/?search=老刊&include_lineage=1")
    assert {r["id"] for r in resp.json()} == {old.id}
    resp = api.get(f"/api/timeline/?title={old.id}&include_lineage=1")
    assert [g["title_id"] for g in resp.json()["groups"]] == [old.id]
    # 定位也不再跨刊
    resp = api.get(
        f"/api/items/locate/?title={new.id}&volume=1&number=1&include_lineage=1")
    assert {g["title_id"] for g in resp.json()["groups"]} == {new.id}

    # 审计里一建一撤两条事件
    events = TitleSuccessionEvent.objects.filter(succession=edge).order_by("id")
    assert [e.action for e in events] == [
        TitleSuccessionEvent.Action.ESTABLISH,
        TitleSuccessionEvent.Action.REVOKE,
    ]
    # 只凭事件重放，得出该沿革当前为 revoked（旧检索结果可复现/可解释）
    states = replay_succession_events(events)
    assert len(states) == 1
    assert states[0]["status"] == "revoked"
    assert states[0]["effective_month"] == "2024-01-01"
    # API 重放端点一致
    resp = api.get("/api/successions/replay/?title=%d" % old.id)
    assert resp.json()["states"][0]["status"] == "revoked"

    # 撤销后可按新版本重新建立（纠正沿革元信息），旧版本保留。
    # 两侧已有发行把生效月锚定在 2024-01（实体不迁移，边界不能越过既有发行），
    # 因此纠正以「同一生效月重新建边、更新备注」的方式走新版本。
    resp = api.post("/api/successions/", {
        "predecessor": old.id, "successor": new.id,
        "effective_month": "2024-01",
        "actor": "tester", "reason": "补正更名批文文号",
    }, format="json")
    assert resp.status_code == 201, resp.json()
    assert resp.json()["version"] == 2
    versions = list(
        TitleSuccession.objects.filter(
            predecessor=old, successor=new).order_by("version")
        .values_list("version", flat=True))
    assert versions == [1, 2]  # v1 已撤销但不被覆盖
    # 重放两条边的事件，折叠出当前有效状态
    resp = api.get("/api/successions/replay/")
    state = next(s for s in resp.json()["states"]
                 if s["predecessor_id"] == old.id
                 and s["successor_id"] == new.id)
    assert state["status"] == "active"
    assert state["effective_month"] == "2024-01-01"
    assert state["version"] == 2


@pytest.mark.django_db
def test_after_revoke_cross_title_binding_still_forbidden(renamed_titles, api):
    old, new, edge = renamed_titles["old"], renamed_titles["new"], renamed_titles["edge"]

    # 刷新页面（重新请求）后撤销，再试跨刊装订——仍然 400
    resp = api.post("/api/successions/revoke/", {"succession": edge.id},
                    format="json")
    assert resp.status_code == 200

    old_item = Item.objects.get(barcode="OLD-5912")
    new_item = Item.objects.get(barcode="NEW-101")
    resp = api.post("/api/bindings/", {
        "call_number": "Q/POST-REVOKE-MIX", "title": old.id,
        "location": "X", "item_ids": [old_item.id, new_item.id],
    }, format="json")
    assert resp.status_code == 400
    assert "混装" in str(resp.json())
    # 撤销沿革没有移动任何实体
    assert old_item.title_id == old.id
    assert new_item.title_id == new.id


@pytest.mark.django_db
def test_cannot_revoke_twice(renamed_titles, api):
    edge = renamed_titles["edge"]
    r1 = api.post("/api/successions/revoke/",
                  {"succession": edge.id}, format="json")
    assert r1.status_code == 200
    r2 = api.post("/api/successions/revoke/",
                  {"succession": edge.id}, format="json")
    assert r2.status_code == 400
    # 审计里只有一次撤销
    assert TitleSuccessionEvent.objects.filter(
        succession=edge, action=TitleSuccessionEvent.Action.REVOKE).count() == 1
