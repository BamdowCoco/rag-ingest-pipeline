"""
内容索引仓储：document_dedup 的读写。

准入的 **权威** 就在这张表上（``content_hash`` 唯一索引），仓储只管读写、不做业务判断。
"""
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_dedup import DocumentDedup


class DedupRepo:
    def __init__(self, db: AsyncSession) -> None:
        """
        绑定请求级会话；本仓储不自行开事务。

        :param db: 请求级异步会话（事务边界由 service 控制）。
        """
        self.db = db

    async def get_by_hash(self, content_hash: str) -> DocumentDedup | None:
        """
        按 hash 取内容索引记录。

        :param content_hash: 标准化后标题+正文的 sha256。
        :return: 对应的准入行；没有则为 ``None``。
        """
        stmt = select(DocumentDedup).where(DocumentDedup.content_hash == content_hash)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def insert_dedup(self, content_hash: str, clean_id: int) -> DocumentDedup:
        """
        认领一份内容（只 ``add``，不 flush/commit —— 由 service 统一提交整笔事务）。

        :param content_hash: 这份内容的指纹。
        :param clean_id: 持有它的当前版本 id。
        :return: 新建的准入行（尚未 flush）。
        """
        row = DocumentDedup(content_hash=content_hash, clean_id=clean_id)
        self.db.add(row)
        return row

    async def delete_by_content_hash(self, content_hash: str) -> int:
        """
        **释放** 一份内容的占位，返回删了几行。

        更新 / 删除某个版本时会调它 —— 旧内容不再 active，就该放回"可准入"状态。
        不放的话，别的文档再推这份内容会被白白判成重复（而它其实已经不在语料里了）。

        同样不 commit，跟着调用方的事务走。

        :param content_hash: 要释放的内容指纹。
        :return: 实际删除的行数（通常是 1）。
        """
        stmt = delete(DocumentDedup).where(DocumentDedup.content_hash == content_hash)
        result = await self.db.execute(stmt)
        return result.rowcount or 0

    # ---------- 多行写入（bulk planner 用）----------

    async def get_by_hashes(self, hashes: list[str]) -> list[DocumentDedup]:
        """
        一次取回多个内容指纹的准入行（多行写入的读阶段）。

        :param hashes: 内容指纹列表；空列表直接返回空结果。
        :return: 命中的准入行，没命中的 hash 不会出现在结果里。
        """
        if not hashes:
            return []
        stmt = select(DocumentDedup).where(DocumentDedup.content_hash.in_(hashes))
        return list((await self.db.execute(stmt)).scalars().all())

    async def insert_dedups_bulk(self, rows: list[dict]) -> int:
        """
        一条多行 INSERT 认领多份内容（不是 executemany）。

        :param rows: 每行的列字典（``content_hash`` / ``clean_id``）。
        :return: 插入的行数。
        """
        if not rows:
            return 0
        await self.db.execute(insert(DocumentDedup).values(rows))
        return len(rows)

    async def delete_by_hashes(self, hashes: list[str]) -> int:
        """
        批量释放多份内容（更新时把旧内容让回准入池）。

        :param hashes: 要释放的内容指纹列表；空列表直接返回 0。
        :return: 实际删除的行数。
        """
        if not hashes:
            return 0
        result = await self.db.execute(
            delete(DocumentDedup).where(DocumentDedup.content_hash.in_(hashes))
        )
        return result.rowcount or 0
