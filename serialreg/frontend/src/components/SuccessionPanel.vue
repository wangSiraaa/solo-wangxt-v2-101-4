<template>
  <div class="panel">
    <h2>刊名沿革（前身 / 后继）</h2>
    <p class="muted">
      沿革只连接书目层：生效月前的旧发行、条码与装订永远留在原刊名下，
      不会因跨年卷或合刊迁移实体；原刊名与原 ISSN 在关系上留快照。
    </p>

    <!-- 当前沿革链（按生效月排序） -->
    <div v-for="(node, i) in chain" :key="node.title_id" class="chain-row">
      <span
        class="badge"
        :class="
          node.title_id === titleId
            ? 'ok'
            : node.index < ownIndex
              ? 'predecessor'
              : 'successor'
        "
      >
        {{
          node.title_id === titleId
            ? "当前刊名"
            : node.index < ownIndex
              ? "前身"
              : "后继"
        }}
      </span>
      <strong>{{ node.title }}</strong>
      <span class="muted">{{ node.issn || "无 ISSN" }}</span>
      <template v-if="node.effective_from">
        <span class="muted">更名生效于 {{ fmt(node.effective_from) }}</span>
      </template>
      <button
        v-if="node.title_id !== titleId"
        class="tiny ghost"
        @click="$emit('open-title', node.title_id)"
      >
        打开该刊
      </button>
    </div>
    <p v-if="chain.length <= 1" class="empty-hint">
      该刊暂无沿革关系，可在下方登记「更名后继」。
    </p>

    <h3>① 登记更名（带生效月份）</h3>
    <div class="row">
      <label class="field"><b>后继刊</b>
        <select v-model="form.successor">
          <option :value="null">选择已存在的刊名…</option>
          <option
            v-for="t in successorCandidates"
            :key="t.id"
            :value="t.id"
          >
            {{ t.title }}（{{ t.issn || "无 ISSN" }}）
          </option>
        </select>
      </label>
      <label class="field"><b>更名生效月</b>
        <input v-model="form.effective_month" type="month" />
      </label>
      <button @click="createRel">建立沿革</button>
    </div>
    <p class="muted">
      含生效当月：该月起的新发行应登在后继刊名下；生效月前的发行归属不变。
    </p>

    <!-- 已发布的沿革版本（含被纠正/撤销的历史版本） -->
    <h3>② 沿革版本与纠正 / 撤销</h3>
    <div
      v-for="rel in relations"
      :key="rel.id"
      class="rel-card"
      :class="rel.state"
    >
      <div class="row">
        <code class="badge" :class="stateCls(rel.state)">{{ stateLabel(rel.state) }}</code>
        <strong>{{ rel.predecessor_title_snapshot }}</strong>
        <span class="muted">[原 ISSN {{ rel.predecessor_issn_snapshot || "无" }}]</span>
        <span>→</span>
        <strong>{{ rel.successor_title }}</strong>
        <span class="muted">@ {{ fmt(rel.effective_month) }}</span>
        <span v-if="rel.superseded_by_id" class="muted">
          （被 v{{ rel.superseded_by_id }} 纠正）
        </span>
      </div>
      <div v-if="rel.state === 'active'" class="row" style="margin-top:6px">
        <label class="field"><b>纠正后继为</b>
          <select v-model="corrections[rel.id].successor">
            <option :value="rel.successor">保持《{{ rel.successor_title }}》</option>
            <option
              v-for="t in correctCandidates(rel)"
              :key="t.id"
              :value="t.id"
            >
              {{ t.title }}
            </option>
          </select>
        </label>
        <label class="field"><b>纠正生效月</b>
          <input v-model="corrections[rel.id].effective_month" type="month" />
        </label>
        <button class="ghost" @click="doCorrect(rel)">
          纠正（生成新版本，不覆盖）
        </button>
        <button class="danger" @click="doRevoke(rel)">撤销</button>
      </div>
      <div v-else-if="rel.state === 'revoked'" class="muted" style="margin-top:4px">
        撤销原因：{{ rel.revoked_reason || "（未填写）" }}；审计行保留，旧结果可重放。
      </div>
    </div>
    <p v-if="relations.length === 0" class="empty-hint">暂无沿革版本。</p>

    <h3>③ 审计事件（append-only，可重放）</h3>
    <div class="audit-list">
      <div v-for="e in audits" :key="e.id" class="audit-row">
        <span class="badge" :class="actionCls(e.action)">{{ e.action_label }}</span>
        <span class="muted">{{ e.created_at?.slice(0, 16).replace("T", " ") }}</span>
        <span>
          {{ e.predecessor_title_snapshot }}
          [{{ e.predecessor_issn_snapshot || "无 ISSN" }}]
          → {{ e.successor_title_now }} @ {{ fmt(e.effective_month) }}
        </span>
        <span class="muted">{{ e.detail }}</span>
      </div>
      <p v-if="audits.length === 0" class="empty-hint">暂无审计事件。</p>
    </div>
    <button class="ghost" @click="showReplay = !showReplay">
      {{ showReplay ? "隐藏重放结果" : "重放审计（查看当前版本映射）" }}
    </button>
    <pre v-if="showReplay && replay" class="replay">{{
      JSON.stringify(
        replay.events.map((x) => ({
          action: x.action,
          from: x.predecessor_title,
          to: x.successor_id,
          at: x.effective_month,
          current: x.state_after,
        })),
        null,
        1,
      )
    }}</pre>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({
  titleId: [Number, String],
  chain: { type: Array, default: () => [] },
  allTitles: { type: Array, default: () => [] },
});
const emit = defineEmits(["changed", "open-title"]);

