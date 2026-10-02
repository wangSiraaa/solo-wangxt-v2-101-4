# 连续出版物登记应用（SerialReg）

管理连续出版物的「编号覆盖」与「实体位置」两层关系。典型场景：**一期合刊覆盖两个期号，
同时又是馆内不同的物理实体**——系统用四层模型同时表达，不允许用一个条码覆盖多个期号关系。

## 核心领域模型（PostgreSQL）

| 层 | 模型 | 含义 |
|---|---|---|
| 书目 | `Title` | 刊名、ISSN、出版状态；停刊需填**停刊月份** |
| 沿革 | `TitleSuccession` + `SuccessionAudit` | 前身刊→后继刊的**版本化更名关系**，带生效月份与原刊名/原 ISSN 快照 |
| 编号 | `IssueNumber` | 卷期编号槽位（卷+期唯一），**不含年月**，跨年卷只有一个编号 |
| 发行 | `Issue` + `IssueNumbering` | 一次出版行为。普通期关联 1 个编号，**两期合刊关联 ≥2 个独立编号记录** |
| 实体 | `Item` | 一条条码 = 一个实物，指向一个 `Issue`，多期号覆盖由关联表表达 |
| 装订 | `Binding` + `BindingEntry` | 多个实物订成一册，记录装订后位置并封存各实物原位置 |

关键业务规则：

- **刊名沿革只连书目层，绝不迁移实体**：生效月份（含当月）之前的发行仍归前身刊，
  之后的新发行归后继刊；馆藏、ISSN、装订册、跨年卷历史期号都保留原 `Title` 外键，
  不会因为跨年卷或合刊被「改挂」到新刊名下。沿革行上保存**原刊名与原 ISSN 快照**。
- **两种检索视图**：普通检索（默认）仅当前刊名，前后刊各自独立；
  `lineage=1` 把沿革链上的前身/后继连续展示，但每个刊名仍独立成组、独立标注归属。
- **沿革关系是版本化的，不可覆盖**：纠正（换后继/改生效月）= 旧行置 `superseded`
  + 新建 `active` 行；撤销 = 置 `revoked`；两者都追加 `SuccessionAudit`，旧检索结果可重放。
- 沿革图不得成环（含自环）；同一前身刊在同一时期只能有一个生效中的后继
  （PostgreSQL 用 `WHERE state='active'` 部分唯一索引强制）；生效月份在 DB 层强制为当月 1 日。
- **发行年月与卷期编号分开录入**：跨年卷（如 v.60 no.3 印 2023-12～2024-01）
  编号仍是一条，发行覆盖区间记在 `Issue.issue_month / issue_month_end`。
- **缺号 ≠ 缺藏**：
  - `not_published`（在刊但无发行记录）/ `ceased_gap`（停刊月之后）= **缺号**，
    只表示没有发行，不自动判定缺藏；
  - `issued+missing` = **缺藏**：已发行但没有可用实物（未入藏或全部丢失）。
- **合刊**：一个实物 + 两条（或更多）`IssueNumbering`。从 no.3 或 no.4 都能定位到同一实物。
- **装订**：实物条码与多期号关系不变，实际位置改指向装订册；
  **拆订**后按封存的 `previous_location` 恢复各自位置，合刊编号关系依旧完整。
- 禁止跨刊混装、重复装订（沿革不改变这条限制——前身/后继实物仍不能装进同一册）；
  `status=bound` 只能由装订/拆订流程设置。

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
# 无 PG 环境（SQLite，ORM 通用）
SERIALREG_DB=sqlite pytest -q
```

33 条测试覆盖：跨年卷单编号跨两年、停刊必须填月份、缺号(`ceased_gap`/`not_published`)
不等于缺藏(`issued+missing`)、合刊保留两条编号关联、任一期号可定位、条码反查得到两个期号、
装订后从 no.3/no.4/no.5 均指向装订册、禁止跨刊混装与重复装订、拆订恢复原位置且关系完好、
合刊实物在两个槽位下重复提交装订时自动去重；
以及刊名沿革：生效月两种检索视图、时间边界（含当月）、跨年卷/合刊/装订实体不迁移、
循环与矛盾后继被拒且不影响已发布沿革、DB 部分唯一索引、版本化纠正、
撤销后当前视图更新+审计重放+跨刊装订仍禁止、原刊名/原 ISSN 快照不被书目改写覆盖。

手工端到端（样例数据）：

```bash
# 装订态下从合刊任一期号定位 → 装订库
curl "/api/items/locate/?title=3&volume=8&number=4"
# 拆订 → 恢复「现刊区 B-02」
curl -X POST /api/bindings/unbind/ -H "Content-Type: application/json" -d '{"binding_id":1}'

