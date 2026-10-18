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
| POST | `/api/v1/documents` | 提交文档（**`external_id` 必填**）。**新建 → `201`；内容没变 → `200`；同 id 改了内容 → `200`（更新）** |
| POST | `/api/v1/documents/bulk` | **批量提交**（同步器的真实调用方式，上限 200 篇） |
| POST | `/api/v1/documents/delete` | **删除信号**（按 `source + external_id`，**幂等**，恒回 `200`） |
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

指纹算的是 **标准化后的「标题 + 正文」** —— 不含来源、不含 URL。所以同一篇稿子在别处转载会被拦住。
反过来也要说清： **标题变了，指纹就变了**（那种情况拦不住）—— 公式里带着标题，这是刻意的。

### 2. 双层去重：快路径 + 权威兜底

| 层 | 角色 | 为什么不能只留一个 |
|---|---|---|
| Redis Set | **快路径**：挡掉绝大多数重复，省一次数据库往返 | 但它会被清空 / 淘汰 / 重启丢数据，**不能当真相** |
| MySQL 唯一索引 | **权威**：`document_dedup.content_hash` 唯一 | 但它慢，且同步洪峰下每次查库不划算 |

**这里去重省下的不只是数据库往返，还有下游的 embedding 费用**——所以它值得做两层。

### 3. 更新语义与「文档身份」—— 为什么光有内容指纹不够

一开始只有 `content_hash` 一个键，它同时被当成 **去重键** （这份内容见过吗）和 **身份键** （这是同一篇吗）。
结果 **系统表达不了「更新」** ：文档改了内容 → hash 变了 → 只能新增一行，旧行成孤儿。

所以摄入契约里加了 **`external_id`（源系统的文档 ID，必填）** ，并单独建一张 `document_source` 身份表：
一个源文档一行、`(source, external_id)` 唯一。`document_clean` 从此 **一行 = 一个版本** ，
只有 `superseded_at IS NULL` 的那行是当前版本。于是「同一篇改过了」终于能表达出来：

| 情况 | 行为 |
|---|---|
| 身份不存在 | **新增** （先过下面那道内容准入） |
| 身份在、内容没变 | **跳过** （回 `200`） |
| 身份在、内容变了 | **更新** ：新增一个版本，旧版本与旧 chunk 一并作废 |

> **为什么唯一约束不能直接建在 `document_clean` 上？** 那里同一个 `external_id` 有 **多行版本** ，
> 唯一索引根本立不住。身份和版本是两件事 —— 一张表装不下，所以有了 `document_source`。

**内容去重没被删掉，只是换了位置**：`document_dedup.content_hash` 唯一 + Redis Set 快路径，
现在只在一个 **新文档进门** 时问一句「这份内容是不是已经属于别人了」（多源转载就靠它挡）。
**更新 / 复活是版本操作、不参与内容准入** —— 否则它们的新内容正好与别人一样时，会把自己的更新卡死。

**还有一条「身份版本快路径」**：Redis 里存 `身份 → {clean_id, content_hash}`。
同步器周期性重推**没变**的文档（最主要的那路流量）命中它就直接回 `200` —— **不查库、也不去抢那把
`SELECT ... FOR UPDATE` 行锁**（没这一步，每次重推都要抢锁、还互相排队）。
它沿用「快路径可以错、权威必须在 DB」这个模式，但 **失败模式更重** ：
这份缓存错了是 **「漏一次更新」** ，不是去重 SET 那种「多拒一次」。所以它 **write-through**
（DB 提交成功后才写）+ TTL 兜底 + 删除时清掉。


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

### 8. AI 接缝：从「留了位」到「真的在跑」，再到「可插拔」

`enrich()` 给文档补几列字段（语言 / 摘要 / 关键词 / 署名）。铁律： **AI 是可选增强，失败绝不阻断入库** ——
出错只让 **那一项** 留 NULL（**逐项降级**：语言挂了不连累摘要）。

