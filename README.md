# 企业知识库 RAG 数据接入与预处理管道

> 一套企业知识库的**数据接入与预处理管道**。多个同步器（Confluence / 飞书 / 语雀 / 本地上传）把文档推进来，管道负责**标准化 → 内容去重 → 落库 → 切块 → 派发向量化任务**，为下游的向量检索与 RAG 提供干净、切好块、带状态的数据。**去重挡掉重复同步，省的是真金白银的 embedding 调用。**

---

## 业务背景

企业知识库的数据是这么进来的：

- **多源同步**：同一份文档往往同时挂在飞书和 Confluence 上，两个同步器都会推过来
- **反复同步**：同步器按周期跑（每天/每小时），**文档没变也会再推一遍**
- **体量大**：一次全量同步可能来几千篇；每篇要切块、每块要向量化

如果不做处理，下游会收到大量重复文档，**把同一份内容反复切块、反复调 embedding**——而 embedding 调用是**按量收费**的。重复同步的代价是真金白银。

**本服务就是接入层**：接收 → 标准化 → 内容指纹去重 → 落库 → 切块 → 标记待向量化。对外提供文档查询、分块查询，以及给下游 worker 的**任务认领**接口。

**两个核心指标**：**去重率**（挡掉了多少重复同步）和**向量化积压**（还有多少 chunk 没处理）。

---

## 接口一览

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/v1/documents` | 提交单篇文档。**新建 → `201`；内容已存在 → `200`（去重，不重复切块）** |
| POST | `/api/v1/documents/bulk` | **批量提交**（同步器的真实调用方式，上限 200 篇） |
| GET | `/api/v1/documents` | 文档分页查询（`page/limit/source/q`），走 Redis 缓存 |
| GET | `/api/v1/documents/{id}` | 单篇详情，走 Cache-Aside |
| GET | `/api/v1/documents/{id}/chunks` | **分块列表**（分页），走 Cache-Aside |
| GET | `/api/v1/documents/stats` | 统计：文档数、按来源 top10、今日新增、**去重计数**、**chunk 状态分布** |
| POST | `/api/v1/chunks/claim` | **下游 worker 认领待向量化的 chunk**（`SKIP LOCKED`，可并发抢） |
| POST | `/api/v1/chunks/{id}/complete` | worker 上报完成，回写 `embedding_status` |

> **为什么"重复不是错误"**：同步器周期性跑，**文档没变就会再推一遍**，这是**正常行为**。所以语义是「已存在，跳过」，返回 `200` 而不是报错——同步器不必把正常同步当异常处理。

---

## 核心机制

### 1. 内容指纹（去重键）

```
content_hash = sha256(title + "\n" + cleaned_content)
```

判重跟的是**内容**，不是标题、也不是 URL——**同一份文档换个标题再传一遍，照样拦住**。

### 2. 双层去重：快路径 + 权威兜底

| 层 | 角色 | 为什么不能只留一个 |
|---|---|---|
| Redis Set | **快路径**：挡掉绝大多数重复，省一次数据库往返 | 但它会被清空 / 淘汰 / 重启丢数据，**不能当真相** |
| MySQL 唯一索引 | **权威**：`document_dedup.content_hash` 唯一 | 但它慢，且同步洪峰下每次查库不划算 |

**这里去重省下的不只是数据库往返，还有下游的 embedding 费用**——所以它值得做两层。

### 3. 批量摄入：把 N 次判重往返压成 1 次（pipeline）

同步器一次推几百篇（`POST /documents/bulk`），**逐条**判重就是几百次 Redis 往返。
`bulk_ingest` 用 `pipeline` 把这些 `SADD` **一次发出去**：

```python
pipe = redis.pipeline()
for h in hashes:
    pipe.sadd("dedup:documents", h)     # 每条 SADD 各自返回 1 / 0
