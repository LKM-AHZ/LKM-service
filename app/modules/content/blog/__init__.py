"""blog 子包：博客系列的模型、仓库、服务、git 智能 HTTP 面与 REST/GraphQL 面。

原为独立业务域 `app.modules.blog`，按蓝图 M2「内容域收敛」并入 content 聚合（目录级合并，
表结构与路由前缀不变）。子包与 boards/columns/qa 同构，不自行导出 ROUTERS/GRAPHQL——
对外聚合统一由 `app.modules.content.__init__` 提供。
"""
