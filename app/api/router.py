from fastapi import APIRouter

from app.modules import registry
from app.ws.router import router as ws_router

api_router = APIRouter()

# 业务 REST 出口由 registry.MODULES 汇总；模型与任务另在 app.bootstrap 登记。
for _name in registry.MODULES:
    for _r in registry.routers_of(_name):
        api_router.include_router(_r)

# 非业务模块的横切路由（WebSocket）保持显式挂载。auth 路由只由独立 AUTH 进程提供。
api_router.include_router(ws_router)
