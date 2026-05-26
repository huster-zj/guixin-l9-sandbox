# 旧架构缺陷分析与重构说明

## 一、51张旧表核心问题分析

### 1. 能力建模层面

**问题：动态字符串标签作为能力标准**

```sql
-- 旧表结构
sub_skills (id, name, definition, embedding, vector_index)
candidate_current_skills (candidate_id, sub_skill_id, level, current_level)
```

**缺陷：**
- `sub_skills.name` 是动态字符串，不同面试官录入"Go并发"和"Golang并发"被视为不同技能
- 没有统一的能力度量衡，无法跨岗位比较
- 向量索引 `vector_index` 与实际原子能力没有严格映射

**重构方案：**
- 引入 `ability_library` 表，锁死 1024 个原子能力（ID: 0001-1024）
- 所有能力评分必须映射到这 1024 个固定 ID
- 新增三层向量资产（32/128/1024）支持分层召回

### 2. 向量检索层面

**问题：单维向量无法支撑分层召回**

```sql
-- 旧表结构
candidates (skill_vector extensions.vector, assessment_embedding extensions.vector)
```

**缺陷：**
- 只有单维向量，无法实现 "粗召回→中召回→精召回" 的分层策略
- 高维向量直接全库搜索会导致维度诅咒
- 没有稀疏稠密混合检索能力

**重构方案：**
- `candidates_v2` 表引入 `vec_32`, `vec_128`, `vec_1024` 三层向量
- 每层向量独立构建 HNSW 索引
- 引入 `profile_data JSONB` 支持 BM25 稀疏召回

### 3. 数据追溯层面

**问题：评分结果不可追溯、不可重做**

```sql
-- 旧表结构
assessments (role_rating, subskill_ratings JSONB, dimension_scores JSONB)
candidate_subskill_history (candidate_id, sub_skill_id, score, assessed_at)
```

**缺陷：**
- `subskill_ratings` 是覆盖写，重做后旧数据丢失
- 无法回答"为什么这个候选人的Go评分是0.72"
- 同一候选人多次考试，分数如何聚合没有明确定义

**重构方案：**
- 引入事件账本模型：`question_ability_scores` 记录每道题对每个能力的贡献
- 引入 `candidate_ability_snapshots` 支持版本回溯
- 引入 `assessment_ability_aggregates` 明确单场测试内的聚合逻辑

### 4. 搜索层面

**问题：缺乏工业级搜索管线设计**

```sql
-- 旧表结构
employer_unlock_history (employer_id, candidate_id, ...)
```

**缺陷：**
- 没有需求画像持久化，无法重放搜索
- 没有分层召回管线，直接在高维向量上暴力搜索
- 缺乏 RRF 融合、Cross-Encoder 重排等现代检索技术

**重构方案：**
- 新增 `job_requirement_profiles` 固化每次搜索的需求
- 新增 `search_sessions` 记录完整搜索上下文
- 新增 `search_candidate_scores` 记录分层得分详情

### 5. 索引优化层面

**问题：索引设计未考虑高并发场景**

**缺陷：**
- HNSW 索引参数未针对召回场景调优
- 缺乏部分索引（Partial Index）减少索引大小
- JSONB 查询缺乏 GIN 索引支持

**重构方案：**
- HNSW 索引配置: `m=16, ef_construction=64`
- 添加部分索引只索引活跃候选人
- 为 `profile_data->'verified_skills'` 建立专门 GIN 索引

---

## 二、系统性缺陷总结

| 缺陷类别 | 旧架构问题 | 重构方案 |
|---------|-----------|---------|
| 能力标准 | 动态字符串标签 | 1024原子能力锁死 |
| 向量资产 | 单维向量 | 32/128/1024三层向量 |
| 数据追溯 | 覆盖写无版本 | 事件账本模型 |
| 搜索管线 | 暴力搜索 | 分层召回+RRF+重排 |
| 索引优化 | 通用配置 | 场景化调优索引 |
| 召回策略 | 仅稠密向量 | 稠密+稀疏混合 |

---

## 三、平滑迁移策略

### Phase 1: 双轨并行（Week 1-2）

1. 创建新表（`candidates_v2`, `assessments_v2` 等）
2. 旧表继续写入，新表同步写入
3. 数据回填脚本将历史数据迁移到新表

### Phase 2: 灰度切换（Week 3-4）

1. 搜索功能灰度：5%流量走新搜索管线
2. 对比旧新搜索结果的质量指标
3. 逐步扩大灰度比例

### Phase 3: 完全切换（Week 5）

1. 100% 流量走新搜索管线
2. 旧表降级为只读
3. 保留6个月后归档删除

---

## 四、关键 SQL 优化说明

### HNSW 索引参数选择

```sql
-- m=16: 每个节点16个连接，平衡内存与召回率
-- ef_construction=64: 构建时搜索范围，越大越精确但构建越慢
CREATE INDEX idx_candidates_v2_vec1024_hnsw
ON candidates_v2 USING hnsw (vec_1024 vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

### 部分索引减少索引大小

```sql
-- 只索引认证状态为 certified 且可见的候选人
-- 预计减少 30-40% 索引大小
CREATE INDEX idx_candidates_v2_searchable
ON candidates_v2(is_visible, certification_status, last_certified_at)
WHERE is_visible = TRUE AND certification_status = 'certified';
```

### 时间衰减函数

```sql
-- 在查询时动态计算时间衰减后的分数
-- decay = e^(-0.05 * months)
CREATE OR REPLACE FUNCTION search_candidates_with_decay(...)
RETURNS TABLE (...) AS $$
    SELECT
        c.candidate_id,
        (1 - (c.vec_1024 <=> query_vec)) * 
            EXP(-0.05 * EXTRACT(MONTH FROM AGE(NOW(), c.last_certified_at)))
        as adjusted_score
    FROM candidates_v2 c
    ...
$$ LANGUAGE plpgsql;
```