const msg = ref(null);
function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 5000);
}
const fmt = (d) => (d ? String(d).slice(0, 7) : "");

const form = reactive({ successor: null, effective_month: "" });
const relations = ref([]);
const audits = ref([]);
const replay = ref(null);
const showReplay = ref(false);
const corrections = reactive({});

const ownIndex = computed(
  () => props.chain.find((n) => n.title_id === props.titleId)?.index ?? 0,
);
const chainIds = computed(() => new Set(props.chain.map((n) => n.title_id)));

// 后继候选：不能是自己，也不能已在链上（会成环）
const successorCandidates = computed(() =>
  props.allTitles.filter(
    (t) => t.id !== props.titleId && !chainIds.value.has(t.id),
  ),
);
function correctCandidates(rel) {
  return props.allTitles.filter(
    (t) => t.id !== rel.predecessor && t.id !== rel.successor,
  );
}

async function loadRelations() {
  try {
    const [rels, auditData] = await Promise.all([
      api.listSuccessions(props.titleId),
      api.successionAudits(props.titleId, true),
    ]);
    relations.value = rels;
    audits.value = auditData.events;
    replay.value = auditData;
    for (const r of rels) {
      if (r.state === "active" && !corrections[r.id]) {
        corrections[r.id] = {
          successor: r.successor,
          effective_month: fmt(r.effective_month),
        };
      }
    }
  } catch (e) {
    notify(e.message, true);
  }
}
watch(() => props.titleId, loadRelations, { immediate: true });

async function createRel() {
  if (!form.successor || !form.effective_month)
    return notify("后继刊与更名生效月必填。", true);
  try {
    await api.createSuccession({
      predecessor: props.titleId,
      successor: form.successor,
      effective_month: form.effective_month,
    });
    form.successor = null;
    form.effective_month = "";
    notify("沿革已建立：原刊名/原 ISSN 已留存快照。");
    await loadRelations();
    emit("changed");
  } catch (e) {
    notify(e.message, true);
  }
}

async function doCorrect(rel) {
  const c = corrections[rel.id];
  try {
    await api.correctSuccession(rel.id, {
      successor: c.successor,
      effective_month: c.effective_month,
    });
    notify("已生成纠正版本，旧版本与检索结果保留。");
    await loadRelations();
    emit("changed");
  } catch (e) {
    notify(e.message, true);
  }
}

async function doRevoke(rel) {
  const reason = window.prompt(`撤销《${rel.predecessor_title_snapshot} → ${rel.successor_title}》的原因（审计留痕）：`, "");
  if (reason === null) return;
  try {
    await api.revokeSuccession(rel.id, reason);
    notify("沿革已撤销，当前视图更新；审计仍可重放。");
    await loadRelations();
    emit("changed");
  } catch (e) {
    notify(e.message, true);
  }
}

const stateLabel = (s) =>
  ({ active: "生效中", superseded: "已被纠正", revoked: "已撤销" })[s] || s;
const stateCls = (s) =>
  ({ active: "ok", superseded: "gap", revoked: "missing" })[s] || "gap";
const actionCls = (a) =>
  ({ create: "ok", correct: "successor", revoke: "missing" })[a] || "gap";
</script>
