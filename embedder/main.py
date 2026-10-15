"""
模拟向量化 worker：认领 chunk → （模拟）embedding 调用 → 上报完成。

要点：
- 用 `/chunks/claim` 认领（`SKIP LOCKED`，多实例并发认领不会拿到同一条）
- 每条之间 sleep 一下，模拟 embedding 这种【秒级外部调用】—— 这正是
  「一次同步几千篇、同步等不可行」的由来，也是任务分发的意义
- 一个进程复用一个 `httpx.AsyncClient`
"""
import asyncio
import logging
import os

import httpx

API_BASE = os.getenv("API_BASE", "http://api:8000")
CLAIM_LIMIT = int(os.getenv("CLAIM_LIMIT", "20"))
EMBED_SLEEP = float(os.getenv("EMBED_SLEEP", "0.05"))   # 模拟单块 embedding 的耗时
IDLE_SLEEP = float(os.getenv("IDLE_SLEEP", "2"))        # 没活干时的轮询间隔

logging.basicConfig(level=logging.INFO, format="%(asctime)s [embedder] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("embedder")


async def main() -> None:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as client:
        logger.info("向量化 worker 已启动 -> %s", API_BASE)
        while True:
            try:
                r = await client.post("/api/v1/chunks/claim", json={"limit": CLAIM_LIMIT})
                chunks = r.json()["chunks"]
                if not chunks:
                    await asyncio.sleep(IDLE_SLEEP)
                    continue

                for c in chunks:
                    await asyncio.sleep(EMBED_SLEEP)                     # 模拟 embedding 调用
                    await client.post(f"/api/v1/chunks/{c['id']}/complete")
                logger.info("完成 %d 块", len(chunks))
            except Exception:
                logger.exception("认领或上报失败，等一会儿再试")
                await asyncio.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    asyncio.run(main())
