"""模块8 配置沉淀：settings 成为限流/会话时长唯一来源（含默认值与覆盖）。"""

from app.core.config import Settings, settings


def test_security_and_cookie_settings_have_sane_defaults() -> None:
    """限流与后台 cookie 时长沉淀为 settings，默认值与安全策略一致。"""
    assert settings.login_ip_max_per_min == 20
    assert settings.login_global_max_per_min == 200
    assert settings.login_window_seconds == 60
    assert settings.admin_access_cookie_minutes == 15
    # refresh 天数与原有配置一致（后台 cookie 复用它）
    assert settings.refresh_token_expire_days == 7


def test_login_limits_env_overridable(monkeypatch) -> None:
    """LKM_LOGIN_* 环境变量可覆盖限流参数（生产可按需调）。"""
    monkeypatch.setenv("LKM_LOGIN_IP_MAX_PER_MIN", "50")
    monkeypatch.setenv("LKM_LOGIN_WINDOW_SECONDS", "30")
    reloaded: Settings = Settings(_env_file=None)
    assert reloaded.login_ip_max_per_min == 50
    assert reloaded.login_window_seconds == 30


def test_admin_uses_settings_for_cookie_and_token_days() -> None:
    """admin cookie/refresh 不再用本地魔法数字，而引用 settings。

    S5-A2 后会话写面（login/refresh/2fa）迁入 AUTH 进程 —— admin cookie/token 时长由
    ``auth.admin_router``（auth 面）消费 settings；monolith 的
    ``admin.auth_router`` 现仅 /me。故源守卫指向 auth 侧拥主模块保“无魔法数字”意图。
    """
    import inspect

    import auth.admin_router as ar

    src = inspect.getsource(ar)
    assert "settings.admin_access_cookie_minutes" in src
    assert "settings.refresh_token_expire_days" in src
    assert "REFRESH_TOKEN_DAYS" not in src