**默认那四项是纯 Python / 本地词典的**（不依赖任何模型服务）：
- **语言检测** → `language`（`py3langid`；文本太短就留空）
- **简单摘要** → `ai_summary`（ **首段 + 尾段 + 截断** ，不上 textrank、更不上 LLM）
- **关键词** → `tags`（`jieba` 的 TF-IDF）
- **署名** → `author`，**只在正文开头几行**里认 `作者：X` / `文/ X` ——
  正文中间出现的"作者："很可能只是在**引用**别人，**猜错比留空更糟**

**需要更高质量时可以切到模型服务，管道一行不改** —— 一个配置项切四档：

| `ENRICH_PROVIDER` | 行为 | 契约是谁定的 |
|---|---|---|
| `rule`（默认） | 上面那四项纯 Python 规则，**不出网** | — |
| `model` | 调 HTTP 上游：`POST {text} → {summary, keywords, language}`（**网关形态**） | **我方** |
| `openai_compat` | **OpenAI 兼容协议**：`messages` 数组 → `choices[0].message.content`，长度看 `max_tokens`；**路径与 JSON 模式都可配**（`ENRICH_OPENAI_PATH` / `ENRICH_JSON_MODE`） | 协议方（自建 vLLM / Ollama / 云厂商兼容端点） |
| `off` | 什么都不做，几列全 NULL（"能关掉"也是降级开关） | — |

**三个 HTTP 之外的档都自动回落到 `rule`**：上游超时 / 4xx / 5xx / 响应形状不对 → 回落纯 Python → 几列仍有值。
**接第三家厂商只加一个文件**（`app/services/enrich_providers/` 里一个 provider 一个文件），
**表结构、状态机、后台循环、调用方都不用动** —— 这就是"适配层"的含义。

> **`model/` 容器里那个"模型"是替身 —— 替身的是服务端，不是"HTTP 调用"这件事。**
> 它还故意**讲两种契约**（`/enrich` 与 `/v1/chat/completions`），于是
> "适配层到底在翻译什么"不用嘴说：**一个上游、两个适配器、两种解析**。
> 为什么需要适配层、四种上游形态的差别，见
> [`docs/RAG数据接入与预处理管道项目疑问.md`](docs/RAG数据接入与预处理管道项目疑问.md) 的 **Q3** 。

**几处形状上的讲究，都是这几轮改出来的：**

- **它不在请求路径上。** 模型调用是 **秒级外部依赖** ，而 `bulk_ingest` 是 **逐篇** 走的 ——
  留在请求里，"同步器一次推 200 篇"就是 **10 分钟**。所以入库只写 `enrich_status = 'pending'`，
  补全交给 lifespan 起的**后台循环**：认领（`SKIP LOCKED`）→ 补 → 写回，形状与 chunk 的任务队列**完全一致**，
  也共用同一套**超时回收**（补它的进程挂了，卡在 `processing` 的会被放回 `pending`）。
- **`enrich_version` 记下这一行是哪一版规则补的** —— 与 `clean_version` 一个思路：
  **规则改了，得知道哪些行该重跑**。
- **只发样本，不发整篇。** 摘要本来只用首尾段，而调用按 **输入全长** 计费 ——
  所以发之前先取"首段 + 尾段"（`app/utils/sample.py`，超长时两头截），长文档不再白烧 token。
- **解析失败要抛，不许默默留 NULL。** 响应形状不对是"**接法错了**"，不是"这篇文档没有摘要" ——
  静默留空会把这两件事混成一个，排查时无从下手。抛出去正好走降级链。

> **上游挂了会怎样？** 入库照样成功 —— 降级链把它兜住了。这也是 `model` 服务在
> `docker-compose.yml` 里 **故意不写 `depends_on`** 的原因：它挂了，整套照常起、照常收文档。
> 现场验收（`ENRICH_PROVIDER` 走 `.env`，改完要重建 api 容器它才读得到新的环境变量；
> **跑测试前记得把它删掉** —— 测试容器也读 `.env`）：
> ```bash
> 把 `.env` 里的 `ENRICH_PROVIDER` 改成 `openai_compat`，再 `docker compose up -d api`（改完要重建容器它才读得到）
> # 发一篇文档 → 等后台补完 → 摘要里会出现样本标记 [中间省略]（规则侧不可能产出它）
> docker compose stop model      # 把上游停掉
> # 再发一篇 → 依然 201 入库；那几列是规则算出来的值（摘要里带 " … "、tags 是 jieba 真词）
> ```

