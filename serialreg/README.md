# 连续出版物登记应用（SerialReg）

管理连续出版物的「编号覆盖」与「实体位置」两层关系。典型场景：**一期合刊覆盖两个期号，
同时又是馆内不同的物理实体**——系统用四层模型同时表达，不允许用一个条码覆盖多个期号关系。

## 核心领域模型（PostgreSQL）

| 层 | 模型 | 含义 |
|---|---|---|
| 书目 | `Title` | 刊名、ISSN、出版状态；停刊需填**停刊月份** |
| 编号 | `IssueNumber` | 卷期编号槽位（卷+期唯一），**不含年月**，跨年卷只有一个编号 |
| 发行 | `Issue` + `IssueNumbering` | 一次出版行为。普通期关联 1 个编号，**两期合刊关联 ≥2 个独立编号记录** |
| 实体 | `Item` | 一条条码 = 一个实物，指向一个 `Issue`，多期号覆盖由关联表表达 |
| 装订 | `Binding` + `BindingEntry` | 多个实物订成一册，记录装订后位置并封存各实物原位置 |
| 沿革 | `TitleSuccession` + `TitleSuccessionEvent` | 带生效月份的刊名更名关系（版本化、可撤销）+ append-only 审计 |

关键业务规则：

- **发行年月与卷期编号分开录入**：跨年卷（如 v.60 no.3 印 2023-12～2024-01）
  编号仍是一条，发行覆盖区间记在 `Issue.issue_month / issue_month_end`。
- **缺号 ≠ 缺藏**：
  - `not_published`（在刊但无发行记录）/ `ceased_gap`（停刊月之后）= **缺号**，
    只表示没有发行，不自动判定缺藏；
  - `issued+missing` = **缺藏**：已发行但没有可用实物（未入藏或全部丢失）。
- **合刊**：一个实物 + 两条（或更多）`IssueNumbering`。从 no.3 或 no.4 都能定位到同一实物。
- **装订**：实物条码与多期号关系不变，实际位置改指向装订册；
  **拆订**后按封存的 `previous_location` 恢复各自位置，合刊编号关系依旧完整。
- 禁止跨刊混装、重复装订；`status=bound` 只能由装订/拆订流程设置。

## 刊名沿革（更名）

期刊更名后，读者希望一次检索看到前后刊名的连续沿革；但**馆藏、ISSN、装订和历史期号
不能被混成同一本刊物**。系统用「带生效月份的刊名沿革边」解决这对矛盾：

- **模型**：`TitleSuccession`（原刊名 → 后继刊名 + `effective_month`）是有向边，
  建边时封存**原刊名与原/新 ISSN 快照**；`TitleSuccessionEvent` 是 append-only 审计。
- **实体永不迁移**：生效月**之前**的 `Issue/Item/Binding` 永远挂在原 `Title`
  （跨年卷、合刊也一样——归属只看发行**起始月**，不看 `issue_month_end`）；
  生效月起的新发行必须登记在新刊名下，服务端在「建沿革」与「登记发行」两侧都做
  边界校验，拒绝会把既有发行划到另一刊名的生效月。
- **两种视图**（检索 / 时间轴 / 定位同一套开关）：
  - 默认 **仅当前刊名**：各刊名独立，互不混入；
  - `include_lineage=1`（前端「包含前身/后继」）：沿沿革链连续展示前身 → 当前 → 后继，
    但**按刊名分组**并带 `前身刊名/当前刊名/后继刊名` 徽标，编号、条码、ISSN、装订各自归属。
- **图约束**（PostgreSQL 部分唯一索引 + 服务端校验，SQLite 同样生效）：
  - 禁止自环与多跳成环；
  - 同一刊名同一时期最多一条有效后继、一条有效前身（矛盾后继直接拒绝，失败不留半成品，
    **不影响已发布沿革**）；
  - 生效月强制为月份第一天（`GeneratedField(ExtractDay)` + `CheckConstraint`）。
- **版本化纠正/撤销**：撤销只把边标记为 `revoked_at` 并写 `revoke` 审计，不删除旧记录；
  重新建立自动升 `version`。`GET /api/successions/replay/` 可**仅凭审计事件重放**
  出每对刊名的当前状态。撤销沿革不放宽任何实体规则——跨刊混装依旧被装订校验拒绝。

## 技术栈

- 后端：Django 5 + Django REST Framework（`backend/`）
- 数据库：PostgreSQL 16（本地无 PG 时可用 `SERIALREG_DB=sqlite` 跑开发/测试）
- 前端：Vue 3 + Vite（`frontend/`），馆员时间轴界面

## 快速启动

### Docker Compose（推荐，含 PostgreSQL 与样例数据）

```bash
cd serialreg
docker compose up --build
# 前端 http://localhost:5173   后端 http://localhost:8000/api/
```

后端容器启动时自动 `migrate` 并执行 `seed_sample`（跨年卷 / 停刊 / 两期合刊 + 装订样例）。

### 本地分别启动

