<template>
  <div class="panel">
    <h2>
      期号时间轴
      <span class="muted">
        —— {{ data.title.title }}
        <span v-if="data.title.status === 'ceased'" class="badge ceased">
          停刊于 {{ data.title.ceased_month?.slice(0, 7) }}
        </span>
        <span class="badge" :class="isLineage ? 'successor' : 'gap'">
          {{ isLineage ? "包含前身/后继" : "仅当前刊名" }}
        </span>
      </span>
    </h2>

    <!-- 沿革视图：每个刊名一个分组，实体不串刊混排 -->
    <template v-if="isLineage">
      <div v-for="g in data.groups" :key="g.title_id" class="lineage-group">
        <div class="group-head">
          <span
            class="badge"
            :class="
              g.title_id === currentTitleId
                ? 'ok'
                : isOlder(g)
                  ? 'predecessor'
                  : 'successor'
            "
          >
            {{
              g.title_id === currentTitleId
                ? "当前刊名"
                : isOlder(g)
                  ? "前身刊"
                  : "后继刊"
            }}
          </span>
          <strong>{{ g.title }}</strong>
          <span class="muted">{{ g.issn || "无 ISSN" }}</span>
          <span v-if="g.effective_from" class="muted">
            （更名生效于 {{ g.effective_from.slice(0, 7) }}）
          </span>
          <span v-if="g.status === 'ceased'" class="badge ceased">
            停刊 {{ g.ceased_month?.slice(0, 7) }}
          </span>
        </div>

        <div v-if="g.slots.length === 0" class="empty-hint">
          该沿革分组下暂无期号（馆藏仍独立归属本刊名，未迁移）。
        </div>
        <TimelineSlots :slots="g.slots" @mark-lost="$emit('mark-lost', $event)" />
      </div>
    </template>

    <!-- 普通视图：只显示请求刊名自身的槽位 -->
    <TimelineSlots v-else :slots="data.slots" @mark-lost="$emit('mark-lost', $event)" />
  </div>
</template>

<script setup>
import { computed } from "vue";
import { HOLDING_STATUS, ITEM_STATUS } from "../status.js";
import TimelineSlots from "./TimelineSlots.vue";

const props = defineProps({ data: Object });
defineEmits(["mark-lost"]);

const isLineage = computed(() => props.data.view === "lineage");
const currentTitleId = computed(() => props.data.title.id);
const ownIndex = computed(
  () => props.data.lineage?.find((n) => n.title_id === props.data.title.id)
    ?.index ?? 0,
);
function isOlder(group) {
  const node = props.data.lineage?.find(
    (n) => n.title_id === group.title_id,
  );
  return node ? node.index < ownIndex.value : false;
}
</script>
