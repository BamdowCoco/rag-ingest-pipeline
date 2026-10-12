"""统计接口。阶段 3 先占位，阶段 8 实现聚合（总数/按来源 top10/今日新增/判重拒收）。"""
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_article_service
from app.services.article_service import ArticleService

router = APIRouter()


@router.get("/stats", summary="文章统计")
async def get_stats(service: ArticleService = Depends(get_article_service)):
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="阶段 8 实现：聚合统计")
