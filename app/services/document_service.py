"""
文档业务编排。

阶段 3：list_documents（不加缓存）。
阶段 5：ingest() —— 标准化 → 判重(快路径) → 三表落库。
阶段 6：给 ingest 的临界区套上按 hash 粒度的分布式锁。
阶段 7：列表与单篇都走 Cache-Aside；写入成功后 bump 列表版本号。
阶段 9：service 不再抛 HTTPException（改为业务异常，HTTP 映射在 core/exceptions.py）；补处理日志；插入 AI 接缝 enrich（失败不阻断入库）。
阶段 B：落库后接上【切块】；新增分块查询（纯 TTL 缓存）。
阶段 D：重复改为「不是错误」（返回已存在那篇）；新增批量摄入 bulk_ingest（pipeline 判重）。
"""
import logging

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    ContentConflictError,
    DocumentInProgressError,
    DocumentNotFoundError,
    EmptyContentError,
)
from app.models.document_clean import DocumentClean
from app.repositories.chunk_repo import ChunkRepo
from app.repositories.document_repo import DocumentRepo
from app.repositories.dedup_repo import DedupRepo
from app.schemas.chunk import ChunkOut
from app.schemas.document import (
    BulkItemResult,
    BulkResult,
    DocumentCreate,
    DocumentDelete,
    DocumentOut,
)
from app.services.cache_service import (
    cache_delete,
    cache_get,
    cache_set,
    chunk_list_cache_key,
    document_cache_key,
    document_version_key,
    bump_list_version,
    get_list_version,
    list_cache_key,
    stats_cache_key,
)
from app.services.chunk_service import make_chunks
from app.services.normalize_service import normalize_document
from app.services.dedup_service import (
    check_and_mark,
    get_rejected_count,
    incr_rejected,
    release_mark,
)
from app.services.lock_service import LockNotAcquired, document_lock
from app.utils.time import beijing_today_utc_range, utcnow

logger = logging.getLogger(__name__)


