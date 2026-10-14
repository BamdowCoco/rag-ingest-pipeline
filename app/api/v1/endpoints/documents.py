"""
文档接口。

GET 列表 / GET 单篇走 Cache-Aside（阶段 7）；POST 走完整管道（标准化→加锁→判重→入库）。
"""
from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import get_document_service
from app.schemas.chunk import ChunkOut
from app.schemas.document import DocumentCreate, DocumentOut
from app.schemas.common import Page
from app.services.document_service import DocumentService

router = APIRouter()


@router.get("", response_model=Page[DocumentOut], summary="分页查询文档")
async def list_documents(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    source: str | None = Query(None, max_length=50),
    q: str | None = Query(None, min_length=1, max_length=50),
    service: DocumentService = Depends(get_document_service),
) -> Page[DocumentOut]:
    items, total = await service.list_documents(page=page, limit=limit, source=source, q=q)
    return Page[DocumentOut](items=items, total=total, page=page, limit=limit)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DocumentOut, summary="提交文档")
async def create_document(
    payload: DocumentCreate,
    service: DocumentService = Depends(get_document_service),
) -> DocumentOut:
    return await service.ingest(payload)


@router.get("/{document_id}", response_model=DocumentOut, summary="查询单篇文档")
async def get_document(
    document_id: int = Path(..., ge=1),
    service: DocumentService = Depends(get_document_service),
) -> DocumentOut:
    return await service.get_document(document_id)


@router.get("/{document_id}/chunks", response_model=Page[ChunkOut], summary="查询文档的分块")
async def list_document_chunks(
    document_id: int = Path(..., ge=1),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    service: DocumentService = Depends(get_document_service),
) -> Page[ChunkOut]:
    items, total = await service.list_chunks(document_id, page=page, limit=limit)
    return Page[ChunkOut](items=items, total=total, page=page, limit=limit)
