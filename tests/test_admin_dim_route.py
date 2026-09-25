"""``/admin/user-dim`` 端点的接线断言（蓝图 §5.4：user_dim 服务运营报表）。

只验「挂在 admin 公共面上」——没挂上就不会被 ``create_app`` include，端点存在与否无从谈起。
鉴权/分页/权限语义复用 ``reports_router`` 同款依赖与 ``admin_list_user_dim`` 的既有测试
（``tests/test_admin_user_dim_report.py`` 已覆盖 read port 语义）。
"""

from __future__ import annotations


def test_dim_report_router_exported_with_expected_path() -> None:
    from app.modules import admin

    paths = {route.path for r in admin.ROUTERS for route in getattr(r, "routes", [])}
    assert "/admin/user-dim" in paths
