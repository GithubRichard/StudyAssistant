FROM python:3.11-slim

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY app ./app
COPY config.example.yaml ./config.example.yaml
COPY prompt_system.txt ./prompt_system.txt

# 容器内数据目录（SQLite + 上传图片），用 volume 持久化
VOLUME ["/srv/app/data"]

EXPOSE 8000

# 没有 config.yaml 就用模板生成一份（首次启动），有则不动
CMD ["sh", "-c", "cp -n config.example.yaml config.yaml 2>/dev/null; uvicorn app.main:app --host 0.0.0.0 --port 8000"]
