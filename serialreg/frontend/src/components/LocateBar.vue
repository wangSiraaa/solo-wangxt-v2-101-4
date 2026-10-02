<template>
  <div class="panel">
    <h2>定位检索</h2>
    <div class="row">
      <label class="field"><b>卷</b>
        <input v-model="volume" placeholder="如 8（可空）" style="width: 110px" />
      </label>
      <label class="field"><b>期</b>
        <input v-model="number" placeholder="如 3" style="width: 90px"
               @keyup.enter="byNumber" />
      </label>
      <button @click="byNumber">按期号定位</button>
      <label class="checkline"
              title="勾选后沿更名沿革同时检索前身/后继，但分刊名分组展示">
        <input type="checkbox" v-model="includeLineage" />
        包含前身/后继
      </label>
      <span style="width: 8px"></span>
      <label class="field"><b>条码</b>
        <input v-model="barcode" placeholder="SY-8-34" style="width: 130px"
               @keyup.enter="byBarcode" />
      </label>
      <button class="ghost" @click="byBarcode">按条码查</button>
    </div>

    <div v-if="error" class="msg err">{{ error }}</div>

    <!-- 沿革视图：按刊名分组，绝不混成同一本刊物 -->
    <div v-if="groupResult" class="locate-result">
      <p class="muted">
        沿革检索命中 {{ groupResult.length }} 段刊名，按归属分组：
      </p>
      <div v-for="g in groupResult" :key="g.title_id"
           class="lineage-group" :class="g.lineage_role" style="margin-bottom:10px">
        <div class="lineage-head">
          <strong>{{ g.title }}</strong>
          <span class="muted">{{ g.issn || "无 ISSN" }}</span>
          <span class="badge" :class="roleMeta(g.lineage_role).cls">
            {{ roleMeta(g.lineage_role).label }}
          </span>
          <span class="badge" :class="badgeCls(g.holding_status)">
            {{ meta(g.holding_status).label }}
          </span>
          <span class="muted">
            v.{{ g.volume || "—" }} no.{{ g.number }}
            <template v-if="g.effective_from">
              ｜{{ g.effective_from.slice(0, 7) }} 起归该刊名
            </template>
          </span>
        </div>
        <p v-if="g.matches.length === 0" class="empty-hint">
          定位不到该刊名下的实物。
        </p>
        <div v-for="(m, i) in g.matches" :key="i" class="item-line">
          <code>{{ m.barcode }}</code>
          <span class="badge" :class="m.status === 'lost' ? 'missing' : 'ok'">
            {{ itemStatus[m.status] || m.status }}
          </span>
          <span class="loc">
            📍 {{ m.location || "（未排架）" }}
            <template v-if="m.bound">（装订册 {{ m.binding }}）</template>
          </span>
        </div>
      </div>
    </div>

    <!-- 仅当前刊名：原有单层结果 -->
    <div v-else-if="result" class="locate-result">
      <div class="row">
        <span class="badge" :class="badgeCls(result.holding_status)">
          {{ meta(result.holding_status).label }}
        </span>
        <span class="muted">{{ meta(result.holding_status).hint }}</span>
      </div>
      <p v-if="result.matches.length === 0" class="empty-hint">
        定位不到任何实物。若状态为「缺号」，表示没有发行记录，并非自动判定缺藏。
      </p>
      <div v-for="(m, i) in result.matches" :key="i" class="item-line">
        <code>{{ m.barcode }}</code>
        <span v-if="m.title" class="muted">《{{ m.title }}》{{ m.issn }}</span>
        <span class="badge" :class="m.status === 'lost' ? 'missing' : 'ok'">
          {{ itemStatus[m.status] || m.status }}
        </span>
        <span class="loc">
          📍 {{ m.location || "（未排架）" }}
          <template v-if="m.bound">（装订册 {{ m.binding }}）</template>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { api } from "../api.js";
import { HOLDING_STATUS, ITEM_STATUS, LINEAGE_ROLE } from "../status.js";

const props = defineProps({ titleId: [Number, String] });

const volume = ref("");
const number = ref("");
const barcode = ref("");
const includeLineage = ref(false);
const result = ref(null);
const groupResult = ref(null);
const error = ref("");
const itemStatus = ITEM_STATUS;

function meta(s) {
  return HOLDING_STATUS[s] || { label: s || "未登记", cls: "gap", hint: "" };
}
const badgeCls = (s) => meta(s).cls;
const roleMeta = (r) => LINEAGE_ROLE[r] || { label: r, cls: "gap" };

function reset() {
  result.value = null;
  groupResult.value = null;
  error.value = "";
}

async function byNumber() {
  reset();
  try {
    const data = await api.locate({
      title: props.titleId, volume: volume.value, number: number.value,
    }, includeLineage.value);
    if (includeLineage.value && data.groups) {
      groupResult.value = data.groups;
    } else {
      result.value = data;
    }
  } catch (e) {
    error.value = e.message;
  }
}
async function byBarcode() {
  reset();
  try {
    const data = await api.locate({ barcode: barcode.value }, includeLineage.value);
    const m = data.matches[0];
    result.value = m
      ? {
          holding_status:
            m.status === "lost" ? "issued+missing" : "issued+held",
          matches: data.matches,
        }
      : { holding_status: "unregistered", matches: [] };
  } catch (e) {
    error.value = e.message;
  }
}
</script>

<style scoped>
.checkline { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--muted); }
</style>