# 刊名沿革：《学报旧版》2023-01 更名《学报新版》
curl "/api/titles/?q=学报&lineage=1"                       # 一次检索连续展示前后刊
curl "/api/items/locate/?title=<新版id>&volume=20&number=6&lineage=1"  # 沿革命中旧版实物
curl "/api/successions/audits/?title=<旧版id>&replay=1"   # 重放审计事件
```

## 主要 API

| 方法/路径 | 说明 |
|---|---|
| `GET/POST /api/titles/` | 刊名；停刊须带 `ceased_month`；`?q=` 按刊名/ISSN 检索 |
| `GET /api/titles/?q=&lineage=1` | **包含沿革检索**：按更名链连续返回 `lineage_groups`，刊名各自独立 |
| `GET /api/titles/{id}/lineage/` | 该刊所在沿革链（按生效月排序，含边界） |
| `GET/POST /api/successions/` | 沿革版本（只读列表 + 建立；POST 快照原刊名/原 ISSN） |
| `POST /api/successions/{id}/correct/` | 纠正：旧行 `superseded` + 新版本 + 审计（可改后继、生效月） |
| `POST /api/successions/{id}/revoke/` | 撤销：行置 `revoked` 并审计，不删除历史 |
| `GET /api/successions/audits/?title=&replay=1` | 审计事件流与重放后的当前版本映射 |
| `GET/POST /api/numbers/` | 卷期编号槽位 |
| `GET/POST /api/issues/` | 发行期；`kind=combined` 时 `number_ids` 至少 2 个；`?title=&lineage=1` 可展开发行 |
| `GET/POST /api/items/` | 入藏实物（条码+发行期+位置） |
| `GET /api/items/locate/?title=&volume=&number=` | 按期号定位（缺号返回空匹配+状态） |
| `GET /api/items/locate/?...&lineage=1` | 沿革命中标注 `lineage_role` 与真实归属刊名，不合并实体 |
| `GET /api/items/locate/?barcode=` | 按条码反查（含合刊覆盖的全部期号、归属刊名） |
| `GET/POST /api/bindings/` | 装订（同刊、未装订实物；前身/后继刊之间也禁止混装） |
| `POST /api/bindings/unbind/` | 拆订，恢复各自位置 |
| `GET /api/timeline/?title=` | 时间轴（仅当前刊名）：编号槽位×发行×实物×停刊标记 |
| `GET /api/timeline/?title=&lineage=1` | 沿革时间轴：额外按刊名返回 `groups`，`slots` 仍只是请求刊名自身 |

## 界面

- 左侧刊种列表（含停刊月份、前身/后继徽标）、刊名/ISSN 检索与「包含前身/后继」开关；
- **沿革面板**：建立带生效月的更名关系、版本化纠正/撤销、append-only 审计与重放；
- **时间轴**：两种视图——「仅当前刊名」与「包含前身/后继」（按刊名分组、带前身/后继/归属徽标），
  每个卷期一个节点，区分「已入藏 / 缺藏 / 缺号 / 停刊后缺号」，
  展开显示发行年月区间、合刊徽标、实物条码与实际位置（装订后显示装订册）；
- 定位栏：按期号（合刊任一期号）或条码检索，可勾选沿革沿链查找；
- 登记操作：编号槽位 → 发行期（普通/合刊，年月与编号分录）→ 入藏；
- 装订面板：勾选同刊未装订实物建装订册，一键拆订并显示各实物原位置。
