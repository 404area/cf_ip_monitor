# syntax=docker/dockerfile:1

# ---- 依赖构建 (uv) ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- 运行镜像 ----
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends traceroute ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CF_IPDATA_DIR=/app/ipdata

COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock ./
COPY scripts ./scripts
COPY config.example.yaml ./config.example.yaml

RUN mkdir -p /app/data /app/output /app/ipdata

# ---- Master: HTTP API + 调度 + 评分 ----
FROM runtime AS master
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/docs', timeout=3)" || exit 1
CMD ["cf-ip-master", "--config", "/app/config.yaml"]

# ---- Agent: 拉任务 / 探测 / 回报 ----
FROM runtime AS agent
CMD ["cf-ip-agent", "--config", "/app/config.yaml"]
