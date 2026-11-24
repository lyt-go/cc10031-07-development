"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

事件日交接与迁冷接口注册在通用资源路由之前，避免被 ``/api/{module}`` 抢先匹配。
"""
from __future__ import annotations

from app.routers import handover, resource

ROUTERS = [handover, resource]
