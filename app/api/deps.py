"""
依赖注入：把 db / redis / service 组装起来。
endpoint 只写 ``service: DocumentService = Depends(get_document_service)``，不碰 session/redis。
"""
from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.infra.redis_client import get_redis
from app.services.document_service import DocumentService
from app.services.task_service import TaskService


def get_document_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> DocumentService:
    return DocumentService(db, redis)


def get_task_service(db: AsyncSession = Depends(get_db)) -> TaskService:
    # 任务分发只用得到数据库（SKIP LOCKED 是 MySQL 的能力），不需要 redis
    return TaskService(db)