---

## 技术栈

| 类别 | 选型 |
|---|---|
| Web 框架 | FastAPI |
| 数据库 | MySQL 8 + SQLAlchemy 2.0 Async + aiomysql |
| 迁移 | Alembic（启动时 `upgrade head`） |
| 缓存 / 去重 / 锁 | Redis 7 + `redis.asyncio` |
| 配置 | pydantic-settings（`.env` 驱动） |
| 容器 | Docker + Docker Compose（api + mysql + redis + **syncer** + **embedder** + **model**） |
| 同步 / Worker | httpx（异步批量推送、认领任务、调模型服务；前两者可 `--scale` 多实例） |
| AI 增强 | `py3langid`（语言）+ `jieba`（关键词）+ 本项目的极简摘要 / 署名规则；**可选**换成 HTTP 上游（含 **OpenAI 兼容**协议，只加一个 provider 文件） |
| 测试 | pytest + pytest-asyncio |

---

## 目录结构

```
rag-ingest-pipeline/
├── app/
│   ├── main.py                      # FastAPI 入口；lifespan 起停两个后台任务 + 释放 Redis/HTTP/engine
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
│   ├── infra/                       # redis_client.py（Redis）/ model_client.py（模型服务的 HTTP 客户端）
│   ├── models/
│   │   ├── document_raw.py          # 原始文档体（留痕，清洗规则改了能重跑）
│   │   ├── document_clean.py        # 标准化后的正文（含 enrich_* 四列与状态机）
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
│   │   ├── enrich_service.py        # AI 接缝的 facade：provider 分派 + 降级链（只做这两件事）
│   │   ├── enrich_providers/        # 适配层：一个 provider 一个文件（厂商差异关在这里）
│   │   │   ├── base.py              #   协议 + ENRICH_VERSION + EMPTY/OFF + 逐项降级
│   │   │   ├── rule.py              #   纯 Python 四项（不出网；也是降级终点）
│   │   │   ├── model.py             #   自定契约的上游（网关形态）
│   │   │   └── openai_compat.py     #   OpenAI 兼容（自建 vLLM/Ollama 与云厂商兼容端点）
│   │   ├── enricher.py              # 后台补 enrich 的循环（认领 → 补 → 写回）
│   │   └── reclaimer.py             # 超时回收循环（chunk 的 processing + 文档的 enrich processing）
│   └── utils/                       # hashing / time / language / summary / keywords / metadata / sample
├── alembic/                         # 迁移（env.py 走异步 engine，DSN 取自 settings）
│   └── versions/
├── syncer/                          # 同步器（模拟飞书/Confluence 批量推文档）
├── embedder/                        # 向量化 worker（认领 chunk → 标记完成）
├── model/                           # 假模型服务（零依赖；enrich 的可插拔上游）
├── tests/
├── docs/
├── docker-compose.yml               # api + mysql + redis + syncer + embedder + model
├── Dockerfile
└── requirements.txt
```

---

## 分层职责

| 层 | 职责 | 不做什么 |
|---|---|---|
| `api/endpoints` | 定义路由、校验入参、返回响应 | 不写业务逻辑、不直接碰 DB |
| `api/deps` | 组装依赖（session、redis、service） | — |
| `services` | 业务编排（标准化→判重→落库→切块、任务分发、缓存、锁、enrich） | 不直接写 SQL；**不依赖 HTTP**（业务异常在 `core/exceptions.py` 统一映射。唯一的 HTTP 是调模型服务，被关在 `enrich_service.py` 一个模块里） |
| `repositories` | 所有 DB 读写、聚合查询、`SKIP LOCKED` 认领 | 不含业务判断 |
| `models` | ORM 表结构 | — |
| `schemas` | 出入参的 Pydantic 模型 | — |
| `core` | 配置、日志、异常 | — |
| `infra` | 外部连接（Redis 客户端、模型服务的 HTTP 客户端） | 不建连接池以外的逻辑；都是单例 + lifespan 释放 |
| `utils` | 无状态的纯工具函数 | 无 IO、无全局状态 |

