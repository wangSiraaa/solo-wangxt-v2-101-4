"""刊名沿革验收测试：

1) 更名生效月后，lineage=1 检索连续展示前后刊；普通检索各自独立；
2) 跨年卷历史期号、合刊条码、既有装订仍保留原刊名归属（实体不迁移）；
3) 循环 / 同一时期矛盾后继被拒，且不影响已发布沿革；
4) 撤销已使用沿革后当前视图更新、审计可重放、跨刊装订仍被禁止。

另含：原刊名/原 ISSN 快照、纠正版本化、时间轴分组、时间边界（含当月）、
DB 层部分唯一索引。
"""
import datetime

import pytest
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, Issue, IssueNumber, Item, SuccessionAudit,
    Title, TitleSuccession, correct_succession, create_succession,
    lineage_chain, month_start, replay_successions, revoke_succession,
    title_at_month,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def renamed_pair(db):
    """《旧学报》ISSN 1000-0001 于 2020-01 更名为《新学报》ISSN 2000-0002。"""
    old = Title.objects.create(title="旧学报", issn="1000-0001")
    new = Title.objects.create(title="新学报", issn="2000-0002")
    rel = create_succession(
        predecessor=old, successor=new,
        effective_month=datetime.date(2020, 1, 1),
    )
    return old, new, rel


# --------------------------------------------------------------------------
# 验收 1：生效月 + 两种检索视图
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_effective_month_normalized_to_first_day(renamed_pair):
    old, new, rel = renamed_pair
    assert rel.effective_month.day == 1
    assert month_start("2020-06-18") == datetime.date(2020, 6, 1)


@pytest.mark.django_db
def test_search_lineage_shows_continuous_chain_current_view_independent(
        renamed_pair, api):
    old, new, rel = renamed_pair

    # 普通检索（仅当前刊名）：各自独立，只返回命中的那个 Title 实体
    resp = api.get("/api/titles/?q=旧学报")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["title"] for r in rows] == ["旧学报"]
    assert rows[0]["current_successor"]["title"] == "新学报"
    # 普通视图就是裸数组，不带 lineage_groups
    assert isinstance(rows, list)

    resp = api.get("/api/titles/?q=新学报")
    assert [r["title"] for r in resp.json()] == ["新学报"]

    # 包含沿革：一次检索连续展示前刊与后刊，但仍是两个独立刊名行
    resp = api.get("/api/titles/?q=旧学报&lineage=1")
    body = resp.json()
    assert body["view"] == "lineage"
    assert len(body["lineage_groups"]) == 1
    titles = body["lineage_groups"][0]["titles"]
    assert [t["title"] for t in titles] == ["旧学报", "新学报"]
    assert titles[0]["lineage_role"] == "current"
    assert titles[1]["lineage_role"] == "successor"
    # ISSN 是两个实体各自的，绝不混成一本刊
    assert [t["issn"] for t in titles] == ["1000-0001", "2000-0002"]
    assert titles[1]["effective_from"] == "2020-01-01"

    # 从后继侧检索，前身同样连续出现
    resp = api.get("/api/titles/?q=新学报&lineage=1")
    titles = resp.json()["lineage_groups"][0]["titles"]
    assert [t["title"] for t in titles] == ["旧学报", "新学报"]


@pytest.mark.django_db
def test_title_lineage_endpoint(renamed_pair, api):
    old, new, _ = renamed_pair
    resp = api.get(f"/api/titles/{new.id}/lineage/")
    chain = resp.json()["titles"]
    assert [(t["title"], t["index"]) for t in chain] == [
        ("旧学报", 0), ("新学报", 1),
    ]
    assert chain[1]["effective_from"] == "2020-01-01"


# --------------------------------------------------------------------------
# 验收 2：时间边界与实体不迁移（跨年卷 / 合刊 / 装订）
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_title_at_month_boundary_inclusive(renamed_pair):
    old, new, _ = renamed_pair
    # 生效月（含当月）起新发行归新刊；生效月之前归旧刊
    assert title_at_month(old.id, "2019-12") == old.id
    assert title_at_month(old.id, "2020-01") == new.id
    assert title_at_month(new.id, "2020-01") == new.id


