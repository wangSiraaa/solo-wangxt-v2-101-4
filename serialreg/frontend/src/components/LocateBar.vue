<template>
  <div class="panel">
    <h2>
      定位检索
      <label class="muted" style="margin-left:10px;font-weight:normal">
        <input type="checkbox" v-model="lineage" />
        包含前身/后继（沿革视图）
      </label>
    </h2>
    <div class="row">
      <label class="field"><b>卷</b>
        <input v-model="volume" placeholder="如 8（可空）" style="width: 110px" />
      </label>
      <label class="field"><b>期</b>
        <input v-model="number" placeholder="如 3" style="width: 90px"
               @keyup.enter="byNumber" />
      </label>
      <button @click="byNumber">按期号定位</button>
      <span style="width: 18px"></span>
      <label class="field"><b>条码</b>
        <input v-model="barcode" placeholder="SY-8-34" style="width: 130px"
               @keyup.enter="byBarcode" />
      </label>
      <button class="ghost" @click="byBarcode">按条码查</button>
    </div>
    <p class="muted">
      普通检索只在当前刊名内查找；勾选沿革后，当前刊名查无此号时再沿
      前身/后继链逐刊查找——命中的条码仍标注其真实归属刊名，不会合并为同一本刊物。
    </p>

    <div v-if="error" class="msg err">{{ error }}</div>

    <div v-if="result" class="locate-result">
      <div class="row">
        <span class="badge" :class="badgeCls(result.holding_status)">
          {{ meta(result.holding_status).label }}
        </span>
        <span class="muted">{{ meta(result.holding_status).hint }}</span>
        <span
          v-if="result.view === 'lineage'"
          class="badge successor"
        >沿革视图</span>
      </div>
      <p v-if="result.matches.length === 0" class="empty-hint">
        定位不到任何实物。若状态为「缺号」，表示没有发行记录，并非自动判定缺藏。
      </p>
      <div v-for="(m, i) in result.matches" :key="i" class="item-line">
        <span
          v-if="m.lineage_role === 'lineage'"
          class="badge predecessor"
          :title="'该实物属于沿革链上的另一刊名，仅在沿革视图下连续展示'"
        >
          {{ m.title }}（沿革命中）
        </span>
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
</template>

<script setup>
import { ref } from "vue";
import { api } from "../api.js";
import { HOLDING_STATUS, ITEM_STATUS } from "../status.js";

const props = defineProps({ titleId: [Number, String] });

const volume = ref("");
const number = ref("");
const barcode = ref("");
const lineage = ref(false);
const result = ref(null);
const error = ref("");
const itemStatus = ITEM_STATUS;

function meta(s) {
  return HOLDING_STATUS[s] || { label: s || "未登记", cls: "gap", hint: "" };
}
const badgeCls = (s) => meta(s).cls;

async function byNumber() {
  error.value = "";
  result.value = null;
  try {
    result.value = await api.locate({
      title: props.titleId,
      volume: volume.value,
      number: number.value,
      lineage: lineage.value ? 1 : "",
    });
  } catch (e) {
    error.value = e.message;
  }
}
async function byBarcode() {
  error.value = "";
  result.value = null;
  try {
    const data = await api.locate({ barcode: barcode.value });
    const m = data.matches[0];
    result.value = m
      ? {
          view: "current",
          holding_status:
            m.status === "lost" ? "issued+missing" : "issued+held",
          matches: [m],
        }
      : { holding_status: "unregistered", matches: [] };
  } catch (e) {
    error.value = e.message;
  }
}
</script>