数据流：`endpoint` → `service` → `repository` → `DB`；缓存/去重/锁走 `infra` 的 Redis；
调模型服务走 `infra` 的 HTTP 客户端（且只发生在**后台循环**里，不在请求路径上）。

---

## 快速开始

> 目标：**5 分钟**从零到看见整条管道跑起来。每一步都写了"你应该看到什么"——
> 对不上就去最后一步「卡住怎么办」里找。

### 0. 前提

- **Docker Desktop 已装好并在运行**（`docker version` 能打出 Server 段就对了）；
- 第一次跑要构建镜像（装依赖 + 预热 tiktoken 词表与 jieba 词典），**会慢几分钟**，之后就快了；
- **不需要在宿主机装 MySQL / Redis / Python** —— 这个项目只在容器里跑。

> **下面每条命令都给了两版：先 Windows（cmd.exe / PowerShell），再 Git Bash / WSL / macOS。**
> **两种 shell 里一模一样的命令只写一条**（`docker compose up -d` 这种没必要抄两遍）；
> 不一样的就并排写出来，不用你自己翻译。查库的包装也是一对：
> **Windows 用 `scripts\mysql.cmd`**（PowerShell 里带 `.\`，如 `.\scripts\mysql.cmd "…"`），
> **Git Bash 用 `bash scripts/mysql.sh`** —— 两个都不用你输口令。
>
> 下面这几处**在 Windows 上完全不能用**，原因就一条：**cmd / PowerShell 没有 bash 那套语法**。
>
> - **`bash scripts/mysql.sh` 会撞 WSL**：cmd / PowerShell 里的 `bash` 命中的是
>   `C:\Windows\System32\bash.exe` —— **WSL 2 的启动器，不是 Git 的 bash**；而 WSL 里通常没有 docker，
>   于是报 `The command 'docker' could not be found in this WSL 2 distro`。→ 用 `scripts\mysql.cmd`。
> - **`curl` 在 PowerShell 里是 `Invoke-WebRequest` 的别名** → 要写 `curl.exe`。
> - **多行命令（行尾 `\` 续行）**、**heredoc（`cat > f <<'JSON'`）** → Windows 上合并成一行；
>   JSON 别敲，仓库里现成一份（`scripts/example-doc.json`），见第 4 步。
> - **`sed -i`**（改 `.env` 那几处）→ 直接用编辑器改那一行。

### 1. 准备配置
```text
# Windows（cmd）
copy .env.example .env
# PowerShell / Git Bash / WSL / macOS
cp .env.example .env      # 按需改数据库口令、库名、业务参数
```

> **`.env` 里没有 host/port** —— 这个项目**只在容器里跑**，所以 MySQL/Redis 的地址一律是 compose 的服务名
> （`mysql:3306` / `redis:6379`），只写在 `docker-compose.yml` 的 `environment:` 里。
> **一份环境只描述一处**：`.env` 管账号、口令与业务参数，compose 管拓扑。

### 2. 启动服务
```bash
docker compose up -d --build
```

它会拉起**六个**服务：

| 服务 | 是什么 |
|---|---|
| `api` | 本项目本体：FastAPI + 整条管道 |
| `mysql` / `redis` | 数据库与缓存（去重快路径、分布式锁） |
| `syncer` | 模拟**上游**：周期性批量推文档 |
| `embedder` | 模拟**下游**：认领 chunk 做向量化 |
| `model` | 假模型服务（enrich 的替身上游，默认那档用不到它） |

**你应该看到**：

```bash
docker compose ps
# api / mysql / redis        → healthy
# syncer / embedder / model  → Up
```

> **表结构是怎么来的：** api 启动时会自动跑 `alembic upgrade head` —— 空库首启就把表建好，
> 以后改 schema 再写一条迁移升上去即可，**不用再重建库、丢数据**。迁移文件在 `alembic/versions/`：
> ```bash
> docker compose exec api alembic revision --autogenerate -m "说明"   # 生成
> docker compose exec api alembic upgrade head                        # 应用
> ```
> （给一个**已有数据**的库首次引入迁移时，要先 `alembic stamp head` 打上版本戳，
> 否则它会去建已经存在的表。）

### 3. 确认接口真的活着

```bash
# 从容器内部打 —— 本项目日常验证都走这条路，不依赖宿主机网络
docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').status)"
# 期望：200
```

再打开浏览器 **http://localhost:8000/docs** —— 能列出接口就说明通了。

> ⚠️ **如果报 `service "api" is not running`**：说明栈根本没起来（重启过电脑、关过 Docker Desktop
> 之后就是这样）。`docker compose up -d` 一下再回来 —— **这个错跟代码无关，别去改代码。**

### 4. 发一篇文档

两条路，挑一条：

**a. 在浏览器里点（最省事）**：打开 http://localhost:8000/docs →
`POST /api/v1/documents` → Try it out → `external_id` 填 `readme-sample-1`、`content` 写几句 → Execute。
**期望 201**；再点 `GET /api/v1/documents` 能看到刚发的那篇。

**b. 用命令行**（**样例 JSON 就在仓库里**：`scripts/example-doc.json`）：
用文件、别把 JSON 内联进命令行 —— 原因见下面的警告。

```bat
:: Windows（cmd / PowerShell）—— 就【一行】，没有 heredoc、没有 \ 续行；PowerShell 里必须写 curl.exe
curl.exe -s -i -X POST http://localhost:8000/api/v1/documents -H "Content-Type: application/json" --data-binary "@scripts/example-doc.json"
:: 期望：最上面一行是 HTTP/1.1 201（-i 把响应头一起打出来，省掉 -w 里那些 % 的转义麻烦）
```

```bash
# Git Bash / WSL / macOS
curl -s -w '\n%{http_code}\n' -X POST http://localhost:8000/api/v1/documents \
  -H 'Content-Type: application/json' --data-binary @scripts/example-doc.json
