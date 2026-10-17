# 假模型服务：**零第三方依赖** —— 所以这个 Dockerfile 只有三行实质内容
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY main.py .

EXPOSE 8000
CMD ["python", "main.py"]