flags = await pipe.execute()            # ← 一次往返，拿到逐条的「是否新增」
```

**N 次往返 → 1 次**。而且每条 `SADD` 仍然是**原子的**——pipeline 只是把命令打包发送，
并没有把多条命令合成一个非原子操作，所以不存在 `SISMEMBER` + `SADD` 那种两步竞态。

> **批量里为什么反而【不】加分布式锁？** 一次加锁 ≈ 2 次往返/篇（`SET NX PX` + Lua 释放），
> 逐篇加锁会把 pipeline 省下的往返**又加回去**。而 `SADD` 本身就是一次**原子的预留**
> （只有一个人拿到 1），唯一索引仍是最终兜底——锁在这里只剩代价、没有收益。
> 单篇则相反：锁能把「并发同篇」从一次数据库往返 + 异常，变成一次**快速失败**，划算。

### 4. 按内容哈希粒度的分布式锁

同步洪峰下**多个同步器会同时推同一份文档**（飞书 + Confluence 同时同步）。锁把「判重 → 落库」这段临界区串行化。

- 锁**按哈希**（`lock:doc:{hash}`），不是全局锁——否则吞吐直接掉到 1
- 释放用 **Lua 原子比对 token**：裸 `DEL` 可能在锁过期后**删掉别人的锁**
- TTL 自动过期是**崩溃安全**的代价：持有者挂了锁最终会自己释放

### 5. 切块（chunking）

不是"每 500 字切一刀"。真实做法有两条讲究：

- **按段落优先，段落过长再按长度硬切**：在自然语义边界切，块的质量高得多
- **相邻块要有 overlap（重叠）**：否则一个句子正好被切在边界上，两边都不完整，检索时谁都匹配不上

每个 chunk 记 `token_count` 与 `embedding_status`，成为下游可消费的**任务单元**。

### 6. 异步：用 `SKIP LOCKED` 做极简任务队列

切块是 CPU 活，**向量化是一次秒级的外部 API 调用**——一次同步几千篇，**同步等就是不可行的**。所以：

```
落库 → chunk 标记 pending → 下游 worker 认领 → 标记 done
```

认领用 MySQL 8 的 **`SELECT ... FOR UPDATE SKIP LOCKED`**：

- 多个 worker 并发抢，**互不阻塞、绝不重复拿到同一条**
- 这和你项目里的**分布式锁形成漂亮的对照**：

  > **锁**是「这件事只能一个人做」（保护临界区）；**`SKIP LOCKED`** 是「这堆活大家分着做」（分发任务）。同一个项目里两种并发原语，各有各的位置。

### 7. Cache-Aside + 版本号失效

- **列表 / 统计**：键里带 `v{version}`，写入时 `INCR documents:list:version` → 旧键**瞬间不可达**（O(1)，不用 `SCAN`）
- **文档详情 / 分块列表**：**一次写入、永不修改** → 用**纯 TTL** 即可，不需要写时失效

> 「失效策略取决于**数据可变性**」——这里文档与 chunk 都不可变，所以纯 TTL 是正确答案，一行失效逻辑都不用写。

### 8. AI 接缝（已留好，未接模型）

`enrich()` 插在**去重之后、落库之前**，给文档补 `doc_type`（手册/纪要/规范）、`tags`、`ai_summary`（检索结果展示用）。铁律：**AI 是可选增强，失败绝不阻断入库**——出错就留 NULL。

---

## 技术栈

| 类别 | 选型 |
|---|---|
| Web 框架 | FastAPI |
| 数据库 | MySQL 8 + SQLAlchemy 2.0 Async + aiomysql |
| 缓存 / 去重 / 锁 | Redis 7 + `redis.asyncio` |
| 配置 | pydantic-settings（`.env` 驱动） |
| 容器 | Docker + Docker Compose（api + mysql + redis + **syncer** + **embedder**） |
| 同步 / Worker | httpx（异步批量推送、认领任务，均可 `--scale` 多实例） |
| 测试 | pytest + pytest-asyncio |

---

## 目录结构

```
rag-ingest-pipeline/
├── app/
│   ├── main.py                      # FastAPI 入口，lifespan 关闭 redis/engine
│   ├── api/
│   │   ├── deps.py                  # get_db / get_redis / get_document_service
│   │   └── v1/
│   │       ├── router.py            # 聚合路由（注意 stats 必须先于 /{id} 注册）
│   │       └── endpoints/
│   │           ├── documents.py     # POST /documents、/bulk、GET /documents、/{id}、/{id}/chunks
│   │           ├── chunks.py        # POST /chunks/claim、/chunks/{id}/complete
│   │           └── stats.py         # GET /documents/stats
│   ├── core/
│   │   ├── config.py                # pydantic-settings
│   │   ├── logging.py
│   │   └── exceptions.py            # 业务异常 + 统一处理器
│   ├── db/                          # Base / engine / session / init_db
│   ├── infra/redis_client.py
│   ├── models/
│   │   ├── document_raw.py          # 原始文档体（留痕，清洗规则改了能重跑）
│   │   ├── document_clean.py        # 标准化后的正文
│   │   ├── document_dedup.py        # 内容指纹表：content_hash 唯一
│   │   └── document_chunk.py        # 分块：chunk_index / content / token_count / embedding_status
│   ├── schemas/                     # DocumentCreate / DocumentOut / ChunkOut / BulkResult / StatsOut / Page
│   ├── repositories/                # document_repo / dedup_repo / chunk_repo
│   ├── services/
│   │   ├── document_service.py      # 编排：标准化 → 加锁 → 判重 → 落库 → 切块 → 派发
│   │   ├── normalize_service.py     # 纯函数：文档标准化（去 HTML/全角/零宽…）
│   │   ├── chunk_service.py         # 纯函数：切块（段落优先 + overlap）
│   │   ├── dedup_service.py         # Redis 快路径 + 去重计数
│   │   ├── lock_service.py          # 分布式锁（Lua 释放）
│   │   ├── cache_service.py         # Cache-Aside + 版本号
│   │   ├── task_service.py          # chunk 任务认领/完成（SKIP LOCKED）
│   │   └── enrich_service.py        # AI 接缝（空实现）
│   └── utils/                       # hashing / time
├── syncer/                          # 同步器（模拟飞书/Confluence 批量推文档）
├── embedder/                        # 向量化 worker（认领 chunk → 标记完成）
├── tests/
├── docs/
├── docker-compose.yml               # api + mysql + redis + syncer + embedder
├── Dockerfile
└── requirements.txt
```

---

## 分层职责

| 层 | 职责 | 不做什么 |
|---|---|---|
| `api/endpoints` | 定义路由、校验入参、返回响应 | 不写业务逻辑、不直接碰 DB |
| `api/deps` | 组装依赖（session、redis、service） | — |
| `services` | 业务编排（标准化→判重→落库→切块、任务分发、缓存、锁） | 不直接写 SQL；**不依赖 HTTP**（业务异常在 `core/exceptions.py` 统一映射） |
| `repositories` | 所有 DB 读写、聚合查询、`SKIP LOCKED` 认领 | 不含业务判断 |
| `models` | ORM 表结构 | — |
| `schemas` | 出入参的 Pydantic 模型 | — |
| `core` | 配置、日志、异常 | — |
| `infra` | 外部连接（Redis） | — |
| `utils` | 无状态的纯工具函数 | 无 IO、无全局状态 |

数据流：`endpoint` → `service` → `repository` → `DB`；缓存/去重/锁走 `infra` 的 Redis。

---

## 快速开始

### 1. 准备配置
```bash
cp .env.example .env      # 按需改数据库口令、端口
```

### 2. 起全套
```bash
docker compose up -d --build
docker compose ps         # 各服务应为 healthy / running
```
浏览器打开 **http://localhost:8000/docs**。

### 3. 手动发一篇
```bash
curl -s -w '\n%{http_code}\n' -X POST http://localhost:8000/api/v1/documents \
  -H 'Content-Type: application/json' \
  --data-binary '{"source":"feishu","title":"部署手册","content":"第一步…第二步…","url":"https://example.com/doc/1"}'
