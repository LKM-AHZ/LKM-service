"""组合根：唯一允许同时 import ``app`` 与 ``auth`` 的顶层包。

承载进程入口（ASGI / worker / Prefect / seed）与装配逻辑（``boot.assemble``）。
业务包 app、认证包 auth、底座 core 三者都不 import boot。
"""