@pytest.mark.django_db
def test_cross_year_issue_before_effective_month_keeps_old_title(api):
    """跨年卷 v.10 no.3 印 2019-12 ~ 2020-02，2020-01 更名：
    历史期号与实物仍 100% 归前身刊，不因跨年卷迁移。"""
    old = Title.objects.create(title="旧年报", issn="1111-1111")
    new = Title.objects.create(title="新年报", issn="2222-2222")
    create_succession(predecessor=old, successor=new,
                      effective_month=datetime.date(2020, 1, 1))

    n3 = IssueNumber.objects.create(title=old, volume="10", number="3", sort_key=3)
    cross = Issue.objects.create(
        title=old, issue_month="2019-12-01", issue_month_end="2020-02-01")
    cross.numbers.add(n3)
    item = Item.objects.create(barcode="CR-10-3", title=old, issue=cross,
                               location="旧刊架")

    # 实体外键仍是前身刊，时间轴普通视图只看得到旧刊自己的槽位
    item.refresh_from_db()
    assert item.title_id == old.id
    assert cross.title_id == old.id

    resp = api.get(f"/api/timeline/?title={old.id}")
    body = resp.json()
    assert body["view"] == "current"
    assert [s["number"] for s in body["slots"]] == ["3"]
    assert body["slots"][0]["issues"][0]["items"][0]["barcode"] == "CR-10-3"

    # 沿革视图：按刊名分成两组，旧刊的跨年卷出现在前身分组，不串到新刊
    resp = api.get(f"/api/timeline/?title={new.id}&lineage=1")
    groups = {g["title_id"]: g for g in resp.json()["groups"]}
    assert [s["number"] for s in groups[old.id]["slots"]] == ["3"]
    assert groups[new.id]["slots"] == []
    slot = groups[old.id]["slots"][0]
    assert slot["title_id"] == old.id  # 归属徽标
    assert slot["issues"][0]["items"][0]["title_id"] == old.id


