"""
文章接口。

GET 走 service→repo 查库（阶段 7 再加缓存）；POST 走完整管道（清洗→去重→入库）。
"""
from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_article_service
from app.models.article_clean import ArticleClean
from app.schemas.article import ArticleCreate, ArticleOut
from app.schemas.common import Page
from app.services.article_service import ArticleService

router = APIRouter()


@router.get("", response_model=Page[ArticleOut], summary="分页查询文章")
async def list_articles(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    source: str | None = Query(None, max_length=50),
    q: str | None = Query(None, min_length=1, max_length=50),
    service: ArticleService = Depends(get_article_service),
) -> Page[ArticleOut]:
    items, total = await service.list_articles(page=page, limit=limit, source=source, q=q)
    return Page[ArticleOut](items=items, total=total, page=page, limit=limit)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ArticleOut, summary="提交舆情数据")
async def create_article(
    payload: ArticleCreate,
    service: ArticleService = Depends(get_article_service),
) -> ArticleClean:
    return await service.ingest(payload)
