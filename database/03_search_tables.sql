-- =============================================================================
-- 归心 L9 B端搜索检索表
-- 企业需求画像、搜索会话、检索日志
-- =============================================================================

-- =============================================================================
-- 1. 企业需求画像表
-- 每次搜索固化为 profile，支持编辑与重放
-- =============================================================================

CREATE TABLE job_requirement_profiles (
    profile_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- 来源
    employer_id UUID,                                -- 可为空（匿名搜索）
    job_id UUID,

    -- 原始查询
    query_text TEXT NOT NULL,
    query_text_clean TEXT,                           -- 清洗后的查询

    -- 解析结果（Query Parser Agent 输出）
    parsed_result JSONB,
    -- parsed_result 结构:
    -- {
    --   "extracted_tags": ["高并发", "Redis", "死锁"],
    --   "target_atoms": [42, 145, 301],
    --   "atom_weights": {"42": 0.4, "145": 0.35, "301": 0.25},
    --   "hard_filters": {"is_remote_ok": true, "min_level": "L7"}
    -- }

    -- 三层目标向量
    target_vec_32 VECTOR(32),
    target_vec_128 VECTOR(128),
    target_vec_1024 VECTOR(1024),

    -- 能力偏好
    must_have_atoms INTEGER[],                       -- 必须有的能力ID
    nice_to_have_atoms INTEGER[],                    -- 加分项
    ability_weights JSONB,                           -- 各能力权重

    -- 硬过滤条件
    filters JSONB DEFAULT '{}',
    -- filters 结构:
    -- {
    --   "cities": ["北京", "上海"],
    --   "is_remote_ok": true,
    --   "min_experience": 3,
    --   "max_salary": 50000
    -- }

    -- 元数据
    created_by UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    is_template BOOLEAN DEFAULT FALSE                -- 是否保存为模板
);

CREATE INDEX idx_profiles_employer
ON job_requirement_profiles(employer_id, created_at DESC);

-- =============================================================================
-- 2. 搜索会话表
-- 记录每次搜索的完整上下文
-- =============================================================================

CREATE TABLE search_sessions (
    session_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id UUID NOT NULL REFERENCES job_requirement_profiles(profile_id),

    -- 召回策略配置
    recall_config JSONB DEFAULT '{}',
    -- recall_config 结构:
    -- {
    --   "l1_enabled": true, "l1_limit": 200,
    --   "l2_enabled": true, "l2_limit": 80,
    --   "l3_enabled": true, "l3_limit": 40,
    --   "sparse_enabled": true, "sparse_limit": 100
    -- }

    -- RRF 配置
    rrf_k INTEGER DEFAULT 60,
    rrf_weights JSONB DEFAULT '{"dense": 1.0, "sparse": 1.0}',

    -- 重排配置
    rerank_model VARCHAR(50) DEFAULT 'bge-reranker-large',
    rerank_top_k INTEGER DEFAULT 50,
    final_cut INTEGER DEFAULT 10,

    -- 执行追踪
    execution_trace JSONB DEFAULT '{}',
    -- execution_trace 结构:
    -- {
    --   "l1_recall_count": 200,
    --   "l2_recall_count": 80,
    --   "l3_recall_count": 40,
    --   "rrf_fusion_count": 50,
    --   "rerank_count": 10,
    --   "execution_time_ms": 850
    -- }

    created_by UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_sessions_profile
ON search_sessions(profile_id, created_at DESC);

-- =============================================================================
-- 3. 搜索候选结果表
-- 记录每个候选人在搜索中的得分详情
-- =============================================================================

CREATE TABLE search_candidate_scores (
    session_id UUID NOT NULL REFERENCES search_sessions(session_id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates_v2(candidate_id),

    -- 分层召回得分
    score_l1 NUMERIC(6,5),                           -- 32维粗召回得分
    score_l2 NUMERIC(6,5),                           -- 128维中召回得分
    score_l3 NUMERIC(6,5),                           -- 1024维精召回得分
    score_sparse NUMERIC(6,5),                       -- 稀疏召回得分

    -- RRF 融合得分
    rrf_rank_sparse INTEGER,
    rrf_rank_dense INTEGER,
    score_rrf NUMERIC(8,6),

    -- 重排得分
    score_rerank NUMERIC(6,5),

    -- 最终得分
    final_score NUMERIC(6,5),
    final_rank INTEGER,

    -- 解释性数据
    explanations JSONB DEFAULT '{}',
    -- explanations 结构:
    -- {
    --   "matched_atoms": [42, 145],
    --   "atom_score_breakdown": {"42": 0.85, "145": 0.72},
    --   "rerank_reason": "候选人在Redis死锁场景表现优异"
    -- }

    -- 时间衰减调整
    time_decay_factor NUMERIC(6,5) DEFAULT 1.0,
    decay_adjusted_score NUMERIC(6,5),

    PRIMARY KEY (session_id, candidate_id)
);

CREATE INDEX idx_scs_session_rank
ON search_candidate_scores(session_id, final_rank)
WHERE final_rank <= 50;

CREATE INDEX idx_scs_candidate
ON search_candidate_scores(candidate_id, created_at);

-- =============================================================================
-- 4. 搜索日志表（审计与优化）
-- =============================================================================

CREATE TABLE search_logs (
    log_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID REFERENCES search_sessions(session_id),

    -- 请求信息
    query_text TEXT,
    filters_applied JSONB,

    -- 性能指标
    total_time_ms INTEGER,
    l1_time_ms INTEGER,
    l2_time_ms INTEGER,
    l3_time_ms INTEGER,
    rrf_time_ms INTEGER,
    rerank_time_ms INTEGER,

    -- 结果统计
    results_returned INTEGER,
    results_from_cache BOOLEAN DEFAULT FALSE,

    -- 用户反馈（可选）
    user_feedback JSONB,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_search_logs_created
ON search_logs(created_at);

CREATE INDEX idx_search_logs_session
ON search_logs(session_id);
