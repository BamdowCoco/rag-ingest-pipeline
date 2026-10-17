"""
模拟同步器：周期性把「一批文档」推给接入管道（飞书 / Confluence / 语雀）。

要点：

 - 一个进程复用 **一个** ``httpx.AsyncClient`` （连接池），别每轮新建
 - 每轮随机取几篇重推 —— 文档内容固定，所以【下一轮推的就是上一轮的重复】。
   这正是要演示的：同步器周期性跑，文档没变也会再推一遍，去重把重复挡在门外
 - 走 ``/documents/bulk`` 批量入口，而不是逐篇 POST
"""
import asyncio
import logging
import os
import random

import httpx

API_BASE = os.getenv("API_BASE", "http://api:8000")
SYNC_INTERVAL = float(os.getenv("SYNC_INTERVAL", "5"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [syncer] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("syncer")

# 固定几份「文档」—— 不同实例、不同轮次推的都是同样的内容 → 天然产生重复同步。
# `external_id` 是源系统里这篇文档的稳定 ID：有了它，管道才能分辨
# 「文档 X 改过了（更新）」和「这是一篇新文档（新增）」。
DOCS = [
    {
        "source": "feishu",
        "external_id": "feishu-doc-001",
        "title": "部署手册",
        "content": "第一步：准备环境。第二步：执行部署脚本。第三步：验证健康检查是否通过。",
    },
    {
        "source": "confluence",
        "external_id": "conf-page-007",
        "title": "接口规范",
        "content": "所有接口必须返回统一的错误结构；分页参数统一为 page 与 limit；时间一律 UTC。",
    },
    {
        "source": "yuque",
        "external_id": "yuque-note-42",
        "title": "会议纪要",
        "content": "本周讨论了接入管道的去重策略，以及基于 SKIP LOCKED 的向量化任务分发方案。",
    },
]


async def main() -> None:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as client:
        logger.info("同步器已启动，每 %s 秒推一批 -> %s", SYNC_INTERVAL, API_BASE)
        while True:
            batch = random.choices(DOCS, k=random.randint(5, 20))
            try:
                r = await client.post("/api/v1/documents/bulk", json={"documents": batch})
                result = r.json()
                logger.info(
                    "推送 %d 篇 -> %s（新建 %s 重复 %s 拒收 %s）",
                    len(batch),
                    r.status_code,
                    result.get("created"),
                    result.get("duplicates"),
                    result.get("rejected"),
                )
            except Exception:
                logger.exception("推送失败，等下一轮再试")
            await asyncio.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
