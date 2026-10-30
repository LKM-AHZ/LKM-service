from fastapi import APIRouter

from app.modules import registry
from app.ws.router import router as ws_router
from core import route_registry

api_router = APIRouter()

# 业务 REST 出口由 registry.MODULES 汇总；模型与任务另在 app.bootstrap 登记。
for _name in registry.MODULES:
    for _r in registry.routers_of(_name):
        api_router.include_router(_r)

# boot.assemble 预先登记跨包路由（含 auth），此处只消费 core 注册表。
for _r in route_registry.extra_routers():
    api_router.include_router(_r)

# 非业务模块的横切路由（WebSocket）保持显式挂载。
api_router.include_router(ws_router)
