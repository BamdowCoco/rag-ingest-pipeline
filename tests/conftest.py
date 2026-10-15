"""
测试脚手架：真 MySQL（独立测试库）+ 真 Redis（独立 db 号）+ ASGI 直连。

跑法（在 compose 网络内跑，别在宿主机直连）：
    docker compose --profile test run --rm test

**为什么用真 MySQL / 真 Redis，而不是 SQLite / fakeredis：**
- `claim_pending` 的 `FOR UPDATE SKIP LOCKED` 是 MySQL 专有，SQLite 执行不了
- 锁的释放走 Lua `EVAL`，要真 Redis（省掉 fakeredis[lua] 这个额外依赖）
被测的正是这两处，所以脚手架必须说同一种语言。

**配置从哪来：** compose 的 test 服务把 `DB_NAME=rag_ingest_test`、`REDIS_DB=15` 灌进环境变量，
而 app 的 `settings` / `engine` / `redis_client` 都在 import 时读环境变量 —— 所以测试进程里的
app **本来就指向测试库/测试 redis db** ，不需要 `dependency_overrides`。
"""
import itertools
import os

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.db.init_db import init_models
from app.db.session import engine
from app.infra.redis_client import redis_client
from app.main import app

# 清库顺序无所谓（会临时关掉外键检查），但列全了才不会漏表
_TABLES = ("document_chunk", "document_dedup", "document_clean", "document_raw")


@pytest.fixture(scope="session", autouse=True)
async def prepare_database():
    """
    session 级、只做一次：建测试库 + 授权 + 建表。

    **建库要用 root：** compose 里的 `MYSQL_USER` 只在 `MYSQL_DATABASE`（rag_ingest）上有权限；
    而 `mysql_data` 是命名卷，`docker-entrypoint-initdb.d` 不会重跑 —— 所以测试库得自己建。
    """
    root_pw = os.getenv("DB_ROOT_PASSWORD", "")
    root_url = f"mysql+aiomysql://root:{root_pw}@{settings.db_host}:{settings.db_port}/"
    db = settings.db_name
    root_engine = create_async_engine(root_url, pool_pre_ping=True)
    async with root_engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{db}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
        await conn.execute(text(f"GRANT ALL ON `{db}`.* TO '{settings.db_user}'@'%'"))
        await conn.execute(text("FLUSH PRIVILEGES"))
    await root_engine.dispose()

    await init_models()          # 用 app 自己的 engine（已指向测试库）建表

    yield

    await engine.dispose()
    await redis_client.aclose()


@pytest.fixture(autouse=True)
async def _clean(prepare_database):
    """
    每个测试前清干净：TRUNCATE 四张表 + flush 当前 redis db 号。

    **为什么不用「事务回滚」隔离：** 应用内部到处 `await db.commit()`
    （`ingest`、`chunk_repo` 每次 claim/mark 都提交），外层事务回滚 **撤不掉已提交的写** 。
    改用「建表一次 + 每测试清表」。

    MySQL 不允许 TRUNCATE 被外键引用的表，所以临时关掉外键检查。
    """
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in _TABLES:
            await conn.execute(text(f"TRUNCATE TABLE {t}"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    await redis_client.flushdb()   # 只清当前 db 号（15）；生产 db 0 在同一个容器里，不受影响


@pytest.fixture(scope="session")
async def client():
    """
    ASGI 直连的客户端（不走网络，也不需要起 uvicorn）。

    **注意 `ASGITransport` 不跑 lifespan** —— 所以：
    - 建表靠上面的 `prepare_database`（不是靠 lifespan 里的 `init_models()`）
    - lifespan 里的 `reclaim_loop()` **不会** 启动（回收测试直接调 `reclaim_once()`）
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def redis():
    """共享的 redis 客户端（与 app 用的是同一个）。"""
    return redis_client


@pytest.fixture
def make_doc():
    """造一篇 **内容唯一** 的文档：内容不同 → hash 不同，测试内部不会互相判重。"""
    counter = itertools.count()

    def _make(**overrides) -> dict:
        n = next(counter)
        doc = {
            "source": "feishu",
            "title": f"标题-{n}",
            "content": f"这是第 {n} 篇的正文内容。",
        }
        doc.update(overrides)
        return doc

    return _make
