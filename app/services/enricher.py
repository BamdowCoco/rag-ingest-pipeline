"""
后台补 enrich 的循环（阶段 H）。

【为什么必须有它】模型调用是 **秒级外部依赖** ，绝不能压在请求路径里：

 - 单篇 POST 会因此慢好几秒；
 - 而 ``bulk_ingest`` 是 **逐篇** 走的 —— 同步器一次推 200 篇，这个请求就要 3 秒 × 200 ≈ **10 分钟**，
   而"同步器批量推送"恰恰是本项目的门面场景。

所以文档落库时只是 ``enrich_status='pending'``，真正的补全交给这个循环。

【和 reclaimer 的关系】两个都是应用内的后台 task（lifespan 起停），形状一样、职责不同：

 - ``reclaimer``：把 **卡住的** 放回去（chunk 的 processing、文档的 enrich processing）
 - 这个循环：把 **待办的** 做掉

【多实例】认领走 ``SKIP LOCKED``，所以 ``--scale api`` 时多个进程各跑一个循环也不会重复调模型。

【不做重试】一次补失败就标 done、几列留 NULL —— 意思是"**试过了**"。
不这么做它会一直留在 pending、每轮被重新认领 → **无限重试（毒丸）**。
真要重试得配重试计数 + 退避，超出本段范围（已记在已知缺口里）。
"""
import asyncio
import logging

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.repositories.document_repo import DocumentRepo
from app.services.enrich_service import enrich

logger = logging.getLogger(__name__)


async def enrich_once(limit: int | None = None) -> int:
    """认领一批、逐条补、写回。返回这次**补完**了几条。"""
    if settings.enrich_provider == "off":
        # 关掉了就别去认领 —— 让它们留在 pending：
        # "待办积压"比"写一堆 NULL"更诚实，而且开关一打开它们还能被补上
        return 0

    async with AsyncSessionLocal() as db:
        repo = DocumentRepo(db)
        rows = await repo.claim_pending_enrich(limit or settings.enrich_batch_size)

        done = 0
        for row in rows:
            # `enrich` 自己从不抛（内部按 provider 分派 + 逐项降级 + 模型失败回落规则），
            # 所以这里不 catch —— 真抛出来只可能是**数据库**病了，那就整轮放弃，
            # 认领了却没写回的会被 reclaimer 放回去 ✓
            ai = await enrich({"content": row.content}, {"author": row.author})
            await repo.mark_enrich_done(row.id, ai)
            done += 1

        if done:
            logger.info("补完 %d 篇文档的 enrich（provider=%s）", done, settings.enrich_provider)
        return done


async def enrich_loop() -> None:
    """后台循环：每 ``enrich_interval_seconds`` 秒补一轮。"""
    logger.info(
        "enrich 后台补任务已启动，每 %s 秒一轮（provider=%s）",
        settings.enrich_interval_seconds,
        settings.enrich_provider,
    )
    while True:
        await asyncio.sleep(settings.enrich_interval_seconds)
        try:
            await enrich_once()
        except asyncio.CancelledError:
            raise                   # 取消要原样抛出（CancelledError 是 BaseException，本就不会被下面吞掉）
        except Exception:
            logger.exception("enrich 后台补失败（不影响主流程，下一轮再试）")
