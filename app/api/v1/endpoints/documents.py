"""
文档接口。

GET 列表 / GET 单篇走 Cache-Aside（阶段 7）；POST 走完整管道（标准化→加锁→判重→入库）。
阶段 D：新增 POST /bulk 批量入口；单篇重复从 409 改为 200（「重复不是错误」）。
阶段 F：带 `external_id` 的文档走身份判断（同 id 内容变了 = 更新）；新增 POST /delete 删除信号。
"""
from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.api.deps import get_document_service
from app.schemas.chunk import ChunkOut
from app.schemas.document import (
    BulkDocumentCreate,
    BulkResult,
    DeleteResult,
    DocumentCreate,
    DocumentDelete,
    DocumentOut,
)
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
    response: Response,
    service: DocumentService = Depends(get_document_service),
) -> DocumentOut:
    row, created = await service.ingest(payload)
    if not created:
        # 内容已存在：不是错误（同步器周期性重推是正常行为），回 200 并返回已存在那篇
        response.status_code = status.HTTP_200_OK
    return row


@router.post("/bulk", status_code=status.HTTP_200_OK, response_model=BulkResult, summary="批量提交文档")
async def create_documents_bulk(
    payload: BulkDocumentCreate,
    service: DocumentService = Depends(get_document_service),
) -> BulkResult:
    # 批量是【混合结果】（有的新建、有的更新、有的重复），所以固定 200，逐条状态由 BulkResult 表达
    return await service.bulk_ingest(payload.documents)


# 注册在 `/{document_id}` 之前（与仓库既有约定一致）。实际 `{document_id}` 是 int 转换器，
# 不会吃掉 "delete"；但顺序写对，将来加静态子路径时就不会踩坑。
@router.post("/delete", response_model=DeleteResult, summary="删除文档（按源标识，幂等）")
async def delete_document(
    payload: DocumentDelete,
    service: DocumentService = Depends(get_document_service),
) -> DeleteResult:
    # external_id 可能含 `/`，所以放 body；删除信号会被反复重发，所以幂等、不报 404
    return DeleteResult(deleted=await service.delete_document(payload))


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
