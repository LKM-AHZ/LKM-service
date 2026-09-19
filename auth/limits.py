"""认证域限流参数集中定义。

各处调用 `check_code_rate_limit` / `check_password_login_rate_limit` 时不再散落
魔法数字，统一引用这些命名常量，避免同名语义漂移（比如把 admin 恢复的 3/600 写错
成常规验证码的 5/3600）。数值即事实来源，改动一处全站生效。

bucket 语义：
  CODE_*_MAX/WINDOW       —— 验证码发放 / 核验（最常见：5 次 / 1 小时）
  REFRESH_*               —— 刷新令牌（按 IP）30 次 / 1 分钟
  GLOBAL_WIDE_*           —— 跨渠道全局防御（magic-link/2FA 核验）10 次 / 1 小时
  RECOVER_ADMIN_BEGIN_*   —— admin 恢复发起：3 次 / 1 小时
  RECOVER_ADMIN_VERIFY_*  —— admin 恢复核验：3 次 / 10 分钟
"""

# 验证码发放 / 核验（默认桶）
CODE_MAX_PER_WINDOW = 5
CODE_WINDOW_SECONDS = 3600

# 刷新令牌（按 IP）
REFRESH_MAX_PER_WINDOW = 30
REFRESH_WINDOW_SECONDS = 60

# 全局高防核验桶：magic-link 核验、2FA 核验 / step-up
GLOBAL_VERIFY_MAX_PER_WINDOW = 10
GLOBAL_VERIFY_WINDOW_SECONDS = 3600

# admin 恢复：发起
RECOVER_ADMIN_BEGIN_MAX = 3
RECOVER_ADMIN_BEGIN_WINDOW = 3600

# admin 恢复：核验验证码
RECOVER_ADMIN_VERIFY_MAX = 3
RECOVER_ADMIN_VERIFY_WINDOW = 600
