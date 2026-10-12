"""
依赖注入：把 db / redis / service 组装起来。
endpoint 只写 `service: ArticleService = Depends(get_article_service)`，不碰 session/redis。
"""
from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.infra.redis_client import get_redis
from app.services.article_service import ArticleService


def get_article_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ArticleService:
    return ArticleService(db, redis)
