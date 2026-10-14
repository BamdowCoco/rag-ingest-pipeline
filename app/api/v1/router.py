"""v1 路由聚合：各 endpoints 的 router 在这里拼起来，main.py 只挂这一个。"""
from fastapi import APIRouter

from app.api.v1.endpoints import chunks, documents, stats

api_router = APIRouter()

# ⚠️ 顺序要紧：stats 必须【先】于 documents 注册。
#    因为 documents 里有一条 /documents/{document_id}；Starlette 按注册顺序匹配路径，
#    若它排在前面，GET /documents/stats 会先命中 /documents/{document_id}
#    （"stats" 当成 id 去转 int 失败）→ 422，stats 路由永远到不了。
api_router.include_router(stats.router, prefix="/documents", tags=["stats"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
# /chunks/* 与 /documents/* 不冲突，放最后即可
api_router.include_router(chunks.router, prefix="/chunks", tags=["chunks"])
