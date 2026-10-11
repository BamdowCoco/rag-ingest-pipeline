# ---- 基础镜像 ----
FROM python:3.13-slim

# PYTHONUNBUFFERED：日志不缓冲，docker logs 才能实时看到（不加会攒着一次性输出）
# PYTHONDONTWRITEBYTECODE：不在镜像里生成 __pycache__
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ---- 先只拷依赖清单并安装 ----
# 利用层缓存：只要 requirements.txt 没变，改代码时这一层直接命中缓存，不用重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 再拷代码 ----
# 这里只拷 app/（不拷 .env：镜像里不留口令，配置由 compose 的 env_file/environment 注入）
COPY app/ ./app/

EXPOSE 8000

# 不加 --reload：容器内不做热重载。想边写边跑，用宿主机 uvicorn（见 README「本地开发」）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