# 期望：末尾一行是 201
```

> **样例用的是 `external_id=readme-sample-1`**，不是 syncer 每 5 秒在推的 `feishu-doc-001` ——
> 否则你第一次发就是「更新」，返回的会是 200 而不是 201。
> 想发自己的文档：把 `scripts/example-doc.json` 复制一份改内容（**`external_id` 记得也换一个**）。
> 同样内容再发一次 → **200**：那是「重复不是错误」；删掉再发也会是 200（走「复活」，语义上等同更新）——
> 都不是失败，见「核心机制」。

> ⚠️ **body 里带中文时，别把 JSON 内联进命令行参数** —— Windows 的 git bash 会把 argv 转成 GBK、
> PowerShell 会把 `-Body` 的字符串按 ANSI 编码，服务端按 UTF-8 解析失败直接 400（或中文静默变 `?`）。
> **用 `--data-binary @文件`**（上面那样，JSON 完全不经过命令行），或干脆走浏览器。

> 若宿主访问不通（Windows + WSL 的端口转发问题），改用 `docker compose exec` 从容器内部打：

```bash
docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').status)"
```

### 5. 看完整流水线跑起来（本项目最有说服力的一幕）
```bash
docker compose up -d --scale syncer=2 --scale embedder=3

# 同步器在推文档、worker 在认领 chunk（不加过滤也能看，加了就只看这两个）
# Windows（cmd）         ： docker compose logs -f syncer embedder | findstr /R "syncer embedder"
# Windows（PowerShell）  ： docker compose logs -f syncer embedder | Select-String 'syncer|embedder'
# Git Bash / WSL / macOS ： docker compose logs -f syncer embedder | grep -E '\[syncer\]|\[embedder\]'

# 去重生效：推了很多次，文档只有几个（一条查文档、一条查块）
# Windows
scripts\mysql.cmd "select count(*) as documents from document_clean;"
scripts\mysql.cmd "select embedding_status, count(*) from document_chunk group by embedding_status;"
# Git Bash / WSL / macOS
bash scripts/mysql.sh "select count(*) as documents from document_clean;"
bash scripts/mysql.sh "select embedding_status, count(*) from document_chunk group by embedding_status;"
```
期望：**同步器疯狂推、worker 并发认领不重复，`document_clean` 只有几行、chunk 状态从 `pending` 流向 `done`。**

### 6. 看 enrich 四档 —— 以及「上游挂了会怎样」

```bash
# 默认 rule：文档入库只标 pending，后台循环（默认 30 秒一轮）会把它补成 done
# Windows
scripts\mysql.cmd "select enrich_status, count(*) from document_clean group by enrich_status;"
# Git Bash / WSL / macOS
bash scripts/mysql.sh "select enrich_status, count(*) from document_clean group by enrich_status;"

