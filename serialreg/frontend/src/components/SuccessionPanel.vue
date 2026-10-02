<template>
  <div class="panel">
    <h2>刊名沿革（更名）</h2>
    <p class="muted" style="margin-top:-6px">
      沿革只建立书目关系：原刊名、原 ISSN、历史期号、合刊条码与装订一律不迁移；
      生效月前的发行归原刊名，生效月后的新发行归新刊名。
    </p>

    <h3>① 登记更名（带生效月份）</h3>
    <div class="row">
      <label class="field"><b>后继刊种</b>
        <select v-model="form.successor">
          <option :value="null">请选择新刊名（不同的刊种）</option>
          <option v-for="t in otherTitles" :key="t.id" :value="t.id">
            {{ t.title }}（{{ t.issn || "无 ISSN" }}）
          </option>
        </select>
      </label>
      <label class="field"><b>更名生效月份</b>
        <input v-model="form.effective_month" type="month" />
      </label>
      <label class="field"><b>备注</b>
        <input v-model="form.note" placeholder="批文/说明（可空）" style="width:170px" />
      </label>
      <button @click="doEstablish">建立沿革</button>
    </div>

    <h3 v-if="successions.length">② 已记录沿革（含已撤销版本）</h3>
    <div class="binding-list">
      <div v-for="s in successions" :key="s.id" class="binding-card">
        <div class="row">
          <strong>{{ s.predecessor_title_snapshot }}</strong>
          <span class="lineage-arrow">→</span>
          <strong>{{ s.successor_title_snapshot }}</strong>
          <span class="badge" :class="s.is_active ? 'ok' : 'revoked'">
            {{ s.is_active ? "生效中" : "已撤销" }}
          </span>
          <span class="muted">
            v{{ s.version }} ｜ 生效 {{ s.effective_month?.slice(0, 7) }}
          </span>
          <span class="muted">
            原ISSN {{ s.predecessor_issn_snapshot || "无" }} →
            新ISSN {{ s.successor_issn_snapshot || "无" }}
          </span>
          <button v-if="s.is_active" class="tiny danger"
                  @click="doRevoke(s)">撤销</button>
        </div>
        <div v-if="s.note" class="muted" style="margin-top:4px">备注：{{ s.note }}</div>
      </div>
    </div>

    <h3>③ 审计与重放</h3>
    <div class="row">
      <button class="ghost" @click="loadAudit">查看审计事件</button>
      <button class="ghost" @click="loadReplay">按审计重放当前状态</button>
    </div>

    <div v-if="audit.length" class="binding-list" style="margin-top:8px">
      <div v-for="e in audit" :key="e.id" class="binding-card">
        <div class="row">
          <span class="badge" :class="e.action === 'establish' ? 'ok' : 'revoked'">
            {{ e.action_label }} v{{ e.version }}
          </span>
          <code>{{ e.created_at?.slice(0, 16).replace("T", " ") }}</code>
          <span v-if="e.payload?.effective_month" class="muted">
            生效月 {{ e.payload.effective_month.slice(0, 7) }}
            ｜《{{ e.payload.predecessor_title }}》
            （{{ e.payload.predecessor_issn || "无 ISSN" }}）
            → 《{{ e.payload.successor_title }}》
            （{{ e.payload.successor_issn || "无 ISSN" }}）
          </span>
          <span v-if="e.reason" class="muted">原因：{{ e.reason }}</span>
        </div>
      </div>
    </div>

    <div v-if="replay.length" class="binding-list" style="margin-top:8px">
      <p class="muted">仅凭审计事件折叠重放：</p>
      <div v-for="r in replay" :key="r.key" class="binding-card">
        <div class="row">
          <strong>{{ r.key }}</strong>
          <span class="badge" :class="r.status === 'active' ? 'ok' : 'revoked'">
            {{ r.status === "active" ? "重放：生效中" : "重放：已撤销" }}
          </span>
          <span class="muted">
            v{{ r.version }} ｜ 生效 {{ r.effective_month?.slice(0, 7) }}
          </span>
        </div>
      </div>
    </div>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({
  titleId: [Number, String],
  // 全部刊种，来自侧边栏，用于选后继
  titles: { type: Array, default: () => [] },
});
const emit = defineEmits(["changed"]);

const form = reactive({
  successor: null, effective_month: "", note: "",
});
const successions = ref([]);
const audit = ref([]);
const replay = ref([]);
const msg = ref(null);

const otherTitles = computed(() =>
  props.titles.filter((t) => t.id !== Number(props.titleId)),
);

function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 5000);
}

async function load() {
  audit.value = [];
  replay.value = [];
  try {
    successions.value = await api.listSuccessions(props.titleId);
  } catch (e) { /* 非关键路径 */ }
}
watch(() => props.titleId, load, { immediate: true });

async function doEstablish() {
  if (!form.successor || !form.effective_month)
    return notify("请选择后继刊种并填写生效月份。", true);
  try {
    await api.createSuccession({
      predecessor: Number(props.titleId),
      successor: form.successor,
      effective_month: form.effective_month, // 'YYYY-MM'，后端按当月 1 日
      note: form.note,
    });
    form.successor = null; form.effective_month = ""; form.note = "";
    notify("沿革已建立：生效月前的发行仍归原刊名，馆藏实体不迁移。");
    await load();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function doRevoke(s) {
  const reason = window.prompt(
    `撤销《${s.predecessor_title_snapshot}》→《${s.successor_title_snapshot}》` +
    "（版本化撤销，旧记录与审计保留，不迁移任何实体）。撤销原因：",
    "",
  );
  if (reason === null) return;
  try {
    await api.revokeSuccession(s.id, reason);
    notify("沿革已撤销，当前视图已更新；可在审计中查看并按事件重放。");
    await load();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function loadAudit() {
  try {
    audit.value = await api.successionAudit(props.titleId);
    if (!audit.value.length) notify("暂无审计事件。");
  } catch (e) { notify(e.message, true); }
}

async function loadReplay() {
  try {
    replay.value = (await api.replaySuccessions(props.titleId)).states;
    if (!replay.value.length) notify("暂无可重放事件。");
  } catch (e) { notify(e.message, true); }
}
</script>
