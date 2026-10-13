"""v1 路由聚合：各 endpoints 的 router 在这里拼起来，main.py 只挂这一个。"""
from fastapi import APIRouter

from app.api.v1.endpoints import articles, stats

api_router = APIRouter()

# ⚠️ 顺序要紧：stats 必须【先】于 articles 注册。
#    因为 articles 里有一条 /articles/{article_id}；Starlette 按注册顺序匹配路径，
#    若它排在前面，GET /articles/stats 会先命中 /articles/{article_id}
#    （"stats" 当成 id 去转 int 失败）→ 422，stats 路由永远到不了。
api_router.include_router(stats.router, prefix="/articles", tags=["stats"])
api_router.include_router(articles.router, prefix="/articles", tags=["articles"])