# 切到 OpenAI 兼容档（假上游在 model 容器里，两个端点都讲）
# 用编辑器把 .env 里那行改成 ENRICH_PROVIDER=openai_compat（Windows 下别用 sed），然后重建容器：
docker compose up -d api
# 发一篇文档 → 等后台补 → 摘要里会出现样本标记 [中间省略]，tags 是 2-gram（如 "者周"）——
# 这两样规则侧都产不出来，所以"值是谁给的"一眼可辨

# 铁律现场：把上游停掉，再发一篇 → 依然 201，那几列回落到规则算出来的值
docker compose stop model
docker compose up -d model          # 看完恢复
# 收尾：把 .env 里那行 ENRICH_PROVIDER 删掉（或注释掉）—— 测试容器也读 .env，留着会让测试打到真上游
```

期望：`enrich_status` 从 `pending` 流向 `done`。
**`ENRICH_PROVIDER=off` 时它会（刻意地）停在 `pending`** —— "待办积压"比"写一堆 NULL"更诚实，
而且开关一打开它们还能被补上。

### 7. 切到真厂商 API（以 DeepSeek 为例）

> **先确认栈在跑**：`docker compose ps` 里 api 应该是 `healthy`；不在跑就先 `docker compose up -d`
> —— 否则下面每条命令都只会报 `service "api" is not running`。

`openai_compat` 那一档本来就是照着"云厂商兼容端点"设计的，所以**接真厂商不用改代码，只改配置**：

```bash
# .env（里面只有 key 需要你填；.env 已 gitignore）
ENRICH_PROVIDER=openai_compat          # 先留 rule，等下面 Step 0 通过再切
ENRICH_OPENAI_BASE_URL=https://api.deepseek.com
ENRICH_OPENAI_PATH=/v1/chat/completions
ENRICH_MODEL_NAME=deepseek-flash       # 写【当前有效】的 id：先跑一次 --models 看一眼（旧名会被重定向，见下）
ENRICH_API_KEY=<你自己的 key>
ENRICH_JSON_MODE=true
ENRICH_TIMEOUT_SECONDS=20              # 默认 5 秒对云厂商太紧，而超时会"静默回落到规则"
docker compose up -d api               # 必须重建容器，它才读得到新环境变量
```

**先隔离变量**：别一上来就走管道。先单独验一次厂商 API，确认 key / 网络 / 模型名 / 路径都对
（这两条在 cmd / PowerShell 里**一模一样**，直接粘）：

```bash
docker compose exec -T api python scripts/probe_upstream.py --models   # 先问：这家现在认哪些 id
docker compose exec -T api python scripts/probe_upstream.py            # 再验：真发一次极小请求
```

`--models` 会列出厂商**当前承认的模型 id**（`GET {base}/models`）—— 模型名会随厂商演进，
**旧名常常仍被接受、只是被重定向到当前模型**（实测：`deepseek-chat` 照样 200，
但响应里的 `model` 是 `deepseek-flash`）。所以"名字写错没"以这张列表为准。

不带参数那次会先打出"将要用的配置"（请求地址 / 模型 / JSON 模式 / **key 配没配**），再发一次极小请求：

- 通了 → 打印 `✓ 通了` + **上游实际服务的模型** + 用量 + 内容（**它会和"我们请求的模型"分开打** ——
  两个数不一样是正常的，厂商在重定向）；
- 不通 → **直接打出上游自己的错误**（比如 `HTTP 401` + `Authentication Fails`），不是一段看不懂的 traceback。

通了之后再切 `ENRICH_PROVIDER=openai_compat`，发一篇文档，等后台补完。**怎么确认"值来自模型、不是规则"**：

- **最硬的证据是日志那一行**：`enrich(openai_compat, model=deepseek-flash) input=… output=… tokens`
  —— 真 token 数，**规则档从不打这行**；
- **摘要的形状**：规则侧只会拼"首段 + 尾段"的原文（带 `" … "` 分隔符）；模型给的是一句**转述**，
  通常不带分隔符、也不是原文照抄；
- 别再拿 `tags` 当判据 —— 模型给的词和 jieba 抽的**可能重合**（实测就撞过：都是"进程/线程/操作系统"）。
  （`[中间省略]` 那个样本标记只在模型**照抄样本**时才出现；它爱转述，所以别只指望这个标记。）

**降级是同一套**：把 key 改错 → 再发一篇 → 依然 `201`，几列回落到规则。

> **接真厂商时实测到的三件事**（2026-10-18，DeepSeek）—— 都不是 bug，但不知道就会以为哪里写错了：
>
> 1. **配置里的名字 ≠ 响应里的名字**：配 `deepseek-chat` 能跑，但响应里的 `model` 是 `deepseek-flash`
>    —— 那是**上游实际服务你的模型**（旧名被重定向了）。验收看响应里那个；想写明确就改成列表里的 id。
> 2. **假名字会被 400 拒**（"The supported API model names are …"）⇒ 它**是按名字路由的**，
>    所以 1 那种重定向是厂商行为，不是我们把名字写错了。
> 3. **JSON 模式不保证内容是合法 JSON**：实测遇到过一次 `{"ok":AFFIRMATIVE}`（值没加引号）——
>    **不是每次**（随后 3 次都是合法 JSON），所以别当成必然，只当"上游可能不守约"的证据。
>    管道里解析失败会**静默回落到规则**、文档照样入库 —— 这正是适配层不假设"上游守约"的原因。
>    （另外那次探活还看到 `reasoning_tokens`：带思考的模型会先把 token 花在思考上，
>    32 个 token 的探活预算可能被吃光、内容为空 —— 探针会自己说明，不是故障。）

> 换别家要动什么？多数只改 `ENRICH_OPENAI_BASE_URL` + `ENRICH_MODEL_NAME`（通义是 `…/compatible-mode`、
> Kimi 是 `https://api.moonshot.cn`）；**智谱 GLM 的路径不同**，所以 `ENRICH_OPENAI_PATH` 才是可配的。
> 细节与两个反直觉的坑见 [`docs/RAG数据接入与预处理管道项目疑问.md`](docs/RAG数据接入与预处理管道项目疑问.md) 的 **Q3 · 3.7**。

