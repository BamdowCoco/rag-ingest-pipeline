"""
去重表仓储：document_dedup 的读写。
判重的【权威】就在这张表上（content_hash 唯一索引），仓储只管读写、不做业务判断。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_dedup import DocumentDedup


class DedupRepo:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_hash(self, content_hash: str) -> DocumentDedup | None:
        """按 hash 取去重记录。当前不在热路径上，用于测试与阶段 6/9 的排查。"""
        stmt = select(DocumentDedup).where(DocumentDedup.content_hash == content_hash)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def insert_dedup(self, content_hash: str, clean_id: int) -> DocumentDedup:
        """插入去重记录。不 flush/commit —— 由 service 统一提交整笔事务。"""
        row = DocumentDedup(content_hash=content_hash, clean_id=clean_id)
        self.db.add(row)
        return row
