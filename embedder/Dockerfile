FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 只装这一个依赖：worker 只需要 httpx（它不碰 DB/Redis，只调 api）
RUN pip install --no-cache-dir "httpx>=0.28"

COPY main.py .

CMD ["python", "main.py"]