### 8. 跑测试

```bash
docker compose --profile test run --rm test
# 限定范围：docker compose --profile test run --rm test pytest -q tests/test_bulk_ingest.py
```

测试跑在**独立库** `rag_ingest_test` + redis 的 db 15 上，**不碰生产数据**。
用真 MySQL/Redis 而不是 SQLite/fakeredis —— 因为 `SKIP LOCKED` 与 Lua 释放锁是 MySQL/Redis 专有的，
被测的正是这两处。

> `tests/` 是**挂载**进测试容器的，改完测试直接重跑即可，不必 `--build`。
> 生产镜像里没有 pytest（那是 `test` stage 才装的）—— 校验方式：`docker compose run --rm --no-deps api python -c "import pytest"` 应当报 `ModuleNotFoundError`。

### 9. 卡住怎么办

| 症状 | 先查这个 |
|---|---|
| `service "xxx" is not running` | 栈没起来 → `docker compose up -d`（重启电脑 / 关过 Docker Desktop 之后最常见） |
| `port is already allocated`，8000 起不来 | 端口被占 → `netstat -ano \| findstr 8000` 找出占用者，或改 `docker-compose.yml` 的端口映射 |
| api 一直 `unhealthy` / 反复重启 | `docker compose logs api --tail 50` —— 它自己会说原因（库连不上？迁移失败？） |
| 浏览器打不开 `localhost:8000` | 见文末那条 Windows/WSL 说明；但**容器内部永远是通的**（第 3 步那条 `docker compose exec` 就是证据） |
| 文档发了，但语言/摘要/关键词还是空的 | enrich 是**后台补**的：等 30 秒（一轮默认 30 秒），或 `docker compose logs api \| grep enrich` 看它在干嘛 |
| 上游探活失败 | 脚本会打出上游的原始错误（401/400/超时），照着它说的改 `.env` |
| 命令行查出来中文是 `????` | **不是数据坏了**：是 `mysql` **客户端**的会话字符集不是 utf8mb4（它按容器 locale 选）。compose 里已设 `LANG=C.UTF-8` 修掉了；哪天又遇到，临时加 `--default-character-set=utf8mb4` 即可。DBeaver 自己设了 utf8mb4，所以它一直正常 |
| 命令粘进 **cmd.exe** 没反应、一行结果都不出 | 见第 0 步那两条：① **别粘多行** —— cmd 的提示符按行执行，折成两行的命令会被截断，前半截先跑掉了；② `2>/dev/null` 是 bash 写法，cmd 里要 `2>NUL`（或不写）。查库用 `scripts\mysql.cmd "select …;"` |
| `The command 'docker' could not be found in this WSL 2 distro` | 你在 cmd / PowerShell 里跑了 `bash scripts/mysql.sh` —— 那边的 `bash` 是 **WSL 2 的启动器**（`C:\Windows\System32\bash.exe`），WSL 里通常没有 docker。换 `scripts\mysql.cmd "…"`（见第 0 步） |
| 窗口里中文结果显示成乱码 | 这次是**控制台代码页**不是 UTF-8（**数据没问题**：库、客户端、连接都是 utf8mb4）→ 先 `chcp 65001`。**别和上面那个 `????` 搞混**：那个是会话字符集（取出来的值就错了），这个只是终端**显示**不对 |

