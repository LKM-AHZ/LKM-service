"""模型预注册中心：导入全部模块 ``models.py``，供 SQLAlchemy registry 解析字符串关系/外键。

历史角色由 ``app/db/models.py`` 巨型文件承担（单文件 import 即带出全部模型）。模型归位
（计划 §5，P1）后，各模型分散到各模块 ``models.py``，本模块作为新的"导入枢纽"——
任何需要全量模型注册的入口（init_db/create_all、worker 进程、Alembic env）只要
``from app.db.model_registry import ensure_all_models`` 即可。

``Base.registry.configure()`` 必须在全部模型注册后仅调用一次；重复调用会抛
``InvalidRequestError``，故用全局标志幂等。
"""

from __future__ import annotations

import app.db.base as _base_module


def ensure_all_models() -> None:
    """预注册全部 ORM 模型并完成 mapper 配置（幂等）。

    副作用：import 所有业务模块的 models.py（经各模块包级导入），使 metadata 填满、
    relationship 字符串引用得以解析。随后 configure() 锁定配置。
    """
    import app.db.event_failure  # outbox 发布耗竭归档(event_failures)
    import app.db.event_processed  # 消费者幂等去重表(event_processed)
    import app.db.outbox  # 共享基础设施表(outbox_events)，非业务模块性质
    import app.db.outbox_archive  # outbox 已发布冷表(outbox_archived)；M6.3
    import app.db.user_dim  # 离线报表宽表(user_dim)，auth源只读反范式副本；B0.1 纯建表
    import app.modules.admin.models
    import app.modules.articles.models

    # S5 拆库：auth 表挂独立 auth 元数据（AuthBase），不混入 Base.metadata（业务库）。
    import app.modules.auth.models
    import app.modules.blog.models
    import app.modules.content.models
    import app.modules.exam.models
    import app.modules.feed.models
    import app.modules.files.models
    import app.modules.interaction.models
    import app.modules.points.models
    import app.modules.projects.models
    import app.modules.starhope.models  # noqa: F401

    if not getattr(_base_module.Base.registry, "_configured", False):
        _base_module.Base.registry.configure()
    from app.db.auth_base import AuthBase

    if not getattr(AuthBase.registry, "_configured", False):
        AuthBase.registry.configure()
