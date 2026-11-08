# ---- 构建阶段:用 uv 安装依赖 ----
FROM python:3.13-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# ---- 运行阶段 ----
FROM python:3.13-slim-bookworm
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
       poppler-utils libreoffice-writer libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app"

COPY . .

RUN mkdir -p /data

ENV LKM_BLOG_REPO_DIR=/data/blog_repos \
    LKM_FILES_STORE_DIR=/data/files_store

# 非 root 运行：进程只需要写 /data（blog 仓库、落盘文件）与 /tmp（上传/转换的临时目录），
# 代码与环境属于 root 且只读。此前以 root 跑意味着「上传一个文档 → LibreOffice 解析 →
# 拿到进程全部凭据（DB/S3/JWT）」的爆炸半径是整个节点。
RUN groupadd --gid 10001 lkm \
    && useradd --uid 10001 --gid lkm --create-home --shell /usr/sbin/nologin lkm \
    && chown -R lkm:lkm /data \
    && chmod +x /app/deploy/docker-entrypoint.sh
ENV HOME=/home/lkm
USER lkm

ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]

EXPOSE 8000
# 多 worker：默认单 worker（语义不变），设 LKM_WEB_WORKERS=N 水平跑满 CPU。
# 启停：uvicorn 收 SIGTERM 通知各 worker，FastAPI lifespan yield 后清理。
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${LKM_WEB_WORKERS:-1}"]
