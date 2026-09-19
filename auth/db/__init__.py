"""AUTH 独立库的 DB 基建：ORM 基座、会话/引擎工厂、schema 初始化。

拆库后业务库与 auth 库各有一条建库链（``app.db.init_db`` / ``auth.db.init``），
本子包持有 auth 侧全部库级物，业务库侧不得反向引用。
"""
