FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# tzdata：让 TZ=Asia/Shanghai 生效（日志按天轮转按北京时间算）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-osd \
    && rm -rf /var/lib/apt/lists/*

# 应用代码 + 技能包（技能包需要同步安装到 Hermes 的 profile，见 README）
COPY app ./app
COPY hermes ./hermes
COPY web ./web
COPY scripts ./scripts
COPY config.example.yaml ./config.example.yaml
COPY prompt_system.txt ./prompt_system.txt

# git 版本号：构建时由 --build-arg GIT_VERSION 传入（.git 不会 COPY 进镜像），
# 运行时 SA_GIT_VERSION 为空时回落到 git rev-parse（本地直接跑的场景）。
ARG GIT_VERSION=unknown
ENV SA_GIT_VERSION=${GIT_VERSION}

# 非 root 运行；运行数据与工作区通过 volume 挂载
RUN useradd --system --uid 10001 --create-home appuser \
    && mkdir -p /srv/app/data /srv/app/workspace \
    && chown -R appuser:appuser /srv/app
USER appuser

ENV CONFIG_PATH=/srv/app/config.yaml \
    APP_HOST=127.0.0.1 \
    APP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

# 只监听本机：公网入口交给宿主机上的 HTTPS 反向代理
# 使用 --factory：导入模块时不加载配置，缺配置时启动阶段报错更清晰
CMD ["sh", "-c", "uvicorn app.main:create_app --factory --host \"${APP_HOST}\" --port \"${APP_PORT}\" --no-access-log"]
