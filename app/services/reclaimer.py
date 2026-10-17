"""
独立的回收任务：把卡在 processing 太久的 **放回 pending** 。

【为什么从 claim 里拆出来】
之前挂在认领里（谁认领谁顺手回收），好处是不用额外跑定时任务；
代价是 **长时间没人来认领，卡住的任务就一直是卡着的** —— 而「没人来认领」
恰恰是 worker 全挂了的典型症状，那时回收反而最该跑。拆成独立循环就解开了这个死结。

【拆出来之后新暴露的两个坑 —— 后台任务独有】

1. **异常必须吞掉** ：循环体里一次数据库抖动就会把这个 task 彻底弄死，
   之后回收静默停摆、而且 **没有任何报错** 。所以每轮都 try/except。
2. **要能优雅关停** ：lifespan 关停时先 cancel 并等它退出，再去关连接池；
   顺序反了会出现「池已 dispose、任务还在用」。

【它回收两处 —— 同一个道理，只是对象不同】

 - ``document_chunk``：worker 领了 chunk 去向量化，然后崩了
 - ``document_clean``：enricher 领了文档去补 enrich，然后崩了（阶段 H 新增）

两处都是" **领了活的人没了，这条就永远卡在 processing** "。
"""
import asyncio
import logging
from datetime import timedelta

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.repositories.chunk_repo import ChunkRepo
from app.repositories.document_repo import DocumentRepo
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


async def reclaim_once() -> int:
    """跑一次回收（chunk + 文档的 enrich），返回 **总共** 回收了几条。

     **自己开 session** —— 后台任务没有请求级的依赖可借。
    """
    older_than = utcnow() - timedelta(seconds=settings.reclaim_after_seconds)
    async with AsyncSessionLocal() as db:
        chunks = await ChunkRepo(db).reclaim_stale(older_than)
        docs = await DocumentRepo(db).reclaim_stale_enrich(older_than)
    return chunks + docs


async def reclaim_loop() -> None:
    """后台循环：每 reclaim_interval_seconds 秒回收一次。"""
    logger.info("回收任务已启动，每 %s 秒一次", settings.reclaim_interval_seconds)
    while True:
        await asyncio.sleep(settings.reclaim_interval_seconds)
        try:
            n = await reclaim_once()
            if n:
                logger.warning(
                    "回收了 %d 条卡在 processing 的活（chunk 或文档 enrich —— 领活的进程可能挂了）", n
                )
        except asyncio.CancelledError:
            raise                   # 取消要原样抛出（CancelledError 是 BaseException，本就不会被下面吞掉）
        except Exception:
            logger.exception("回收失败（不影响主流程，下一轮再试）")
