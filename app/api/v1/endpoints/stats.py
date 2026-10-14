"""统计接口：总数 / 按来源 top10 / 今日新增 / 判重拒收累计。"""
from fastapi import APIRouter, Depends

from app.api.deps import get_document_service
from app.schemas.stats import StatsOut
from app.services.document_service import DocumentService

router = APIRouter()


@router.get("/stats", response_model=StatsOut, summary="文档统计")
async def get_stats(service: DocumentService = Depends(get_document_service)) -> StatsOut:
    return StatsOut(**await service.get_stats())