```

> 若宿主访问不通（Windows + WSL 的端口转发问题），改用 `docker compose exec` 从容器内部打——本项目日常验证走的就是这条路：

```bash
docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').status)"
```

> ⚠️ **body 里带中文时，别把 JSON 内联进命令行参数**——Windows 的 git bash 会把 argv 转成 GBK，服务端按 UTF-8 解析失败直接 400。用 `--data-binary @文件` 或 Python 客户端。

### 4. 看完整流水线跑起来（本项目最有说服力的一幕）
```bash
docker compose up -d --scale syncer=2 --scale embedder=3

# 同步器在推文档、worker 在认领 chunk
docker compose logs -f syncer embedder | grep -E '\[syncer\]|\[embedder\]'

# 去重生效：推了很多次，文档只有几个
docker compose exec -T mysql mysql -uroot -proot123456 -e "
  select count(*) as documents from rag_ingest.document_clean;
  select embedding_status, count(*) from rag_ingest.document_chunk group by embedding_status;"
```
期望：**同步器疯狂推、worker 并发认领不重复，`document_clean` 只有几行、chunk 状态从 `pending` 流向 `done`。**

### 端口说明
容器内 MySQL/Redis 是 3306/6379，**映射到宿主机 3307/6380**，避开本机已有的实例。容器内互连用服务名（`mysql:3306` / `redis:6379`）。

---

## 开发路线图

改造分 5 段，每段独立可验证、可叫停（详见 [`docs/改造路线.md`](docs/改造路线.md)）：

- [x] **A** 全量换皮改名：article 域 → document 域（表 / 模型 / 服务 / 接口 / Redis 键）
- [x] **B** 切块：新增 `document_chunk` 表 + 切块服务（段落优先 + overlap），管道接上切块
- [x] **C** 异步任务分发：`/chunks/claim` + `/complete`（`SKIP LOCKED`），worker 可并发认领
- [x] **D** 批量摄入 + 同步器 + worker 容器（`syncer` / `embedder`，可 `--scale`）
- [ ] **E** 叙事重写（README + 架构说明）

---

## 排错提醒

服务起不来 / 页面一直转圈时，**先查端口占用和 python 进程**（Windows + `uvicorn --reload` 容易留孤儿 worker 占住端口），别先怀疑业务代码。停服务用 `Ctrl+C` 并等它退出，不要直接关终端窗口。

另有一个 Windows 特有的坑：`localhost` 会优先解析到 `::1`，而 Docker 的端口转发若被 WSL 中继占住（`wslrelay.exe`），**宿主机会连不上容器**——但容器自己是好的（`docker compose exec` 里自测 200）。遇到时重启 Docker Desktop。
