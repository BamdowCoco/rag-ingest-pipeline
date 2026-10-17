"""
应用配置（pydantic-settings）

思路：Settings 里只写「拆开的原子字段」(host/port/user…)，DSN 用 @property 现拼 ——
口令里的特殊字符要转义，而且不必在配置里再存一份拼好的串。

**环境只有一种口径：容器。** 这个项目只在 ``docker compose`` 里跑
（Windows 本地装不了 Redis，也没必要装），所以 host/port 一律是 compose 的服务名
（``mysql:3306`` / ``redis:6379``）—— 由 compose 的 ``environment:`` 指定，见 ``docker-compose.yml``。
"""
from typing import Literal
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",            # .env 只放账号 / 口令 / 业务参数；host/port 由 compose 的 environment 给
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
    # 默认值就是容器口径（compose 的服务名）：缺环境变量时也不会连错地方
    db_host: str = "mysql"
    db_port: int = 3306
    db_user: str = "app"
    db_password: str = "app"
    db_name: str = "rag_ingest"

    # ---------- Redis ----------
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0

    # ---------- 业务参数 ----------
    cache_ttl: int = 60          # 列表/统计缓存秒数（阶段 7、8）
    lock_ttl_ms: int = 10000     # 分布式锁毫秒级 TTL（阶段 6）

    # ---------- 后台任务 ----------
    reclaim_interval_seconds: int = 60    # 回收任务多久跑一次
    reclaim_after_seconds: int = 300      # processing 卡住超过多久算 worker 挂了

    # ---------- enrich（阶段 H）----------
    # provider：rule（纯 Python）/ model（自定契约的 HTTP 上游）/ openai_compat（OpenAI 兼容）/ off
    # 用 Literal 而不是裸 str —— 写错值启动就报错，不会静默变成"谁也不知道的模式"
    enrich_provider: Literal["rule", "model", "openai_compat", "off"] = "rule"
    enrich_model_url: str = "http://model:8000/enrich"   # `model` 档：上游地址（契约由我方定）
    # `openai_compat` 档：基址 + 端点路径 + 要哪个模型 + 可选的 key
    enrich_openai_base_url: str = "http://model:8000"
    # 端点路径做成可配：多数厂商是 `/v1/chat/completions`（DeepSeek / 通义 / Kimi / OpenAI 都是），
    # 但不是所有 —— 比如智谱 GLM 是 `/api/paas/v4/chat/completions`。写死一个就会挡掉一整家。
    enrich_openai_path: str = "/v1/chat/completions"
    enrich_model_name: str = "fake-model"
    # key 是可选的：自建推理（Ollama / vLLM）不需要，云厂商兼容端点才需要
    enrich_api_key: str = ""
    # 有些厂商支持 `response_format: {"type": "json_object"}`（DeepSeek / 通义 / GLM）——
    # 开了它模型更不容易跑偏。但不是所有兼容实现都支持，不支持会 400 → 整档不可用，所以默认关。
    enrich_json_mode: bool = False
    enrich_max_tokens: int = 512         # 摘要不该无限长；也是成本闸门
    enrich_sample_limit: int = 4000      # 发给上游前先截到这个长度（首段 + 尾段）
    enrich_timeout_seconds: float = 5.0
    # 后台补的节奏 —— 与 reclaim_* 并列，都是运维参数
    enrich_interval_seconds: int = 30
    enrich_batch_size: int = 50

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
