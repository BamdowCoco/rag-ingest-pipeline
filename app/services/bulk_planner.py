"""
多行写入 planner：把一批 **彼此独立** 的文档用几条多行语句写完。

与逐篇路径的分工：

- 逐篇路径（``document_service._apply_identified_in_tx``）是 **参考实现**，处理一切语义；
- 本 planner 只负责「片内身份唯一、内容唯一」的成功快路径：
  3 条 SELECT 读完 → 纯 Python 分类 → 每类一条多行 INSERT → 一次 commit。
- 任一条件不满足（重复身份/重复内容/内容冲突/行数超限），``plan()`` 返回 ``None``，
  调用方回滚后用逐篇路径处理这一片 —— 正确性永远有兜底。

**ID 映射**：多行 ``INSERT ... VALUES (...),(...)`` 是 MySQL 的 simple insert；
InnoDB 在同一语句内分配连续的自增值，``result.lastrowid`` 是第一个。
本模块按插入顺序映射 ``first_id + i``，依赖这个行为（测试里有连续性断言）。
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.dedup_repo import DedupRepo
from app.repositories.document_repo import DocumentRepo
from app.schemas.document import BulkItemResult
from app.services.chunk_service import make_chunks
from app.utils.time import utcnow


@dataclass
class PostCommit:
    """
    片提交成功后才执行的 Redis 副作用。

    Redis 是「快路径」而不是权威：这里的动作全部在 DB commit 之后执行；
    执行失败只记日志，不影响已经落库的数据。

    :ivar active: ``content_hash -> 是否仍然 active``。同一 hash 在片内多次出现时
        只保留最后一次状态（``True`` 要 SADD，``False`` 要 SREM）。
    :ivar versions: ``(source, external_id) -> (clean_id, content_hash)``。
        每个身份只保留最终版本，避免把片内中间的旧版本写进 docver 缓存。
    """

    # content_hash -> 最终是否 active（True=SADD，False=SREM）
    active: dict[str, bool] = field(default_factory=dict)
    # 身份 -> 最终 (clean_id, content_hash)，用于写 docver 快路径
    versions: dict[tuple[str, str], tuple[int, str]] = field(default_factory=dict)


@dataclass
class _WriteItem:
    """本片里一篇 **需要真正写入** 的文档（新建或更新）及其上下文。"""

    index: int                     # 在请求里的位置，用于把结果按原顺序填回
    raw: dict                      # 原始入参（model_dump 后的 dict）
    cleaned: dict                  # 标准化后的字段 + content_hash
    chunks: list[dict]             # make_chunks 的产物（chunk_index/content/token_count）
    created: bool                  # True = 新建；False = 更新 / 复活
    source_id: int | None          # 已存在身份的 id；None 表示本片新建
    new_source_pos: int | None     # 新建身份在 new_sources 里的下标（用来映射 source id）
    old_clean_id: int | None       # 更新时要作废的旧版本 id；新建为 None
    old_hash: str | None           # 更新时要释放的旧内容 hash；新建为 None
    revive: bool                   # 该身份之前被删除过，需要清掉 deleted_at


@dataclass
class BulkPlan:
    """一次多行写入的完整计划：``plan()`` 读完后产出，``write()`` 照着执行。"""

    duplicates: dict[int, int]          # 请求下标 -> 已存在版本的 id（直接回 duplicate）
    writes: list[_WriteItem]            # 需要写入的文档，顺序与请求一致
    new_sources: list[tuple[str, str]]  # 需要新建的身份；下标用于映射 source id
    post: PostCommit                    # 提交成功后的 Redis 副作用


class BulkPlanner:
    def __init__(self, db: AsyncSession) -> None:
        """
        绑定一个数据库会话与本批用到的两个仓储。

        与调用方共用同一个 session：读阶段的 ``FOR UPDATE`` 锁、写阶段的多行语句、
        以及最后的 commit 必须落在同一个事务里。

        :param db: 请求级异步会话；事务边界由调用方（``bulk_ingest``）控制。
        """
        self.db = db
        self.repo = DocumentRepo(db)
        self.dedup_repo = DedupRepo(db)

    # ---------- 读 + 决策 ----------

    async def plan(
        self, chunk: list[tuple[int, dict, dict]], *, max_chunk_rows: int
    ) -> BulkPlan | None:
        """
        读阶段 + 决策：把一片文档分成「直接 duplicate」与「需要写入」两类。

        先做两条批量当前读（身份、active 版本）与一条 dedup 查询，再按请求顺序分类。
        任何条件不满足（片内重复身份/内容、内容冲突、chunk 行数超限）都返回 ``None``，
        由调用方回滚后用逐篇路径处理 —— 正确性始终有兜底。

        :param chunk: 本片待处理的 ``(请求下标, 原始 dict, 标准化 dict)`` 列表。
        :param max_chunk_rows: 本片允许写入的最大 chunk 行数；超过则放弃多行路径。
        :return: 可执行的多行写入计划；``None`` 表示本片不适合走多行写入。
        """
        keys = [(raw["source"], raw["external_id"]) for _, raw, _ in chunk]
        hashes = [cleaned["content_hash"] for _, _, cleaned in chunk]
        # 片内重复身份/重复内容 → 时序语义复杂，整片交给逐篇
        if len(keys) != len(set(keys)) or len(hashes) != len(set(hashes)):
            return None

        source_by_key = {
            (s.source, s.external_id): s
            for s in await self.repo.get_sources_by_keys_for_update(keys)
        }
        source_ids = [s.id for s in source_by_key.values()]
        active_by_source = {
            v.source_id: v
            for v in await self.repo.get_active_versions_by_source_ids_for_update(source_ids)
        }
        dedup_by_hash = {
            d.content_hash: d for d in await self.dedup_repo.get_by_hashes(hashes)
        }

        # 请求下标 -> 已存在版本的 id：这些条目直接回 duplicate，不写库
        duplicates: dict[int, int] = {}
        # 本片真正要写入的文档（新建 + 更新），顺序与请求一致
        writes: list[_WriteItem] = []
        # 本片要新建的身份；下标就是后面 first_source_id + pos 的映射
        new_sources: list[tuple[str, str]] = []
        # 本片累计的 chunk 行数，用来守住 BULK_MAX_CHUNK_ROWS
        chunk_row_count = 0

        for i, raw, cleaned in chunk:
            key = (raw["source"], raw["external_id"])
            content_hash = cleaned["content_hash"]
            src = source_by_key.get(key)

            if src is None:
                existing = dedup_by_hash.get(content_hash)
                if existing is not None:
                    duplicates[i] = existing.clean_id
                    continue
                chunks = make_chunks(cleaned)
                chunk_row_count += len(chunks)
                if chunk_row_count > max_chunk_rows:
                    return None
                new_sources.append(key)
                writes.append(
                    _WriteItem(
                        index=i, raw=raw, cleaned=cleaned, chunks=chunks,
                        created=True, source_id=None, new_source_pos=len(new_sources) - 1,
                        old_clean_id=None, old_hash=None, revive=False,
                    )
                )
                continue

            active = active_by_source.get(src.id)
            if active is not None and active.content_hash == content_hash:
                duplicates[i] = active.id
                continue

            existing = dedup_by_hash.get(content_hash)
            if existing is not None and (active is None or existing.clean_id != active.id):
                # 新内容已被另一个身份持有 —— 交给逐篇路径判 409 / 200
                return None

            chunks = make_chunks(cleaned)
            chunk_row_count += len(chunks)
            if chunk_row_count > max_chunk_rows:
                return None
            writes.append(
                _WriteItem(
                    index=i, raw=raw, cleaned=cleaned, chunks=chunks,
                    created=False, source_id=src.id, new_source_pos=None,
                    old_clean_id=active.id if active is not None else None,
                    old_hash=active.content_hash if active is not None else None,
                    revive=src.deleted_at is not None,
                )
            )

        return BulkPlan(
            duplicates=duplicates, writes=writes, new_sources=new_sources, post=PostCommit()
        )

    # ---------- 写 ----------

    async def write(
        self, plan: BulkPlan, *, now: datetime | None = None
    ) -> tuple[dict[int, BulkItemResult], PostCommit]:
        """
        执行写计划，但 **不 commit** —— 事务边界由调用方控制。

        语句顺序是「先释放旧内容，再插入新内容」：更新时若把某个 hash 让给同批另一篇，
        先 INSERT 再 DELETE 会撞 ``content_hash`` 唯一索引。

        :param plan: ``plan()`` 产出的写计划。
        :param now: 本批统一使用的时间戳；不传则取当前 UTC 时间。
        :return: ``(逐条结果, post-commit 动作)``；调用方在 commit 成功后再执行 Redis 动作。
        :raises RuntimeError: 多行 INSERT 没有返回自增 id（驱动或配置异常）。
        """
        now = now or utcnow()
        # 先把 duplicate 的结果填好；需要写入的文档在下面写完后补上 id
        results: dict[int, BulkItemResult] = {
            i: BulkItemResult(index=i, status="duplicate", id=cid)
            for i, cid in plan.duplicates.items()
        }
        # 提交成功后要执行的 Redis 动作（由调用方执行）
        post = PostCommit()

        if not plan.writes:
            return results, post

        # 更新路径要作废的旧版本 id / 要释放的旧内容 hash / 需要清 deleted_at 的源 id
        old_ids = [w.old_clean_id for w in plan.writes if w.old_clean_id is not None]
        old_hashes = [w.old_hash for w in plan.writes if w.old_hash is not None]
        revive_ids = [w.source_id for w in plan.writes if w.revive and w.source_id is not None]

        # ① 先释放旧内容，再插入新内容（否则「更新时把 hash 让给同批另一篇」会撞唯一索引）
        if old_ids:
            await self.repo.supersede_versions(old_ids, now)
            await self.repo.supersede_chunks_by_documents(old_ids, now)
        if old_hashes:
            await self.dedup_repo.delete_by_hashes(old_hashes)
        if revive_ids:
            await self.repo.revive_sources(revive_ids, now)

        # ② 新建身份（首 id 之后按片内顺序连续分配）
        first_source_id: int | None = None
        if plan.new_sources:
            first_source_id = await self.repo.insert_sources_bulk(
                [
                    {
                        "source": source,
                        "external_id": external_id,
                        "created_at": now,
                        "updated_at": now,
                        "deleted_at": None,
                    }
                    for source, external_id in plan.new_sources
                ]
            )
            if first_source_id is None:
                raise RuntimeError("多行 INSERT 没有返回自增 id")

        # ③ raw / clean：两个多行 INSERT 各自返回首 id，第 i 篇的 id = first_* + i
        first_raw_id = await self.repo.insert_raws_bulk(
            [
                {
                    "source": w.raw["source"],
                    "external_id": w.raw.get("external_id"),
                    "raw_title": w.raw["title"],
                    "raw_content": w.raw["content"],
                    "received_at": now,
                }
                for w in plan.writes
            ]
        )
        if first_raw_id is None:
            raise RuntimeError("多行 INSERT 没有返回自增 id")

        clean_rows = []
        for i, w in enumerate(plan.writes):
            source_id = w.source_id if w.source_id is not None else first_source_id + w.new_source_pos
            clean_rows.append(
                {
                    "raw_id": first_raw_id + i,
                    "source_id": source_id,
                    "source": w.raw["source"],
                    "external_id": w.raw.get("external_id"),
                    "title": w.cleaned["title"],
                    "content": w.cleaned["content"],
                    "author": w.raw.get("author"),
                    "url": w.raw.get("url"),
                    "doc_updated_at": w.raw.get("doc_updated_at"),
                    "fetched_at": now,
                    "char_count": w.cleaned["char_count"],
                    "clean_version": w.cleaned["clean_version"],
                    "content_hash": w.cleaned["content_hash"],
                    "superseded_at": None,
                    "enrich_status": "pending",
                }
            )
        first_clean_id = await self.repo.insert_cleans_bulk(clean_rows)
        if first_clean_id is None:
            raise RuntimeError("多行 INSERT 没有返回自增 id")

        # ④ 所有文档的块一次插入（先收集每行，最后合成一条多行 INSERT）
        chunk_rows_to_insert: list[dict] = []
        for i, w in enumerate(plan.writes):
            for c in w.chunks:
                chunk_rows_to_insert.append(
                    {
                        "document_id": first_clean_id + i,
                        "chunk_index": c["chunk_index"],
                        "content": c["content"],
                        "token_count": c["token_count"],
                        "embedding_status": "pending",
                        "superseded_at": None,
                        "retracted_at": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        if chunk_rows_to_insert:
            await self.repo.insert_chunks_bulk(chunk_rows_to_insert)

        # ⑤ 内容准入
        await self.dedup_repo.insert_dedups_bulk(
            [
                {"content_hash": w.cleaned["content_hash"], "clean_id": first_clean_id + i}
                for i, w in enumerate(plan.writes)
            ]
        )

        # ⑥ 结果 + post-commit
        for i, w in enumerate(plan.writes):
            clean_id = first_clean_id + i
            results[w.index] = BulkItemResult(
                index=w.index, status="created" if w.created else "updated", id=clean_id
            )
            if w.old_hash is not None:
                post.active[w.old_hash] = False
            post.active[w.cleaned["content_hash"]] = True
            post.versions[(w.raw["source"], w.raw["external_id"])] = (
                clean_id,
                w.cleaned["content_hash"],
            )

        return results, post