### 端口说明

容器内 MySQL/Redis 是 3306/6379。映射到宿主机的 **3307/6380 只给宿主机上的 GUI 工具用**
（RedisInsight、DataGrip 之类，或临时 `mysql -h127.0.0.1 -P3307`），
**应用自己永远走服务名**（`mysql:3306` / `redis:6379`）—— 这个项目只在容器里跑。
所以日常验证也一律走容器内部（见第 3 步那条 `docker compose exec`）。

---

## 开发路线图

改造分 9 段，每段独立可验证、可叫停（详见 [`docs/改造路线.md`](docs/改造路线.md)）：

- [x] **A** 全量换皮改名：article 域 → document 域（表 / 模型 / 服务 / 接口 / Redis 键）
- [x] **B** 切块：新增 `document_chunk` 表 + 切块服务（段落优先 + overlap），管道接上切块
- [x] **C** 异步任务分发：`/chunks/claim` + `/complete`（`SKIP LOCKED`），worker 可并发认领
- [x] **D** 批量摄入 + 同步器 + worker 容器（`syncer` / `embedder`，可 `--scale`）
- [x] **E** 叙事重写（README + 架构说明）+ 补齐自动化测试
- [x] **F** 文档身份与版本：`document_source` 表 + 版本化（`superseded_at`）+ 告诉下游该撤回哪些
- [x] **数据库大改** 内容去重改成「active 内容索引」+ 引入 Alembic（改 schema 不再重建库丢数据）
- [x] **G** enrich 从空实现变成实的：语言 / 摘要 / 关键词 / 署名 + `enrich_version`
- [x] **H** AI 可插拔：`ENRICH_PROVIDER = rule / model / off` + 后台补 + 假模型服务 + 降级链
- [x] **H2** 适配层：provider 包（一个 provider 一个文件）+ **OpenAI 兼容**档 + 只发样本 + `max_tokens`

---

## 排错提醒

- 常见症状与首查项，见上面 **「9. 卡住怎么办」** 的表格。
- **Windows 特有的一个坑**：`localhost` 会优先解析到 `::1`，而 Docker 的端口转发若被 WSL 中继占住
  （`wslrelay.exe`），**宿主机会连不上容器** —— 但容器自己是好的（`docker compose exec` 里自测 200）。
  遇到时重启 Docker Desktop；或者干脆用容器内部验证（本项目的默认做法）。
