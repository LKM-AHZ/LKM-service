"""uvicorn 入口（``uvicorn main:app``）：转交组合根 ``boot.backend``。

装配（app 与 auth 的注册项汇总、端口绑定）在 ``boot`` 里完成，故 app 侧不再 import auth。
"""

from boot.backend import app