```bash
# 后端
cd backend
pip install -r requirements.txt
SERIALREG_DB=postgres PGHOST=127.0.0.1 python manage.py migrate
SERIALREG_DB=postgres python manage.py seed_sample
SERIALREG_DB=postgres python manage.py runserver

# 前端
cd frontend
npm install && npm run dev     # http://localhost:5173 ，/api 代理到 8000
```

## 验证

```bash
# PostgreSQL
SERIALREG_DB=postgres pytest -q
# 无 PG 环境（SQLite，ORM/部分索引/检查约束通用）
SERIALREG_DB=sqlite pytest -q
```

28 条测试覆盖：跨年卷单编号跨两年、停刊必须填月份、缺号(`ceased_gap`/`not_published`)
不等于缺藏(`issued+missing`)、合刊保留两条编号关联、任一期号可定位、条码反查得到两个期号、
装订后从 no.3/no.4/no.5 均指向装订册、禁止跨刊混装与重复装订、拆订恢复原位置且关系完好、
合刊实物在两个槽位下重复提交装订时自动去重；以及刊名沿革四组验收（见 `serials/test_successions.py`）：

1. 设置更名生效月后，`include_lineage=1` 检索连续展示前后刊（角色徽标、原刊名/原ISSN保留），
   普通检索各自独立；时间轴按刊名分组，定位 `groups` 不混合实体；
2. 跨年卷历史期号、合刊条码、既有装订在沿革视图下仍归原刊名，新刊名不出现迁入实体；
   登记发行的边界校验只看起始月（跨年卷/合刊的截止月不触发迁移）；
3. 自环、多跳循环、矛盾后继/前身、与既有发行冲突的生效月全部被 400 拒绝，
   已发布沿革与审计不受失败尝试影响；
4. 撤销已使用沿革后当前视图立即更新（检索/时间轴/定位回到独立），审计可重放出
   `revoked` 状态，重建走新版本而不覆盖 v1；撤销后刷新再做跨刊装订仍被禁止。

手工端到端（样例数据）：

```bash
# 装订态下从合刊任一期号定位 → 装订库
curl "/api/items/locate/?title=3&volume=8&number=4"
# 拆订 → 恢复「现刊区 B-02」
curl -X POST /api/bindings/unbind/ -H "Content-Type: application/json" -d '{"binding_id":1}'
```

## 主要 API

| 方法/路径 | 说明 |
|---|---|
| `GET /api/titles/?search=` | 刊名检索（刊名/ISSN/出版者模糊匹配） |
| `GET /api/titles/?search=&include_lineage=1` | 沿革视图：命中间沿前身/后继连续展开，带 `lineage_role` 徽标 |
| `GET/POST /api/numbers/` | 卷期编号槽位 |
| `GET/POST /api/issues/` | 发行期；`kind=combined` 时 `number_ids` 至少 2 个；更名边界月按刊名校验归属 |
| `GET/POST /api/items/` | 入藏实物（条码+发行期+位置） |
| `GET /api/items/locate/?title=&volume=&number=` | 按期号定位实物/位置（缺号返回空匹配+状态） |
| `GET /api/items/locate/?title=&number=&include_lineage=1` | 沿革定位：链上各刊名分别成组返回 `groups`，实体不混 |
| `GET /api/items/locate/?barcode=` | 按条码反查（含合刊覆盖的全部期号与所属刊名/ISSN） |
| `GET/POST /api/bindings/` | 装订（同刊、未装订实物；更名不放宽跨刊混装限制） |
| `POST /api/bindings/unbind/` | 拆订，恢复各自位置 |
| `GET /api/timeline/?title=` | 时间轴（仅当前刊名）：编号槽位×发行×实物×停刊标记 |
| `GET /api/timeline/?title=&include_lineage=1` | 时间轴（沿革视图）：`groups` 按前身/当前/后继分组 |
| `GET/POST /api/successions/` | 沿革列表/建立（`predecessor`、`successor`、`effective_month=YYYY-MM`）；无 PATCH/DELETE |
| `POST /api/successions/revoke/` | 版本化撤销（写审计，不迁移实体） |
| `GET /api/successions/audit/?title=` | append-only 审计事件流（建立/撤销） |
| `GET /api/successions/replay/?title=` | 仅凭审计事件重放每对刊名沿革的当前状态 |

## 界面

- 顶部「**仅当前刊名 / 包含前身/后继**」视图开关，同时驱动侧边栏检索、时间轴与定位；
- 左侧刊种列表（含停刊月份徽标、沿革角色徽标）、刊名/ISSN 检索与新增刊种；
- **时间轴**：每个卷期一个节点，区分「已入藏 / 缺藏 / 缺号 / 停刊后缺号」，
  沿革视图下按前身 → 当前 → 后继分段，徽标与生效月标注清晰；
  展开显示发行年月区间、合刊徽标、实物条码与实际位置（装订后显示装订册）；
- 定位栏：按期号（合刊任一期号）或条码检索，可勾选「包含前身/后继」并按刊名分组展示；
- **刊名沿革面板**：选择后继刊种 + 生效月建立沿革、撤销（带原因）、查看审计与重放；
- 登记操作：编号槽位 → 发行期（普通/合刊，年月与编号分录）→ 入藏；
- 装订面板：勾选同刊未装订实物建装订册，一键拆订并显示各实物原位置。
