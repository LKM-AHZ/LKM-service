from fastapi import APIRouter

from app.modules import registry
from app.ws.router import router as ws_router
from auth import ROUTERS as _auth_routers

api_router = APIRouter()

# business REST 路由由注册表驱动（§7）：新增模块只动 registry.MODULES，本文件零改动。
for _name in registry.MODULES:
    for _r in registry.routers_of(_name):
        api_router.include_router(_r)

# auth 已独立成顶层包，不在 registry.MODULES 内；前台认证面经其公开面显式挂载。
for _r in _auth_routers:
    api_router.include_router(_r)

# 非业务模块的横切路由（WebSocket）保持显式挂载。
api_router.include_router(ws_router)
