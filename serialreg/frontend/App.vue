<template>
  <div class="app">
    <aside class="sidebar">
      <h1>连续出版物登记</h1>
      <p class="sub">期号覆盖 × 实体位置 × 装订册 × 刊名沿革</p>

      <button class="ghost" style="width:100%;margin-bottom:10px"
              @click="showNewTitle = !showNewTitle">
        {{ showNewTitle ? "收起" : "＋ 新增刊名" }}
      </button>
      <div v-if="showNewTitle" class="panel" style="padding:12px;margin-bottom:12px">
        <label class="field"><b>刊名</b>
          <input v-model="nt.title" placeholder="如 年鉴研究" />
        </label>
        <label class="field" style="margin-top:6px"><b>ISSN</b>
          <input v-model="nt.issn" placeholder="1001-0001" />
        </label>
        <label class="field" style="margin-top:6px"><b>出版状态</b>
          <select v-model="nt.status">
            <option value="active">在刊</option>
            <option value="ceased">停刊</option>
          </select>
        </label>
        <label v-if="nt.status === 'ceased'" class="field" style="margin-top:6px">
          <b>停刊月份</b>
          <input v-model="nt.ceased_month" type="month" />
        </label>
        <button style="margin-top:8px;width:100%" @click="createTitle">建立刊种</button>
        <p v-if="titleMsg" class="msg" :class="titleMsg.err ? 'err' : 'ok'">
          {{ titleMsg.text }}
        </p>
      </div>

      <label class="field" style="margin-bottom:8px"><b>检索刊名 / ISSN</b>
        <input v-model="keyword" placeholder="普通检索：仅命中刊名"
               @keyup.enter="searchTitles" />
      </label>
      <div class="row" style="margin-bottom:10px">
        <button class="tiny ghost" @click="searchTitles">检索</button>
        <button v-if="keyword" class="tiny ghost" @click="clearSearch">清除</button>
        <span class="muted">{{ titles.length }} 条</span>
      </div>

      <div
        v-for="t in titles"
        :key="t.id"
        class="title-item"
        :class="{ active: t.id === currentId }"
        @click="selectTitle(t.id)"
      >
        <div class="tname">
          {{ t.title }}
          <span v-if="t.lineage_role === 'predecessor'" class="badge lineage-pred">前身</span>
          <span v-else-if="t.lineage_role === 'successor'" class="badge lineage-succ">后继</span>
        </div>
        <div class="tmeta">
          {{ t.issn || "无 ISSN" }}
          <span v-if="t.status === 'ceased'" class="badge ceased">
            停刊 {{ t.ceased_month?.slice(0, 7) }}
          </span>
        </div>
      </div>
    </aside>

    <main class="main">
      <p v-if="!currentId" class="muted">请从左侧选择一种刊，或新增刊种。</p>

      <template v-else-if="timeline">
        <div class="row" style="margin-bottom:14px">
          <div class="viewtoggle">
            <button :class="{ on: !includeLineage }" @click="setView(false)">
              仅当前刊名
            </button>
            <button :class="{ on: includeLineage }" @click="setView(true)">
              包含前身/后继
            </button>
          </div>
          <span class="muted">
            {{ includeLineage
              ? "沿革视图：前后刊名连续展示，编号、ISSN、条码与装订仍按刊名分开"
              : "普通视图：各刊名独立，不混入沿革" }}
          </span>
        </div>

        <LocateBar :title-id="currentId" />

        <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap">
          <div style="flex:2;min-width:420px">
            <TimelineView :data="timeline" @mark-lost="markLost" />
          </div>
          <div style="flex:1;min-width:340px">
            <SuccessionPanel
              :title-id="currentId"
              :titles="allTitles"
              @changed="refresh"
            />
            <RegisterForms
              :title-id="currentId"
              :timeline="timeline"
              @changed="refresh"
            />
            <BindingPanel
              :title-id="currentId"
              :timeline="timeline"
              @changed="refresh"
            />
          </div>
        </div>
      </template>
      <p v-else class="muted">加载中…</p>
    </main>
  </div>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { api } from "./api.js";
import TimelineView from "./components/TimelineView.vue";
import LocateBar from "./components/LocateBar.vue";
import RegisterForms from "./components/RegisterForms.vue";
import BindingPanel from "./components/BindingPanel.vue";
import SuccessionPanel from "./components/SuccessionPanel.vue";

const titles = ref([]);       // 侧边栏（可能是沿革展开后的结果）
const allTitles = ref([]);    // 全量刊种，供沿革面板选后继
const currentId = ref(null);
const timeline = ref(null);
const showNewTitle = ref(false);
const titleMsg = ref(null);
const keyword = ref("");
const includeLineage = ref(false);

const nt = ref({ title: "", issn: "", status: "active", ceased_month: "" });

async function loadTitles(selectId = null) {
  const kw = keyword.value.trim();
  // 列表跟随当前视图：沿革模式下检索结果会展开前身/后继并带徽标
  titles.value = await api.listTitles(kw, includeLineage.value);
  allTitles.value = await api.listTitles("", false);
  if (selectId) currentId.value = selectId;
  else if (!currentId.value && titles.value.length)
    currentId.value = titles.value[0].id;
}

async function searchTitles() {
  await loadTitles(currentId.value);
}

async function clearSearch() {
  keyword.value = "";
  await loadTitles(currentId.value);
}

async function setView(v) {
  includeLineage.value = v;
  await refresh();
}

async function selectTitle(id) {
  currentId.value = id;
  await refresh();
}

// 数据变更后：按当前视图刷新左侧刊名与时间轴
async function refresh() {
  const [tl] = await Promise.all([
    api.timeline(currentId.value, includeLineage.value),
    loadTitles(currentId.value),
  ]);
  timeline.value = tl;
}

async function createTitle() {
  titleMsg.value = null;
  if (!nt.value.title) {
    titleMsg.value = { text: "刊名必填。", err: true };
    return;
  }
  try {
    const payload = {
      title: nt.value.title, issn: nt.value.issn, status: nt.value.status,
      ceased_month: nt.value.ceased_month
        ? `${nt.value.ceased_month}-01` : null,
    };
    const created = await api.createTitle(payload);
    nt.value = { title: "", issn: "", status: "active", ceased_month: "" };
    showNewTitle.value = false;
    await loadTitles(created.id);
    await refresh();
  } catch (e) {
    titleMsg.value = { text: e.message, err: true };
  }
}

// 报失：仅实物状态变更，编号的「缺号/缺藏」判定随之自动更新
async function markLost(itemId) {
  await api.setItemStatus(itemId, "lost");
  await refresh();
}

onMounted(refresh);
</script>
