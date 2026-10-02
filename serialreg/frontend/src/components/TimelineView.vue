<template>
  <div class="panel">
    <h2>
      期号时间轴
      <span class="muted">
        —— {{ data.title.title }}
        <span v-if="data.title.status === 'ceased'" class="badge ceased">
          停刊于 {{ data.title.ceased_month?.slice(0, 7) }}
        </span>
      </span>
    </h2>

    <!-- 仅当前刊名：保持原有单层时间轴 -->
    <template v-if="!data.include_lineage">
      <div class="timeline">
        <SlotCard
          v-for="slot in data.slots"
          :key="slot.number_id"
          :slot="slot"
          @mark-lost="(id) => $emit('mark-lost', id)"
        />
      </div>
    </template>

    <!-- 包含前身/后继：按刊名分组，每组带角色徽标，编号/条码/装订不混排 -->
    <template v-else>
      <p class="muted" style="margin-top:-4px">
        沿革视图：前身 → 当前 → 后继连续展示；编号槽位、条码与装订各自归属原刊名，
        更名不迁移实体。
      </p>
      <div
        v-for="g in data.groups"
        :key="g.title_id"
        class="lineage-group"
        :class="g.lineage_role"
      >
        <div class="lineage-head">
          <strong style="font-size:15px">{{ g.title.title }}</strong>
          <span class="muted">{{ g.title.issn || "无 ISSN" }}</span>
          <span class="badge" :class="roleMeta(g.lineage_role).cls">
            {{ roleMeta(g.lineage_role).label }}
          </span>
          <span v-if="g.effective_from" class="muted">
            自 {{ g.effective_from.slice(0, 7) }} 起归该刊名
          </span>
        </div>
        <div class="timeline">
          <SlotCard
            v-for="slot in g.slots"
            :key="`${g.title_id}-${slot.number_id}`"
            :slot="slot"
            @mark-lost="(id) => $emit('mark-lost', id)"
          />
          <p v-if="g.slots.length === 0" class="empty-hint">
            该刊名下尚无编号槽位。
          </p>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import SlotCard from "./SlotCard.vue";
import { LINEAGE_ROLE } from "../status.js";

defineProps({ data: Object });
defineEmits(["mark-lost"]);

function roleMeta(role) {
  return LINEAGE_ROLE[role] || { label: role, cls: "gap" };
}
</script>
