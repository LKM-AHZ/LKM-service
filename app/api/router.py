from fastapi import APIRouter

from app.modules import registry
from app.ws.router import router as ws_router
from core import route_registry

api_router = APIRouter()

# business REST 路由由注册表驱动（§7）：新增模块只动 registry.MODULES，本文件零改动。
for _name in registry.MODULES:
    for _r in registry.routers_of(_name):
        api_router.include_router(_r)

# 跨包路由（auth 前台认证面等）由各包 bootstrap 注册进 core.route_registry；
# 本文件不 import 任何业务域外的包。装配根 boot.backend 保证登记早于此处取用。
for _r in route_registry.extra_routers():
    api_router.include_router(_r)

# 非业务模块的横切路由（WebSocket）保持显式挂载。
api_router.include_router(ws_router)
