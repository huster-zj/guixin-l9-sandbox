# 归心 L9 全链路重构方案

> 全栈 AI 架构师实战压测 - 从静态题库到动态沙盒与混合检索的工业级演进

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              C 端：L9 动态沙盒引擎                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────────┐ │
│  │   Ingestion  │───→│  Battlefield │───→│   X-RAG      │───→│  Oracle   │ │
│  │    Agent     │    │    Agent     │    │    Agent     │    │   Judge   │ │
│  │  (DNA提取)    │    │  (场景生成)   │    │  (动态追问)   │    │ (确权决策) │ │
│  └──────────────┘    └──────────────┘    └──────────────┘    └─────┬─────┘ │
│                                                                     │       │
│                                    ┌────────────────────────────────┘       │
│                                    ↓                                        │
│                         ┌──────────────────────┐                           │
│                         │  1024维能力向量落盘    │                           │
│                         │  + 三层向量资产(32/128)│                           │
│                         └──────────────────────┘                           │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                              B 端：工业级混合检索                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  HR Query ──→ Query Parser ──→ ┌──────────────────────────────────────┐    │
│                                │  双轨并发召回                         │    │
│                                │  ├─ 稀疏路：GIN + BM25 (JSONB)       │    │
│                                │  └─ 稠密路：HNSW + Cosine (pgvector) │    │
│                                └──────────────────┬───────────────────┘    │
│                                                   ↓                         │
│                                        ┌──────────────┐                    │
│                                        │  RRF 融合层   │                    │
│                                        │  k=60, Top50 │                    │
│                                        └──────┬───────┘                    │
│                                               ↓                             │
│                                        ┌──────────────┐                    │
│                                        │ Cross-Encoder│                    │
│                                        │  重排绝杀     │                    │
│                                        └──────┬───────┘                    │
│                                               ↓                             │
│                                         Top 3 候选人                        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 核心纪律

1. **严禁 Elasticsearch**: 底层必须由 PostgreSQL + pgvector 一体化打穿
2. **1024 原子能力锁死**: `ability_library` 表固定 0001-1024 ID，不可动态扩展
3. **向量稀疏性处理**: 未考核能力赋予基线权重 0.05，避免维度诅咒
4. **时间衰减**: B 端召回引入 `e^(-λ·Δt)` 衰减因子，Δt 为月数

---

## 项目结构

```
guixin-l9-sandbox/
├── README.md                          # 本文件：全局架构说明
│
├── prompts/                           # 【Task 1】核心 Agent System Prompt
│   ├── ingestion_agent.md            # 数据解析 Agent：简历 → DNA
│   ├── battlefield_agent.md          # 场景生成 Agent：DNA → 私有框架/残卷
│   ├── x_rag_agent.md                # 动态追问 Agent：代码 Diff → 异常注入
│   └── oracle_judge.md               # 确权决策 Agent：战役日志 → 1024维向量
│
├── workflow/                          # 【Task 2】工作流编排与数据标准
│   ├── state_machine.py              # Agent 状态机流转与熔断机制
│   ├── harness_engineering.md        # Harness Engineering 设计说明
│   └── report_schema.json            # 极客认证报告 JSON Schema
│
├── database/                          # 【Task 3】存量底座重构
│   ├── 01_core_tables.sql            # 核心表：ability_library, role_schemas, candidates_v2
│   ├── 02_assessment_tables.sql      # 评估链路表：题目实例、能力评分账本
│   ├── 03_search_tables.sql          # 检索链路表：需求画像、搜索会话
│   ├── 04_indexes.sql                # HNSW/GIN 索引优化
│   ├── 05_migration.sql              # 平滑迁移脚本
│   └── schema_review.md              # 旧架构缺陷分析
│
└── api/                               # 【Task 4】混合检索管线
    ├── main.py                        # FastAPI 入口
    ├── run.py                         # 启动脚本（解决相对导入）
    ├── search_pipeline.py             # 搜索管线主控制器（L1/L2/L3 + RRF）
    ├── config.py                      # 搜索配置
    ├── models.py                      # Pydantic 数据模型
    ├── requirements.txt               # 依赖清单
    │
    ├── fusion/                        # RRF 融合层
    │   └── rrf_fusion.py              # 手写 RRF 倒数秩融合算法
    │
    ├── rerank/                        # 重排层
    │   └── cross_encoder.py           # Cross-Encoder 重排 (Mock)
    │
    ├── mocks/                         # Mock RPC 层
    │   ├── db_client.py               # 模拟 PostgreSQL/pgvector
    │   └── llm_client.py              # 模拟 LLM (Query Parser + Reranker)
    │
    ├── test_search.py                 # 搜索功能测试脚本
    └── verify_task4.py                # Task 4 验证脚本
```

---

## 快速启动

### 1. 安装依赖

```bash
cd api
pip install -r requirements.txt
```

### 2. 启动服务

```bash
# 方式1: 使用启动脚本
python run.py

# 方式2: 使用 uvicorn
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 3. 访问 Swagger 文档

打开浏览器访问：
```
http://localhost:8000/docs
```

### 4. 测试搜索

**方式1: 使用测试脚本**
```bash
python test_search.py
```

**方式2: 使用 curl (PowerShell)**
```powershell
Invoke-WebRequest -Uri "http://localhost:8000/search" `
    -Method POST `
    -ContentType "application/json" `
    -Body '{"query": "需要一个能在高并发下处理 Redis 分布式死锁的后端"}'
```

**方式3: Swagger UI**
- 访问 `http://localhost:8000/docs`
- 找到 `/search` 接口
- 点击 "Try it out"
- 输入查询语句，点击 Execute