class DocumentService:
    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        self.db = db
        self.redis = redis          # 快路径判重、分布式锁、缓存/版本号
        self.repo = DocumentRepo(db)
        self.dedup_repo = DedupRepo(db)
        self.chunk_repo = ChunkRepo(db)

    # ---------- 读 ----------

    async def list_documents(
        self, *, page: int, limit: int, source: str | None = None, q: str | None = None
    ) -> tuple[list[dict], int]:
        """
        列表查询（Cache-Aside）。返回 (本页数据 dict 列表, 满足条件的总数)。

        带 ``q`` 的请求【不进缓存】：q 是自由文本、键空间无上限（能被刷爆），
        且命中率极低 —— 直接查库反而更划算。
        """
        cache_key: str | None = None
        if q is None:
            version = await get_list_version(self.redis)
            cache_key = list_cache_key(version, page=page, limit=limit, source=source)
            cached = await cache_get(self.redis, cache_key)
            if cached is not None:
                return cached["items"], cached["total"]

        items = await self.repo.list_documents(page=page, limit=limit, source=source, q=q)
        total = await self.repo.count_documents(source=source, q=q)
        # 转成 JSON 友好的 dict（mode="json" 让 datetime 变字符串），缓存和返回都用它
        payload = {
            "items": [DocumentOut.model_validate(o).model_dump(mode="json") for o in items],
            "total": total,
        }
        if cache_key is not None:
            await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload["items"], payload["total"]

    async def get_document(self, document_id: int) -> DocumentOut:
        """
        单篇查询（Cache-Aside）。

        注意：本项目文档写入后永不修改，所以单条缓存用纯 TTL 就够，
        不需要「写时删」—— 失效策略取决于数据可变性，可变数据才需要写时失效。
        """
        cache_key = document_cache_key(document_id)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return DocumentOut.model_validate(cached)

        obj = await self.repo.get_by_id(document_id)
        if obj is None:
            logger.info("文档不存在 id=%s", document_id)
            raise DocumentNotFoundError()

        out = DocumentOut.model_validate(obj)
        await cache_set(self.redis, cache_key, out.model_dump(mode="json"), ttl=settings.cache_ttl)
        return out

    async def get_stats(self) -> dict:
        """
        统计：总数 / 按来源 top10 / 今日新增 / 判重拒收累计 / **chunk 状态分布** 。

        整体缓存，与列表共用同一个版本号 —— 一次 INCR 同时作废两者。
        「今日」按【北京时间】算（库里存的是 UTC，所以这里做一次边界换算）。

        注意 chunk_status 的陈旧度：版本号只在【文档写入】时 +1，而 claim/complete
        不动版本号，所以「向量化积压」最多陈旧一个 cache_ttl（默认 60s）。
        看板指标 60s 粒度可接受，也与 total/today_count 的陈旧度一致 —— 不为它破例。
        """
        version = await get_list_version(self.redis)
        cache_key = stats_cache_key(version)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return cached

        today_start, today_end = beijing_today_utc_range()
        by_source = await self.repo.count_by_source(limit=10)
        chunk_status = await self.repo.count_chunks_by_status()
        payload = {
            "total": await self.repo.count_documents(),
            "by_source": [{"source": s, "count": n} for s, n in by_source],
            "today_count": await self.repo.count_since(today_start, today_end),
            "dedup_rejected": await get_rejected_count(self.redis),
            "chunk_status": [{"status": s, "count": n} for s, n in chunk_status],
        }
        await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload

    async def list_chunks(self, document_id: int, *, page: int, limit: int) -> tuple[list[dict], int]:
        """
        分块列表（Cache-Aside，纯 TTL）。

        为什么不像列表/统计那样用版本号：chunk 是 **一次写入、永不修改** 的 ——
        没有「写后失效」这回事，TTL 自然过期就够了。
        这正是「失效策略取决于数据可变性」的正面例子。
        """
        cache_key = chunk_list_cache_key(document_id, page=page, limit=limit)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return cached["items"], cached["total"]

        rows = await self.repo.list_chunks(document_id, page=page, limit=limit)
        total = await self.repo.count_chunks(document_id)
        # 空结果时再确认文档是否存在 —— 免得把「id 打错了」显示成「文档存在但没有块」。
        # 只在空结果时多查一次，常见路径不吃这个开销。
        if total == 0 and await self.repo.get_by_id(document_id) is None:
            raise DocumentNotFoundError()

        payload = {
            "items": [ChunkOut.model_validate(r).model_dump(mode="json") for r in rows],
            "total": total,
        }
        await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload["items"], payload["total"]

    # ---------- 写 ----------

    async def _persist(
        self,
        raw: dict,
        cleaned: dict,
        *,
        source_id: int,
    ) -> DocumentClean:
        """
        三表顺序落库：raw → clean → dedup（顺带切块）， **只 flush、不 commit** 。

        单篇与批量都要走这一段，但事务边界各自由调用方决定（批量是每篇一个事务），
        所以提交不放在这里。

        **落库时同时认领这份内容** （写 ``document_dedup``）—— 那是 **准入占位** 。
        新建 / 更新 / 复活 **都要认领** ：只有这样，「任何时刻 active 内容两两不同」
        才是条 **真不变式** ，而不是"只在新建时成立"。
        撞车由 ``content_hash`` 的唯一索引拦下，报什么由调用方决定。

        **enrich 那几列这里一个字都不写** —— 它们由后台循环补（见 ``enricher.py``），
        落库时 ``enrich_status`` 还是默认的 ``pending``。这正是「AI 不拦请求」的落点。

        **raw 不再存整包请求体** （2026-10-17 删了 ``payload`` 那列，见 §9.13-a）：
        留痕靠独立列 —— ``source`` / ``external_id`` / ``raw_title`` / ``raw_content`` / ``received_at``
        已经够自描述，且正文因此从三份降到两份。
        """
        raw_row = await self.repo.insert_raw(
            source=raw["source"],
            external_id=raw.get("external_id"),
            raw_title=raw["title"],
            raw_content=raw["content"],
        )
        clean_row = await self.repo.insert_clean(
            raw_id=raw_row.id,
            source=raw["source"],
            source_id=source_id,
            external_id=raw.get("external_id"),
            content_hash=cleaned["content_hash"],
            title=cleaned["title"],
            content=cleaned["content"],
            author=raw.get("author"),          # enrich 补的署名由后台写回，不在这里
            url=raw.get("url"),
            doc_updated_at=raw.get("doc_updated_at"),
            char_count=cleaned["char_count"],
            clean_version=cleaned["clean_version"],
        )
        # 切块：文档落库后立刻切。chunk 与文档在【同一个事务】里 ——
        # 要么文档和它的块都在，要么都不在，不留半截数据。
        chunks = make_chunks(cleaned)
        await self.repo.insert_chunks(clean_row.id, chunks)

        # 认领这份内容 —— 唯一索引是权威，撞车就抛 IntegrityError，报什么由调用方决定
        await self.dedup_repo.insert_dedup(cleaned["content_hash"], clean_row.id)
        logger.info("落库完成 doc_id=%s 块数=%d", clean_row.id, len(chunks))
        return clean_row

    async def _find_existing(self, content_hash: str) -> DocumentClean | None:
        """按内容指纹回读「已存在」的那篇文档（准入检查命中时要把这篇回给调用方）。"""
        dedup = await self.dedup_repo.get_by_hash(content_hash)
        if dedup is None:
            return None
        return await self.repo.get_by_id(dedup.clean_id)

    async def ingest(self, payload: DocumentCreate) -> tuple[DocumentClean, bool]:
        """
        整条管道：标准化 → **快路径** → 加锁 → 身份判断 → 三表落库 → 提交 → bump 列表版本号。

        只有一条路：``external_id`` 就是文档身份。

        - **快路径** （不查库、不抢锁）：命中且内容没变 → 直接回当前版本
        - **库路径** ：没有身份行 → 过准入检查后新建；有 → 比 hash 决定跳过还是更新

        返回 (文档, 是否新建)，接口层据此回 201 / 200。
        """
        raw = payload.model_dump()
        cleaned = normalize_document(raw)
        content_hash = cleaned["content_hash"]
        logger.info(
            "ingest 开始 source=%s ext=%s hash=%s",
            raw["source"], raw["external_id"], content_hash[:12],
        )

        # 标准化后正文为空 → 拒收。放锁外做，省一次加锁（反正它不涉及竞态）。
        if not cleaned["content"]:
            logger.info("拒收：标准化后正文为空 hash=%s", content_hash[:12])
            raise EmptyContentError()

        # ---- 快路径：最常见的那路流量（重推一篇没变的文档），不查库、不抢锁 ----
        fast = await self._fast_skip(raw, cleaned)
        if fast is not None:
            return fast, False

        lock_id = self._identity_lock_id(raw["source"], raw["external_id"])
        try:
            async with document_lock(self.redis, lock_id):
                clean_row, created, changed = await self._apply_identified(raw, cleaned)
        except LockNotAcquired:
            logger.info("未拿到锁（同身份正在处理）ext=%s", raw["external_id"])
            raise DocumentInProgressError() from None

        if changed:
            await bump_list_version(self.redis)
        return clean_row, created

    async def _fast_skip(self, raw: dict, cleaned: dict) -> DocumentClean | None:
        """
        身份版本快路径：缓存命中且内容没变 → 直接回【当前版本】。

        **它挡的是最常见的流量** ：同步器周期性重推一篇没变的文档。
        没这一步，每次重推都要 ``SELECT ... FOR UPDATE`` 去抢一次行锁 —— 又慢、又互相排队。
        缓存里存 ``{clean_id, content_hash}``：光有 hash 不够，还得有 id 才能取出文档本体回给调用方。

        **失败模式** ：这份缓存错了是 **「漏一次更新」** （把新内容当成没变），
        不是去重 SET 那种「多拒一次」—— 严重性不同，所以它 write-through + TTL 兜底。
        读不到实体行（缓存与库不同步）时 **老实回落** 到库路径，不硬信缓存。
        """
        cached = await cache_get(
            self.redis, document_version_key(raw["source"], raw["external_id"])
        )
        if not cached or cached.get("content_hash") != cleaned["content_hash"]:
            return None

        row = await self.repo.get_by_id(cached["clean_id"])
        if row is None:                      # 不一致态 → 回落，别撒谎
            return None

        await incr_rejected(self.redis)      # 它确实挡掉了一次重复同步
        logger.info("快路径命中 → 内容未变 ext=%s", raw["external_id"])
        return row

    async def _remember_version(
        self, source: str, external_id: str, clean_row: DocumentClean
    ) -> None:
        """
        write-through： **DB 提交成功之后** 才写快路径。
        顺序反了就会缓存住一个根本没落库的版本 —— 那是"漏更新"，很难查。
        """
        await cache_set(
            self.redis,
            document_version_key(source, external_id),
            {"clean_id": clean_row.id, "content_hash": clean_row.content_hash},
            ttl=settings.cache_ttl,
        )

    # ---------- 写：身份路径（阶段 F）----------

    @staticmethod
    def _identity_lock_id(source: str, external_id: str) -> str:
        """
        身份锁的 ID。 **必须按身份锁、不能按内容锁** ——
        同一篇文档"改了内容"前后是两个不同的 hash，按 hash 锁等于各拿各的锁，根本拦不住。
        """
        return f"src:{source}:{external_id}"

    async def _apply_identified(
        self, raw: dict, cleaned: dict
    ) -> tuple[DocumentClean, bool, bool]:
        """
        身份路径的核心： **一个事务里「读身份 → 决定 → 写」** 。
        返回 (文档, 是否新建, active 集合有没有变动)。

        并发上分三层（形状与既有的「双层去重」一致）：

        - 快路径：Redis 身份锁（把并发同篇变成 **快速 409** ）
        - **权威** ：下面这把 ``SELECT ... FOR UPDATE`` **行锁** —— 真正串行化更新；
          ``bulk_ingest`` 没有 Redis 锁，就全靠它
        - 兜底：``UNIQUE(source, external_id)`` —— 并发 **创建** 同一身份时拦下

        **调用方负责拿锁** （单篇拿 Redis 锁；批量有意不拿，省往返）。
        """
        source, external_id = raw["source"], raw["external_id"]
        content_hash = cleaned["content_hash"]
        # 已存在身份的路径（更新 / 复活）撞车时报 409；新建撞车回 200 指向已有那篇。
        # 差别在于：新建时"这篇已经在库里了"是个正常答案；更新时回 200 却会静默撒谎
        # （调用方以为更新成功，其实文档还停在旧内容）。
        conflict_is_409 = False

        try:
            src = await self.repo.get_source(source, external_id, for_update=True)
            current = None
            if src is None:
                # ---- 新建：先问一句「这份内容是不是已经属于别人了」----
                # 快路径（Redis SADD）只是省一次查库；真正的权威是下面落库时的唯一索引。
                if not await check_and_mark(self.redis, content_hash):
                    existing = await self._find_existing(content_hash)
                    if existing is not None:
                        # 用 commit 收尾而不是 rollback —— 本事务只读过，
                        # 而 rollback 会把已加载对象的属性全部过期，之后 FastAPI
                        # 在同步上下文里取属性就 MissingGreenlet。
                        await self.db.commit()
                        await incr_rejected(self.redis)
                        logger.info("准入检查 → 内容已属于别人 hash=%s", content_hash[:12])
                        return existing, False, False
                    # Redis 说"见过"、索引里却没有 → 旧占位的幽灵（罕见、自愈），继续按新建走
                src = await self.repo.insert_source(source, external_id)
                created, changed = True, True
            else:
                current = await self.repo.get_active_version(src.id)
                if current is not None and current.content_hash == content_hash:
                    # 内容没变 —— 不是错误，回【当前那一版】
                    # 同上：用 commit 而不是 rollback，避免属性被过期。
                    await self.db.commit()
                    await incr_rejected(self.redis)      # 它确实挡掉了一次重复同步
                    logger.info("身份判重 → 内容未变 ext=%s", external_id)
                    return current, False, False

                created, changed = False, True
                conflict_is_409 = True       # 已存在身份 → 内容撞车是冲突，不是"已存在"
                if src.deleted_at is not None:
                    # 源系统删过、又推回来了 → 复活
                    src.deleted_at = None
                    logger.info("身份复活 ext=%s", external_id)

            clean_row = await self._persist(raw, cleaned, source_id=src.id)

            if current is not None:
                # 作废旧版本：它的块一并作废；它的内容也要释放 ——
                # 旧内容不再 active 了，占着位会把"其实已经不在语料里"的内容一直判成重复。
                # 撤回任务不用在这里创建 —— claim_retractions 按「已 done 且未 retracted」自己筛。
                await self.repo.supersede_version(current.id)
                await self.chunk_repo.supersede_by_document(current.id)
                await self.dedup_repo.delete_by_content_hash(current.content_hash)

            await self.db.commit()
        except IntegrityError:
            # 【必须 rollback】：不回滚，session 就废了，后续任何操作都会报错。
            # 注意：更新路径上"释放旧内容"也在这个事务里，回滚会把它一并撤销 ✓。
            await self.db.rollback()
            await release_mark(self.redis, content_hash)     # 撤掉刚做的快路径占位
            existing = await self._find_existing(content_hash)
            if existing is not None:
                await incr_rejected(self.redis)
                if conflict_is_409:
                    logger.info("内容撞车（更新/复活）hash=%s", content_hash[:12])
                    raise ContentConflictError() from None
                logger.info("准入检查（唯一索引兜底）→ 内容已属于别人 hash=%s", content_hash[:12])
                return existing, False, False
            # 否则是并发创建同一身份 → 唯一约束拦下。报 409，让同步器下一轮重推。
            logger.warning("身份并发创建冲突 ext=%s", external_id)
            raise DocumentInProgressError() from None

        # Redis 快路径 best-effort 跟上（DB 是权威；不一致会自愈）
        if current is not None:
            await release_mark(self.redis, current.content_hash)   # 旧内容不再 active
        await check_and_mark(self.redis, content_hash)             # 新内容占位（幂等）

        await self._remember_version(source, external_id, clean_row)   # write-through 快路径
        logger.info(
            "%s 身份路径 ext=%s clean_id=%s",
            "新增" if created else "更新", external_id, clean_row.id,
        )
        return clean_row, created, changed

    async def delete_document(self, payload: DocumentDelete) -> bool:
        """
        删除信号（ **幂等** ，返回本次是否真的删掉了）。

        和更新一样 **必须拿同一把身份锁** —— 否则会和 ingest 交错出
        「active 版本还在、``deleted_at`` 却已置上」，那条「deleted ⇒ 没有 active 版本」
        的不变式就破了（读路径靠它才不用 join 到 document_source）。

        一个事务里：作废 active 版本 + 它的块 + **释放它的内容占位** → 置 ``deleted_at`` → bump 列表版本号。
        """
        lock_id = self._identity_lock_id(payload.source, payload.external_id)
        released_hash: str | None = None      # 被释放占位的那份内容，删完要在 Redis 里也放行
        try:
            async with document_lock(self.redis, lock_id):
                src = await self.repo.get_source(
                    payload.source, payload.external_id, for_update=True
                )
                if src is None or src.deleted_at is not None:
                    await self.db.rollback()        # 没这个身份 / 已经删过 → 幂等返回
                    return False

                current = await self.repo.get_active_version(src.id)
                src.deleted_at = utcnow()
                if current is not None:
                    await self.repo.supersede_version(current.id)
                    await self.chunk_repo.supersede_by_document(current.id)
                    # 文档都没了，它的内容不该继续占着「准入」的位
                    await self.dedup_repo.delete_by_content_hash(current.content_hash)
                    released_hash = current.content_hash
                await self.db.commit()
        except LockNotAcquired:
            logger.info("未拿到锁（同身份正在处理）ext=%s", payload.external_id)
            raise DocumentInProgressError() from None

        if released_hash is not None:
            await release_mark(self.redis, released_hash)
        # 必须把快路径也清掉：否则下次推同样的内容会命中缓存，把"已删除的版本"直接回给调用方
        await cache_delete(
            self.redis, document_version_key(payload.source, payload.external_id)
        )
        await bump_list_version(self.redis)
        logger.info("文档已删除 source=%s ext=%s", payload.source, payload.external_id)
        return True

    async def bulk_ingest(self, payloads: list[DocumentCreate]) -> BulkResult:
        """
        批量摄入（同步器的真实调用方式）。 **与单篇走同一条路** ，只是不拿 Redis 锁：

        1. 每篇先过 **身份版本快路径** —— 重推没变的文档在这一步就被挡掉，不查库、不加锁
        2. 剩下的逐篇走 ``_apply_identified``， **不加分布式锁** ：
           一次加锁 ≈ 2 次往返/篇，会把快路径省下的又加回去；
           正确性交给 ``document_source`` 那一行的 **行锁 + 唯一约束**
        3. 重复 **按条跳过** 、不报错：同步器周期重推是正常行为

        按 **下标** 把结果填回同一个 ``items``，所以出参顺序与请求一致。
        """
        total = len(payloads)
        items: list[BulkItemResult | None] = [None] * total

        # ① 先全部标准化（纯函数、便宜），顺带挑出空正文的
        prepared: list[tuple[int, dict, dict]] = []   # (index, raw, cleaned)
        # TODO: enumerate
        #   遍历可迭代对象时，同时拿到「下标」和「元素」
        for i, payload in enumerate(payloads):
            raw = payload.model_dump()
            cleaned = normalize_document(raw)
            if not cleaned["content"]:
                items[i] = BulkItemResult(index=i, status="rejected")
                continue
            prepared.append((i, raw, cleaned))

        created = updated = duplicates = 0

        for i, raw, cleaned in prepared:
            # ② 快路径：重推没变的文档在这一步就打住（0 次查库、0 次加锁）
            fast = await self._fast_skip(raw, cleaned)
            if fast is not None:
                duplicates += 1
                items[i] = BulkItemResult(index=i, status="duplicate", id=fast.id)
                continue

            # ③ 库路径：逐篇走身份判断（不加 Redis 锁 —— 权威是那把行锁 + 唯一约束）
            try:
                clean_row, was_created, changed = await self._apply_identified(raw, cleaned)
            except Exception:
                await self.db.rollback()
                logger.exception("批量落库失败 index=%d ext=%s", i, raw.get("external_id"))
                items[i] = BulkItemResult(index=i, status="rejected")
                continue

            if not changed:
                duplicates += 1
                items[i] = BulkItemResult(index=i, status="duplicate", id=clean_row.id)
            elif was_created:
                created += 1
                items[i] = BulkItemResult(index=i, status="created", id=clean_row.id)
            else:
                updated += 1
                items[i] = BulkItemResult(index=i, status="updated", id=clean_row.id)

        if created or updated:
            await bump_list_version(self.redis)
        logger.info(
            "bulk_ingest 完成 收到=%d 新建=%d 更新=%d 重复=%d",
            total, created, updated, duplicates,
        )
        return BulkResult(
            received=total,
            created=created,
            updated=updated,
            duplicates=duplicates,
            rejected=total - created - updated - duplicates,
            items=[it for it in items if it is not None],
        )