@pytest.mark.django_db
def test_combined_barcode_before_effective_month_stays_on_old_title(api):
    """2019 年的两期合刊条码在更名后：沿革定位能连续展示，但条码实体仍归旧刊。"""
    old = Title.objects.create(title="旧双月", issn="3333-3333")
    new = Title.objects.create(title="新双月", issn="4444-4444")
    create_succession(predecessor=old, successor=new,
                      effective_month=datetime.date(2020, 1, 1))
    n3 = IssueNumber.objects.create(title=old, volume="8", number="3", sort_key=3)
    n4 = IssueNumber.objects.create(title=old, volume="8", number="4", sort_key=4)
    comb = Issue.objects.create(
        title=old, kind=Issue.IssueKind.COMBINED,
        issue_month="2019-11-01", issue_month_end="2019-12-01")
    comb.numbers.set([n3, n4])
    Item.objects.create(barcode="CM-8-34", title=old, issue=comb,
                        location="旧合刊位")

    # 普通定位：只在旧刊命中
    resp = api.get(f"/api/items/locate/?title={old.id}&volume=8&number=3")
    assert resp.json()["matches"][0]["barcode"] == "CM-8-34"
    # 从后继刊普通定位：找不到（编号属于旧刊）
    resp = api.get(f"/api/items/locate/?title={new.id}&volume=8&number=3")
    assert resp.status_code == 404

    # 沿革定位：从后继刊沿链回溯到前身刊，命中且标注为沿革命中
    resp = api.get(
        f"/api/items/locate/?title={new.id}&volume=8&number=3&lineage=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["view"] == "lineage"
    assert body["resolved_title_id"] == old.id
    match = body["matches"][0]
    assert match["barcode"] == "CM-8-34"
    assert match["title_id"] == old.id
    assert match["lineage_role"] == "lineage"
    # 条码反查仍得到旧刊归属与两个期号
    resp = api.get("/api/items/locate/?barcode=CM-8-34")
    m = resp.json()["matches"][0]
    assert m["title"] == "旧双月"
    assert {x["number"] for x in m["numbers"]} == {"3", "4"}


@pytest.mark.django_db
def test_existing_binding_remains_on_old_title_after_rename(api):
    """更名前已装订的旧刊合刊，装订册仍属旧刊，不允许借沿革把新刊实物混装进来。"""
    old = Title.objects.create(title="旧装订刊", issn="5555-5555")
    new = Title.objects.create(title="新装订刊", issn="6666-6666")
    create_succession(predecessor=old, successor=new,
                      effective_month=datetime.date(2020, 1, 1))

    n1 = IssueNumber.objects.create(title=old, volume="9", number="1", sort_key=1)
    iss1 = Issue.objects.create(title=old, issue_month="2019-03-01")
    iss1.numbers.add(n1)
    old_item = Item.objects.create(barcode="BD-9-1", title=old, issue=iss1,
                                   location="旧现刊区")
    binding = Binding.objects.create(
        call_number="Q/OLD-2019", title=old, location="旧装订库")
    BindingEntry.objects.create(item=old_item, binding=binding,
                                previous_location="旧现刊区")
    Item.objects.filter(id=old_item.id).update(status=Item.ItemStatus.BOUND)

    # 沿革后装订册的 title 外键没有任何迁移
    assert Binding.objects.get(call_number="Q/OLD-2019").title_id == old.id
    assert old_item.current_location() == "旧装订库"

    # 新刊 2020-02 入藏一本；旧刊另有一本未装订实物。
    # 即使有沿革，也不允许把两本不同刊的实物装进同一新装订册
    n2 = IssueNumber.objects.create(title=old, volume="9", number="2", sort_key=2)
    iss2 = Issue.objects.create(title=old, issue_month="2019-04-01")
    iss2.numbers.add(n2)
    old_item2 = Item.objects.create(barcode="BD-9-2", title=old, issue=iss2,
                                    location="旧现刊区")
    nn = IssueNumber.objects.create(title=new, volume="1", number="1", sort_key=1)
    niss = Issue.objects.create(title=new, issue_month="2020-02-01")
    niss.numbers.add(nn)
    new_item = Item.objects.create(barcode="BD-1-1", title=new, issue=niss,
                                   location="新现刊区")
    resp = api.post("/api/bindings/", {
        "call_number": "Q/MIXED", "title": old.id, "location": "X",
        "item_ids": [old_item2.id, new_item.id],
    }, format="json")
    assert resp.status_code == 400
    assert "混装" in str(resp.json())
    # 既有装订册完好，两本实物仍各自可用
    assert Binding.objects.filter(call_number="Q/OLD-2019").exists()
    old_item2.refresh_from_db()
    assert old_item2.status != Item.ItemStatus.BOUND


# --------------------------------------------------------------------------
# 验收 3：循环与矛盾后继被拒，且不影响已发布沿革
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_self_loop_rejected(db, api):
    t = Title.objects.create(title="自循环刊")
    resp = api.post("/api/successions/", {
        "predecessor": t.id, "successor": t.id, "effective_month": "2020-01",
    }, format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_cycle_rejected_and_published_relations_intact(api):
    a = Title.objects.create(title="甲刊")
    b = Title.objects.create(title="乙刊")
    c = Title.objects.create(title="丙刊")
    create_succession(predecessor=a, successor=b,
                      effective_month=datetime.date(2020, 1, 1))
    create_succession(predecessor=b, successor=c,
                      effective_month=datetime.date(2021, 1, 1))

    # 试图闭环 C -> A
    resp = api.post("/api/successions/", {
        "predecessor": c.id, "successor": a.id, "effective_month": "2022-01",
    }, format="json")
    assert resp.status_code == 400
    assert "循环" in str(resp.json())

    # 已发布沿革完全不受失败尝试影响
    chain = [n["title_id"] for n in lineage_chain(a.id)]
    assert chain == [a.id, b.id, c.id]
    assert TitleSuccession.objects.filter(
        state=TitleSuccession.State.ACTIVE).count() == 2


@pytest.mark.django_db
def test_conflicting_successor_same_period_rejected(api):
    a = Title.objects.create(title="甲报")
    b = Title.objects.create(title="乙报")
    d = Title.objects.create(title="丁报")
    create_succession(predecessor=a, successor=b,
                      effective_month=datetime.date(2020, 1, 1))
    resp = api.post("/api/successions/", {
        "predecessor": a.id, "successor": d.id, "effective_month": "2020-02",
    }, format="json")
    assert resp.status_code == 400
    assert "矛盾" in str(resp.json())
    # 当前后继仍是 B
    assert TitleSuccession.objects.get(
        predecessor=a, state=TitleSuccession.State.ACTIVE).successor_id == b.id


@pytest.mark.django_db
def test_db_partial_unique_index_blocks_two_active_rows(db):
    """直接绕过服务层插两条 active 行：PostgreSQL 部分唯一索引 / SQLite 都会拒绝。"""
    a = Title.objects.create(title="甲")
    b = Title.objects.create(title="乙")
    d = Title.objects.create(title="丁")
    create_succession(predecessor=a, successor=b,
                      effective_month=datetime.date(2020, 1, 1))
    rogue = TitleSuccession(
        predecessor=a, successor=d, effective_month=datetime.date(2020, 2, 1),
        predecessor_title_snapshot="甲", predecessor_issn_snapshot="",
        state=TitleSuccession.State.ACTIVE,
    )
    with pytest.raises(Exception):  # IntegrityError（两端一致）
        rogue.save()


# --------------------------------------------------------------------------
# 版本化纠正 / 撤销 + 审计重放
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_correction_is_versioned_not_overwritten(db):
    a = Title.objects.create(title="原名", issn="1000-0001")
    b = Title.objects.create(title="误登后继")
    d = Title.objects.create(title="正确后继")
    row = create_succession(predecessor=a, successor=b,
                            effective_month=datetime.date(2020, 1, 1))
    original_created = row.created_at

    new_row = correct_succession(row, successor=d,
                                 effective_month=datetime.date(2020, 3, 1))

    row.refresh_from_db()
    assert row.state == TitleSuccession.State.SUPERSEDED
    assert row.superseded_by_id == new_row.id
    assert new_row.state == TitleSuccession.State.ACTIVE
    assert new_row.successor_id == d.id
    assert new_row.effective_month == datetime.date(2020, 3, 1)
    # 快照原样继承，不覆盖
    assert new_row.predecessor_title_snapshot == "原名"
    assert new_row.predecessor_issn_snapshot == "1000-0001"
    # 审计有 create + correct 两条
    actions = list(SuccessionAudit.objects.values_list("action", flat=True))
    assert actions == ["create", "correct"]


@pytest.mark.django_db
def test_correct_into_cycle_rejected_and_keeps_active(db):
    a = Title.objects.create(title="A刊")
    b = Title.objects.create(title="B刊")
    c = Title.objects.create(title="C刊")
    row = create_succession(predecessor=a, successor=b,
                            effective_month=datetime.date(2020, 1, 1))
    create_succession(predecessor=b, successor=c,
                      effective_month=datetime.date(2021, 1, 1))
    # 把 B->C 纠正为 B->A：会形成 A->B->A 环，必须拒绝
    bc = TitleSuccession.objects.get(predecessor=b, state="active")
    with pytest.raises(ValidationError):
        correct_succession(bc, successor=a)
    bc.refresh_from_db()
    assert bc.state == TitleSuccession.State.ACTIVE  # 未被半更新
    # 已发布沿革链完好，且没有遗留 superseded 版本或多余审计
    assert [n["title_id"] for n in lineage_chain(a.id)] == [a.id, b.id, c.id]
    assert TitleSuccession.objects.filter(
        state=TitleSuccession.State.SUPERSEDED).count() == 0
    assert SuccessionAudit.objects.filter(action="correct").count() == 0
    # 经 API 路径同样返回 400 而非 500
    from rest_framework.test import APIClient
    resp = APIClient().post(
        f"/api/successions/{bc.id}/correct/",
        {"successor": a.id}, format="json")
    assert resp.status_code == 400
    assert "循环" in str(resp.json())


@pytest.mark.django_db
def test_revoke_used_succession_updates_view_and_audit_replays(api, renamed_pair):
    old, new, rel = renamed_pair

    # 沿革已被检索使用：在旧刊放历史数据
    n = IssueNumber.objects.create(title=old, volume="1", number="1", sort_key=1)
    iss = Issue.objects.create(title=old, issue_month="2019-05-01")
    iss.numbers.add(n)

    resp = api.post(f"/api/successions/{rel.id}/revoke/",
                    {"reason": "核实并无更名"}, format="json")
    assert resp.status_code == 200
    rel.refresh_from_db()
    assert rel.state == TitleSuccession.State.REVOKED

    # 当前视图：两刊不再属于同一链
    resp = api.get(f"/api/titles/{old.id}/lineage/")
    assert [t["title"] for t in resp.json()["titles"]] == ["旧学报"]
    resp = api.get("/api/titles/?q=旧学报&lineage=1")
    assert [t["title"] for t in
            resp.json()["lineage_groups"][0]["titles"]] == ["旧学报"]

    # 旧审计仍在，且可重放
    current, events = replay_successions(SuccessionAudit.objects.all())
    assert [e["action"] for e in events] == ["create", "revoke"]
    # 重放最终态：撤销后 A 没有后继
    assert current == {}
    # 只重放到撤销之前：A -> B 关系恢复
    current_before, _ = replay_successions(
        SuccessionAudit.objects.filter(action="create"))
    assert current_before == {old.id: rel.id}

    # 撤销接口的审计快照保留原刊名/原 ISSN
    revoke_event = SuccessionAudit.objects.filter(action="revoke").get()
    assert revoke_event.predecessor_title_snapshot == "旧学报"
    assert revoke_event.predecessor_issn_snapshot == "1000-0001"


@pytest.mark.django_db
def test_cross_title_binding_still_blocked_after_revoke(api, renamed_pair):
    """验收 4 收尾：撤销沿革刷新后，跨刊装订依旧被禁止。"""
    old, new, rel = renamed_pair
    # 两刊各一本实物
    n1 = IssueNumber.objects.create(title=old, volume="1", number="1", sort_key=1)
    i1 = Issue.objects.create(title=old, issue_month="2019-01-01")
    i1.numbers.add(n1)
    oi = Item.objects.create(barcode="O-1", title=old, issue=i1, location="O")
    n2 = IssueNumber.objects.create(title=new, volume="1", number="1", sort_key=1)
    i2 = Issue.objects.create(title=new, issue_month="2020-03-01")
    i2.numbers.add(n2)
    ni = Item.objects.create(barcode="N-1", title=new, issue=i2, location="N")

    api.post(f"/api/successions/{rel.id}/revoke/", {"reason": "x"},
             format="json")
    # 重新加载所有数据后再尝试混装
    resp = api.post("/api/bindings/", {
        "call_number": "Q/POST-REVOKE", "title": old.id, "location": "X",
        "item_ids": [oi.id, ni.id],
    }, format="json")
    assert resp.status_code == 400
    assert "混装" in str(resp.json())
    assert Binding.objects.count() == 0


# --------------------------------------------------------------------------
# 原刊名 / 原 ISSN 快照不随后续书目改写而变
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_snapshots_survive_bibliographic_rewrite(db, api):
    old = Title.objects.create(title="原始刊名", issn="1000-0001")
    new = Title.objects.create(title="后继刊名", issn="2000-0002")
    create_succession(predecessor=old, successor=new,
                      effective_month=datetime.date(2020, 1, 1))
    # 书目层后来被改写
    old.title = "被改写的刊名"
    old.issn = "9999-9999"
    old.save(update_fields=["title", "issn"])

    resp = api.get("/api/successions/?title=" + str(old.id))
    row = resp.json()[0]
    assert row["predecessor_title_snapshot"] == "原始刊名"
    assert row["predecessor_issn_snapshot"] == "1000-0001"
    # 现值仍可对照
    assert row["predecessor_title"] == "被改写的刊名"


@pytest.mark.django_db
def test_timeline_lineage_groups_ordered_with_boundaries(api, renamed_pair):
    old, new, _ = renamed_pair
    IssueNumber.objects.create(title=old, volume="1", number="1", sort_key=1)
    IssueNumber.objects.create(title=new, volume="2", number="1", sort_key=1)
    resp = api.get(f"/api/timeline/?title={old.id}&lineage=1")
    body = resp.json()
    assert body["view"] == "lineage"
    groups = body["groups"]
    assert [g["title"] for g in groups] == ["旧学报", "新学报"]
    assert groups[0]["effective_from"] is None
    assert groups[1]["effective_from"] == "2020-01-01"
    # slots 字段始终只代表请求刊名自身
    assert {s["title_id"] for s in body["slots"]} == {old.id}


@pytest.mark.django_db
def test_revoke_then_audit_endpoint_replay(api, renamed_pair):
    old, new, rel = renamed_pair
    api.post(f"/api/successions/{rel.id}/revoke/", {"reason": "误录"},
             format="json")
    resp = api.get(f"/api/successions/audits/?title={old.id}&replay=1")
    body = resp.json()
    assert [e["action"] for e in body["events"]] == ["create", "revoke"]
    assert body["replayed_current"] == {}
    labels = {e["action_label"] for e in body["events"]}
    assert labels == {"建立沿革", "撤销沿革"}