---

## 关键设计决策

### 1. 三层向量架构

| 层级 | 维度 | 用途 | 召回量 |
|------|------|------|--------|
| L1 | 32 | 粗召回，方向对齐 | Top 200 |
| L2 | 128 | 中召回，能力族匹配 | Top 80 |
| L3 | 1024 | 精召回，原子能力精准匹配 | Top 40 |

### 2. 分层召回管线

```
HR Query
    ↓
Query Parser → Target Vector + Tags
    ↓
L1 (32维) → Top 200
    ↓
L2 (128维) + L3 (1024维) + Sparse (BM25) 并行执行
    ↓
RRF 融合 (k=60) → Top 50
    ↓
时间衰减调整 e^(-λ·Δt)
    ↓
Cross-Encoder 重排 → Top 10
    ↓
返回 HR
```

### 3. RRF 融合公式（手写实现）

```python
# fusion/rrf_fusion.py 第 34 行
contribution = rlist.weight / (self.k + rank)

# 完整公式
rrf_score(d) = Σ weight_i / (k + rank_i(d))
# k = 60 (平滑常数)
```

### 4. 时间衰减公式

```python
final_score = raw_score * exp(-λ * months_since_last_certified)
# λ = 0.05 (约5%每月衰减)
# 最大衰减月数 = 24
```

### 5. 熔断器设计

```python
# 三种状态
CLOSED   → 正常状态，允许调用
OPEN     → 熔断状态，拒绝调用
HALF_OPEN → 半开状态，试探性调用

# 触发条件
- 连续失败次数 > failure_threshold (默认5次)
- 自动恢复时间 > recovery_timeout (默认60秒)
```

---

## 核心文件说明

### Task 1: Agent Prompts (`prompts/`)

| 文件 | 功能 | 关键约束 |
|------|------|----------|
| `ingestion_agent.md` | 简历解析 → DNA | 强制 JSON Schema 输出，只能从 1024 原子能力中选取 |
| `battlefield_agent.md` | 生成私有框架/残卷 | 禁止 LeetCode 原题，必须捏造 fictional 框架 |
| `x_rag_agent.md` | 实时异常注入 | Debounce 10秒，Checkpoint 触发，最多3轮追问 |
| `oracle_judge.md` | 1024维向量评分 | 受控评分（只能评目标能力），0.0-1.0 量化 |

### Task 2: 工作流编排 (`workflow/`)

| 文件 | 功能 |
|------|------|
| `state_machine.py` | 4 个 Agent 的状态机 + Circuit Breaker 熔断器 |
| `harness_engineering.md` | Harness Engineering 设计原则与最佳实践 |
| `report_schema.json` | 极客认证报告 JSON Schema（含三层向量、防伪标识） |

### Task 3: 数据库重构 (`database/`)

| 文件 | 内容 |
|------|------|
| `01_core_tables.sql` | ability_library (1024原子能力), role_schemas, candidates_v2 (三层向量) |
| `02_assessment_tables.sql` | 题目实例、能力评分账本、聚合表 |
| `03_search_tables.sql` | 需求画像、搜索会话、候选得分详情 |
| `04_indexes.sql` | HNSW (m=16, ef=64) + GIN 索引优化 |
| `schema_review.md` | 旧架构缺陷分析 + 重构理由 |

### Task 4: 搜索管线 (`api/`)

| 文件 | 功能 |
|------|------|
| `search_pipeline.py` | 主控制器：分层召回 → RRF融合 → 时间衰减 → Cross-Encoder重排 |
| `fusion/rrf_fusion.py` | 手写 RRF 算法，纯内存计算，无外部依赖 |
| `mocks/db_client.py` | Mock PostgreSQL + pgvector，50 条模拟候选数据 |
| `mocks/llm_client.py` | Mock Query Parser + Cross-Encoder |

---

## 性能指标

| 指标 | 目标 | 实际 |
|------|------|------|
| 总延迟 | < 1500ms | ~6ms (Mock环境) |
| L1 召回 | Top 200 | 50 (Mock数据) |
| L2 召回 | Top 80 | 50 |
| L3 召回 | Top 40 | 40 |
| 稀疏召回 | Top 100 | 18 |
| RRF 融合 | Top 50 | 50 |
| 重排输出 | Top 10 | 10 |

---

## 验证清单

- [x] **Task 1**: 4 个 Agent Prompt，强 JSON 约束，高压面试官人设
- [x] **Task 2**: 状态机 + Circuit Breaker + 降级机制
- [x] **Task 3**: DDL 重构，HNSW/GIN 索引，1024 原子能力
- [x] **Task 4**: 
  - [x] L1/L2/L3 分层召回逻辑完整
  - [x] RRF 算法手写实现（非库调用）
  - [x] 全量 Mock 函数（无真实数据库）
  - [x] 搜索管线端到端可运行

---

## 常见问题

### Q: 为什么搜索返回 0 个候选人？
A: 检查 filters 是否过于严格（如 `min_experience: 50`），建议传入空对象 `{}`

### Q: 为什么有编码错误？
A: Windows PowerShell 默认使用 GBK，建议使用 Swagger UI 或在 Python 脚本中测试

### Q: 如何验证 RRF 是手写实现？
A: 运行 `python verify_task4.py`，检查第 34 行代码：`contribution = rlist.weight / (self.k + rank)`

---

## 技术栈

- **Backend**: Python 3.10+, FastAPI, Pydantic v2
- **Vector Search**: pgvector (Mock)
- **Async**: asyncio
- **Testing**: Swagger UI, 自定义测试脚本

---

## License

内部项目，仅供面试考核使用。
