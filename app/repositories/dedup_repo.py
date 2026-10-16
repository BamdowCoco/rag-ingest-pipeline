"""
内容索引仓储：document_dedup 的读写。

准入的 **权威** 就在这张表上（`content_hash` 唯一索引），仓储只管读写、不做业务判断。
"""
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_dedup import DocumentDedup


class DedupRepo:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_hash(self, content_hash: str) -> DocumentDedup | None:
        """按 hash 取内容索引记录。"""
        stmt = select(DocumentDedup).where(DocumentDedup.content_hash == content_hash)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def insert_dedup(self, content_hash: str, clean_id: int) -> DocumentDedup:
        """认领一份内容。不 flush/commit —— 由 service 统一提交整笔事务。"""
        row = DocumentDedup(content_hash=content_hash, clean_id=clean_id)
        self.db.add(row)
        return row

    async def delete_by_content_hash(self, content_hash: str) -> int:
        """
        **释放** 一份内容的占位，返回删了几行。

        更新 / 删除某个版本时会调它 —— 旧内容不再 active，就该放回"可准入"状态。
        不放的话，别的文档再推这份内容会被白白判成重复（而它其实已经不在语料里了）。

        同样不 commit，跟着调用方的事务走。
        """
        stmt = delete(DocumentDedup).where(DocumentDedup.content_hash == content_hash)
        result = await self.db.execute(stmt)
        return result.rowcount or 0
