"""统计接口：总数 / 按来源 top10 / 今日新增 / 判重拒收累计。"""
from fastapi import APIRouter, Depends

from app.api.deps import get_document_service
from app.schemas.stats import StatsOut
from app.services.document_service import DocumentService

router = APIRouter()


@router.get("/stats", response_model=StatsOut, summary="文档统计")
async def get_stats(service: DocumentService = Depends(get_document_service)) -> StatsOut:
    """
    文档统计（整体缓存，与列表共用版本号）。

    :param service: 文档服务。
    :return: 总数 / 来源 top10 / 今日新增 / 判重拒收 / chunk 状态分布。
    """
    return StatsOut(**await service.get_stats())
