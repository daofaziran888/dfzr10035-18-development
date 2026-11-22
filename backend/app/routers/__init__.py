"""业务路由汇总：增量缺口重放清单专用路由须在通用资源路由之前注册，
否则固定路径 /api/incr-gap/... 会被动态路径 /api/{module} 抢先匹配。"""
from __future__ import annotations

from app.routers import gap_handoff, resource

ROUTERS = [gap_handoff, resource]
