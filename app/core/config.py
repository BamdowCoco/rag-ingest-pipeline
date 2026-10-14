"""
应用配置（pydantic-settings）

思路：Settings 里只写「拆开的原子字段」(host/port/user…)，DSN 用 @property 现拼。
这样本地开发(localhost:3307)与容器运行(mysql:3306)共用同一份代码，只靠环境变量切换
"""
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",            # 本地跑 uvicorn 时读它；容器里靠 compose 的 environment 覆盖
        env_file_encoding="utf-8",
        extra="ignore",             # .env 里有无关的键也不报错
        case_sensitive=False,       # 大小写忽略
    )

    # ---------- 应用 ----------
    app_name: str = "企业知识库 RAG 接入管道"
    debug: bool = False
    api_port: int = 8000
    log_level: str = "INFO"

    # ---------- MySQL ----------
    # 默认值按「本地开发连宿主机映射端口」设；容器内 compose 用 environment 覆盖成 mysql:3306
    db_host: str = "localhost"
    db_port: int = 3307
    db_user: str = "app"
    db_password: str = "app"
    db_name: str = "rag_ingest"

    # ---------- Redis ----------
    redis_host: str = "localhost"
    redis_port: int = 6380
    redis_db: int = 0

    # ---------- 业务参数 ----------
    cache_ttl: int = 60          # 列表/统计缓存秒数（阶段 7、8）
    lock_ttl_ms: int = 10000     # 分布式锁毫秒级 TTL（阶段 6）

    # ---------- 后台任务 ----------
    reclaim_interval_seconds: int = 60    # 回收任务多久跑一次
    reclaim_after_seconds: int = 300      # processing 卡住超过多久算 worker 挂了

    # ---------- 派生 DSN（用 @property 现拼，不重复存字段） ----------
    @property
    def database_url(self) -> str:
        # 密码里可能出现 @ : / 等字符，必须转义，否则 URL 会被解析错
        pwd = quote_plus(self.db_password)
        return (
            f"mysql+aiomysql://{self.db_user}:{pwd}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


# 模块级单例：全局共享一份
settings = Settings()
