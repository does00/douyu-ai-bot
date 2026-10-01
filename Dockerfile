FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py webui.py douyu_proto.py douyu_auth.py login.py ./

# config.yaml / .env / data 由 docker compose 挂载（见 docker-compose.yml）
# 如需 HTTP 代理：compose 里设置 PROXY_URL 环境变量

CMD ["python", "bot.py"]
