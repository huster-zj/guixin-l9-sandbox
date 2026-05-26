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

## 目录结构

```
guixin-l9-sandbox/
├── README.md                 # 本文件：全局架构说明
├── prompts/                  # 【Task 1】核心 Agent System Prompt
│   ├── ingestion_agent.md    # 数据解析 Agent：简历 → DNA
│   ├── battlefield_agent.md  # 场景生成 Agent：DNA → 私有框架/残卷
│   ├── x_rag_agent.md        # 动态追问 Agent：代码 Diff → 异常注入
│   └── oracle_judge.md       # 确权决策 Agent：战役日志 → 1024维向量
├── workflow/                 # 【Task 2】工作流编排与数据标准
│   ├── state_machine.py      # Agent 状态机流转与熔断机制
│   ├── harness_engineering.md# Harness Engineering 设计说明
│   └── report_schema.json    # 极客认证报告 JSON Schema
├── database/                 # 【Task 3】存量底座重构
│   ├── 01_core_tables.sql    # 核心表：ability_library, role_schemas, candidates_v2
│   ├── 02_assessment_tables.sql # 评估链路表
│   ├── 03_search_tables.sql  # 检索链路表
│   ├── 04_indexes.sql        # HNSW/GIN 索引优化
│   ├── migration_plan.md     # 平滑迁移策略
│   └── schema_review.md      # 旧架构缺陷分析
└── api/                      # 【Task 4】混合检索管线
    ├── requirements.txt      # FastAPI + pgvector + pydantic
    ├── main.py               # FastAPI 入口
    ├── config.py             # 配置管理
    ├── models.py             # Pydantic 数据模型
    ├── search_pipeline.py    # L1/L2/L3 分层召回 + RRF 主逻辑
    ├── recall/               # 分层召回实现
    │   ├── l1_coarse.py      # 32维粗召回
    │   ├── l2_medium.py      # 128维中召回
    │   └── l3_fine.py        # 1024维精召回 + 稀疏标签
    ├── fusion/               # 融合层
    │   └── rrf_fusion.py     # 倒数秩融合实现
    ├── rerank/               # 重排层
    │   └── cross_encoder.py  # Cross-Encoder 重排 (Mock)
    └── mocks/                # Mock RPC 层
        ├── db_client.py      # 模拟数据库调用
        └── llm_client.py     # 模拟 LLM 调用
```

---

## 快速启动

```bash
# 1. 安装依赖
cd api && pip install -r requirements.txt

# 2. 启动 Mock 服务
uvicorn main:app --reload

# 3. 测试搜索管线
curl -X POST "http://localhost:8000/search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "需要一个能在高并发下处理 Redis 分布式死锁的后端，不限语言",
    "filters": {"is_remote_ok": true}
  }'
```

---

## 关键设计决策

### 1. 三层向量架构

| 层级 | 维度 | 用途 | 召回量 |
|------|------|------|--------|
| L1 | 32 | 粗召回，方向对齐 | Top 200 |
| L2 | 128 | 中召回，能力族匹配 | Top 80 |
| L3 | 1024 | 精召回，原子能力精准匹配 | Top 40 |

### 2. RRF 融合公式

```python
k = 60
rrf_score(d) = 1/(k + rank_sparse(d)) + 1/(k + rank_dense(d))
```

### 3. 时间衰减公式

```python
final_score = raw_score * exp(-λ * months_since_last_certified)
# λ = 0.05 (约5%每月衰减)
```

---

## 验收清单

- [ ] Agent Prompt 具备强 JSON Schema 约束
- [ ] 状态机具备异常熔断与降级机制
- [ ] DDL 包含 HNSW 索引与 GIN 索引优化
- [ ] 搜索管线实现 L1/L2/L3 分层召回
- [ ] RRF 算法手写实现，非库调用
- [ ] 全程 Mock 可运行演示
