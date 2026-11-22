import { createRouter, createWebHistory } from 'vue-router'

import GapHandoffView from '@/views/GapHandoffView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 增量缺口重放清单含导出/导入/回执等专属交互，使用独立工作台。
    { path: '/incr_gap_handoff', name: 'gap-handoff', component: GapHandoffView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
