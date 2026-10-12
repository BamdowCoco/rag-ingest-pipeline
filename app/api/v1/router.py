"""v1 路由聚合：各 endpoints 的 router 在这里拼起来，main.py 只挂这一个。"""
from fastapi import APIRouter

from app.api.v1.endpoints import articles, stats

api_router = APIRouter()
api_router.include_router(articles.router, prefix="/articles", tags=["articles"])
api_router.include_router(stats.router, prefix="/articles", tags=["stats"])
