# 运行期不依赖任何外部下载；构建期只用下面那条 secret 挂载。
# 依赖 `RUN --mount=type=secret`（Docker 23+/BuildKit 内置前端，compose v2 默认开启）。
# 若引擎的内置前端过旧而报错，再在首行加 `# syntax=docker/dockerfile:1`
# ——那需要能拉到 docker/dockerfile:1 前端镜像，内网无出口时请改走镜像仓库。

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖（层缓存）。
# pip 内网镜像凭据在构建期由 BuildKit secret 挂载为 /etc/pip.conf，只在这次
# RUN 可见：不进镜像层、不进 docker history、不落到任何 COPY 的文件里。
# 凭据文件由部署机自备（仓库外/已 gitignore），构建命令见 README。
COPY requirements.txt .
RUN --mount=type=secret,id=pip_conf,target=/etc/pip.conf \
    pip install -r requirements.txt

# 拷贝运行所需代码（.env/.venv/.git 等由 .dockerignore 排除，运行时再挂载）。
# 顶层模块用通配符整目录拷贝：新增 mixin 时不必再改这里（曾因逐个列名漏掉
# flag_operations.py，导致容器启动即 ModuleNotFoundError）。
COPY *.py ./
COPY utils/ utils/

RUN useradd -m appuser && mkdir -p /data/ews_mcp && chown -R appuser:appuser /data/ews_mcp
USER appuser

EXPOSE 7805

# streamable-http，MCP 入口 /mcp（共享密钥走 .env 的 EWS_MCP_API_KEY，缺失即拒绝启动）。
# /healthz 与签名的 /downloads/* 免鉴权，分别供 compose healthcheck 和附件链接使用。
CMD ["python", "mcp_server.py"]
