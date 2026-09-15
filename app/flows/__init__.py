"""Prefect flows 定义（M5 7.2.5，蓝图《后端规划.md》§八 `flows/`）。

APScheduler 只做简单 cron 触发入口（cron.* 经总线），复杂数据管道的 DAG / 失败重试 /
回填由本层 Prefect flow 编排。flow 不复制业务 SQL，只复用各域 owner 侧既有入口。
"""
