# AI 舆情数据管道 API

一个用于学习「生产级 FastAPI 工程结构」的练手项目：接收舆情数据 → 清洗 → 去重 → 入库 → 分页查询 → 统计。

- **接口**：`POST /api/v1/articles`、`GET /api/v1/articles`、`GET /api/v1/articles/stats`
- **核心机制**：三表管道（raw→clean→dedup）+ `content_hash` 唯一索引 + Redis Set 去重快路径 + 按 hash 的分布式锁 + Cache-Aside 缓存
- **学习目标**：从「平铺文件」进阶到**分层架构**（repository / service / `APIRouter` / 依赖注入 / 异常处理 / 日志 / Docker）

> 本项目**主要由你自己手写**，助手只讲流程与思路、卡住时辅助。逐阶段的实现指引见 [`docs/开发流程.md`](docs/开发流程.md)。

---

## 技术栈

| 类别 | 选型 |
|---|---|
| Web 框架 | FastAPI |
| 数据库 | MySQL 8 + SQLAlchemy 2.0 Async + aiomysql |
| 缓存 / 去重 / 锁 | Redis 7 + `redis.asyncio` |
| 配置 | pydantic-settings（`.env` 驱动） |
| 容器 | Docker + Docker Compose（api + mysql + redis） |
| 测试（末段） | pytest + pytest-asyncio |

---

## 目录结构

```
ai-public-opinion-pipeline-api/
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI 入口，lifespan 关闭 redis/engine
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py                 # get_db / get_redis / get_service
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── router.py           # 聚合各 endpoints 路由
│   │       └── endpoints/
│   │           ├── __init__.py
│   │           ├── articles.py     # POST /articles, GET /articles
│   │           └── stats.py        # GET /articles/stats
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py               # pydantic-settings
│   │   ├── logging.py              # 日志配置
│   │   └── exceptions.py           # 自定义异常 + 异常处理器
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py                 # DeclarativeBase
│   │   ├── session.py              # create_async_engine + async_sessionmaker + get_db
│   │   └── init_db.py              # 异步建表 create_all
│   ├── infra/
│   │   ├── __init__.py
│   │   └── redis_client.py         # redis.asyncio.Redis + get_redis
│   ├── models/
│   │   ├── __init__.py
│   │   ├── article_raw.py          # 原始提交（留痕）
│   │   ├── article_clean.py        # 清洗后正文
│   │   └── article_dedup.py        # 去重表：content_hash 唯一索引
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── article.py              # 请求/响应模型
│   │   ├── stats.py
│   │   └── common.py               # 分页信封等通用模型
│   ├── repositories/
│   │   ├── __init__.py
│   │   ├── article_repo.py         # async DB 查询/写入（含聚合）
│   │   └── dedup_repo.py           # 去重表读写
│   ├── services/
│   │   ├── __init__.py
│   │   ├── article_service.py      # 管道编排：清洗→去重→入库
│   │   ├── clean_service.py        # 纯函数文本清洗
│   │   ├── dedup_service.py        # async Redis + MySQL 去重
│   │   ├── cache_service.py        # async Redis 缓存 + 版本号
│   │   └── lock_service.py         # async Redis 分布式锁
│   └── utils/
│       ├── __init__.py
│       ├── hashing.py              # content_hash
│       └── time.py                 # utcnow / 日期范围
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # AsyncClient + async DB/Redis fixtures
│   ├── test_articles_api.py
│   ├── test_dedup.py
│   └── test_stats_api.py
├── docs/
│   └── 开发流程.md                  # 逐阶段实现指引
├── docker-compose.yml              # api + mysql + redis
├── Dockerfile                      # api 镜像
├── .dockerignore
├── .env                            # 真实配置（不提交）
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
└── pytest.ini                      # 末段才用
```

---

## 分层职责（一句话记住每层干嘛）

| 层 | 职责 | 不做什么 |
|---|---|---|
| `api/endpoints` | 定义路由、校验入参、返回响应 | 不写业务逻辑、不直接碰 DB |
| `api/deps` | 组装依赖（session、redis、service） | — |
| `services` | 业务编排（清洗→去重→入库、缓存、锁） | 不直接写 SQL |
| `repositories` | 所有 DB 读写、聚合查询 | 不含业务判断 |
| `models` | ORM 表结构 | — |
| `schemas` | 出入参的 Pydantic 模型 | — |
| `core` | 配置、日志、异常 | — |
| `infra` | 外部连接（Redis） | — |
| `utils` | 无状态的纯工具函数 | 无 IO、无全局状态 |

数据流：`endpoint` → `service` → `repository` → `DB`；缓存/去重/锁走 `infra` 的 Redis。

---

## 快速开始

> 下面的命令等你按 `docs/开发流程.md` 的阶段 0 写好文件后再跑。

### 1. 准备配置

```bash
cp .env.example .env      # 然后按需修改（数据库密码、端口等）
```

### 2. 起基础设施（MySQL + Redis + api）

```bash
docker compose up -d
docker compose ps         # 三个服务都应为 healthy / running
```

浏览器打开 **http://localhost:8000/docs** 查看 Swagger。

### 3. 本地开发（改代码热重载，可选）

容器里的 api 不吃热重载。想边写边跑，可以让 MySQL/Redis 留在容器里，api 用本地 uvicorn：

```bash
docker compose up -d mysql redis
pip install -r requirements.txt
# 此时 .env 里指向宿主机映射端口：
#   DB_HOST=localhost  DB_PORT=3307
#   REDIS_HOST=localhost  REDIS_PORT=6380
uvicorn app.main:app --reload --port 8000
```

> 容器内运行（compose 的 api 服务）则用服务名连：`DB_HOST=mysql`、`REDIS_HOST=redis`。**同一份 `config.py` 靠环境变量切换**——这就是 pydantic-settings 的意义。

### 端口说明（重要）

容器内的 MySQL/Redis 是 3306/6379，但**映射到宿主机的 3307/6380**，目的是避开你本机可能已经在跑的 MySQL(3306)/Redis(6379)。

- 检测冲突：`netstat -ano | findstr 3306`（或 6379）
- 宿主机连容器：用 **3307 / 6380**
- 容器内互连：用服务名 `mysql:3306` / `redis:6379`

---

## 接口一览

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/v1/articles` | 提交舆情数据 → 存原始 → 清洗 → 去重 → 入库（重复返回 `409`） |
| GET | `/api/v1/articles` | 分页查询，支持 `page/limit/source/q`，走 Redis 缓存 |
| GET | `/api/v1/articles/stats` | 统计：总数、按来源 top10、今日新增、判重拒收数 |

---

## 开发路线图

共 10 个阶段，每段都能独立跑通，细节见 `docs/开发流程.md`：

```
0 骨架+基础设施  1 数据层  2 模型与 schema  3 API 骨架+最小查询  4 清洗(纯函数)
5 去重入库  6 分布式锁  7 列表缓存  8 统计  9 异常/日志/AI 接缝  10 补测试
```

## 排错提醒

服务起不来 / 页面一直转圈时，**先查端口占用和 python 进程**（Windows + `uvicorn --reload` 容易留下孤儿 worker 占住端口），别先怀疑业务代码。停服务请用 `Ctrl+C` 并等它打印退出信息，不要直接关终端窗口。
