# ---- 基础镜像（生产用的 runtime stage）----
FROM python:3.13-slim AS runtime

# PYTHONUNBUFFERED：日志不缓冲，docker logs 才能实时看到（不加会攒着一次性输出）
# PYTHONDONTWRITEBYTECODE：不在镜像里生成 __pycache__
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ---- 先只拷依赖清单并安装 ----
# 利用层缓存：只要 requirements.txt 没变，改代码时这一层直接命中缓存，不用重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
 && python -c "import jieba; jieba.initialize()"

# ---- 再拷代码 ----
# 这里只拷 app/（不拷 .env：镜像里不留口令，配置由 compose 的 env_file/environment 注入）
COPY app/ ./app/

# 迁移：alembic 的配置 + 迁移脚本。lifespan 里要跑 `upgrade head`，所以必须在镜像里
COPY alembic.ini .
COPY alembic/ ./alembic/

EXPOSE 8000

# 不加 --reload：容器内不做热重载。想边写边跑，用宿主机 uvicorn（见 README「本地开发」）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# ---- 测试 stage（多阶段构建的第二个 target）----
# 用法：docker compose --profile test run --rm test
# FROM runtime 而不是重开一个 slim：直接继承上面【构建期已预热】的 tiktoken 词表，
# 否则测试进程首次 import 又会去联网下词表。
FROM runtime AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

# pytest 的 asyncio 配置在 pyproject.toml 里（生产镜像不拷它，也就读不到那段配置）
COPY pyproject.toml .
COPY tests/ ./tests/

CMD ["pytest", "-q"]
