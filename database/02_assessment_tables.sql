-- =============================================================================
-- 归心 L9 评估链路表
-- 题目实例化、能力评分、账本模型
-- =============================================================================

-- =============================================================================
-- 1. 题目实例表
-- 题库表(question_bank)作为模板，实际使用的题目必须实例化
-- =============================================================================

CREATE TABLE question_instances (
    instance_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    assessment_id UUID NOT NULL,

    -- 来源（可选）
    source_question_bank_id UUID,
    generation_prompt_version VARCHAR(50),  -- 如果是AI生成的题目

    -- 题目内容
    question_type VARCHAR(20) NOT NULL
        CHECK (question_type IN ('implementation', 'debugging', 'design', 'analysis', 'choice')),
    difficulty_level NUMERIC(4,2) CHECK (difficulty_level BETWEEN 1 AND 10),

    -- 题目负载（JSONB存储完整题目）
    question_payload JSONB NOT NULL,
    -- question_payload 结构:
    -- {
    --   "title": "实现服务注册中心",
    --   "description": "...",
    --   "code_skeleton": { "main.go": "..." },
    --   "acceptance_criteria": [...],
    --   "hidden_traps": [...],
    --   "estimated_time_minutes": 20
    -- }

    -- 状态
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'completed', 'abandoned')),

    round_no INTEGER NOT NULL DEFAULT 1,  -- 第几轮题目
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_question_instances_assessment
ON question_instances(assessment_id, status);

-- =============================================================================
-- 2. 题目能力绑定表
-- 一题多能力命中
-- =============================================================================

CREATE TABLE question_ability_bindings (
    binding_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    question_instance_id UUID NOT NULL REFERENCES question_instances(instance_id) ON DELETE CASCADE,
    atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),

    weight NUMERIC(6,5) NOT NULL CHECK (weight BETWEEN 0 AND 1),
    binding_source VARCHAR(20) DEFAULT 'llm_generated'
        CHECK (binding_source IN ('llm_generated', 'rule_mapped', 'human_reviewed')),
    confidence NUMERIC(6,5) DEFAULT 0.8,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(question_instance_id, atom_id)
);

CREATE INDEX idx_qa_bindings_question
ON question_ability_bindings(question_instance_id);

CREATE INDEX idx_qa_bindings_atom
ON question_ability_bindings(atom_id);

-- 校验：同一题目的权重和应接近1（允许5%误差）
CREATE OR REPLACE FUNCTION check_binding_weights()
RETURNS TRIGGER AS $$
DECLARE
    total_weight NUMERIC;
BEGIN
    SELECT COALESCE(SUM(weight), 0) INTO total_weight
    FROM question_ability_bindings
    WHERE question_instance_id = NEW.question_instance_id;

    IF total_weight > 1.05 THEN
        RAISE EXCEPTION 'Total weight for question exceeds 1.05: %', total_weight;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_binding_weights
AFTER INSERT OR UPDATE ON question_ability_bindings
FOR EACH ROW EXECUTE FUNCTION check_binding_weights();

-- =============================================================================
-- 3. 题目能力评分表（原始账本）
-- 这是最核心的原始数据，支持任意粒度的追溯
-- =============================================================================

CREATE TABLE question_ability_scores (
    score_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    question_instance_id UUID NOT NULL REFERENCES question_instances(instance_id),
    assessment_id UUID NOT NULL,
    candidate_id UUID NOT NULL REFERENCES candidates_v2(candidate_id),
    atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),

    -- 评分数据
    raw_score NUMERIC(5,2) CHECK (raw_score BETWEEN 0 AND 100),      -- 原始百分制
    normalized_score NUMERIC(6,5) CHECK (normalized_score BETWEEN 0 AND 1),  -- 归一化
    weight NUMERIC(6,5) DEFAULT 1.0,                                 -- 本题对该能力的权重
    contribution_score NUMERIC(8,5),                                 -- 加权贡献 = normalized * weight

    -- 评分来源
    score_source VARCHAR(20) DEFAULT 'ai_grader'
        CHECK (score_source IN ('ai_grader', 'objective_rule', 'human_override')),
    grader_version VARCHAR(20),

    -- 证据
    evidence JSONB DEFAULT '{}',
    -- evidence 结构:
    -- {
    --   "code_quality_score": 0.85,
    --   "test_pass_rate": 0.9,
    --   "xrag_performance": [...],
    --   "rationale": "..."
    -- }

    scored_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_qas_assessment
ON question_ability_scores(assessment_id);

CREATE INDEX idx_qas_candidate_atom
ON question_ability_scores(candidate_id, atom_id);

CREATE INDEX idx_qas_scored_at
ON question_ability_scores(scored_at);

-- =============================================================================
-- 4. 单场测试能力聚合表
-- 同一 assessment 内，同一能力的加权聚合
-- =============================================================================

CREATE TABLE assessment_ability_aggregates (
    aggregate_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    assessment_id UUID NOT NULL,
    candidate_id UUID NOT NULL REFERENCES candidates_v2(candidate_id),
    atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),

    layer SMALLINT NOT NULL CHECK (layer IN (32, 128, 1024)),
    aggregation_mode VARCHAR(20) DEFAULT 'weighted_mean',

    -- 聚合结果
    score NUMERIC(6,5) CHECK (score BETWEEN 0 AND 1),

    -- 支持统计
    support_count INTEGER DEFAULT 0,          -- 支持该分数的题目数
    support_weight NUMERIC(8,5) DEFAULT 0,    -- 总权重
    source_question_count INTEGER DEFAULT 0,

    -- 重算追踪
    computed_at TIMESTAMPTZ DEFAULT NOW(),
    recomputed_at TIMESTAMPTZ,

    UNIQUE(assessment_id, atom_id)
);

CREATE INDEX idx_aaa_assessment
ON assessment_ability_aggregates(assessment_id);

CREATE INDEX idx_aaa_candidate
ON assessment_ability_aggregates(candidate_id, atom_id);

-- =============================================================================
-- 5. 评估主表（精简版）
-- 继承旧表概念，但精简字段，复杂数据存入JSONB
-- =============================================================================

CREATE TABLE assessments_v2 (
    assessment_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    candidate_id UUID NOT NULL REFERENCES candidates_v2(candidate_id),
    schema_id UUID NOT NULL REFERENCES role_schemas(schema_id),

    -- 状态机状态
    status VARCHAR(30) DEFAULT 'created'
        CHECK (status IN (
            'created',
            'ingestion_processing', 'ingestion_completed', 'ingestion_failed',
            'battlefield_provisioning', 'battlefield_ready', 'battlefield_fallback',
            'combat_active', 'combat_paused', 'combat_completed',
            'evaluating', 'evaluation_completed', 'evaluation_failed',
            'certified', 'failed', 'abandoned'
        )),

    -- 时间追踪
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_minutes INTEGER,

    -- 结果摘要
    overall_score NUMERIC(6,5),
    confidence NUMERIC(6,5),

    -- 完整报告（JSONB）
    final_report JSONB,

    -- 反作弊
    trust_score INTEGER CHECK (trust_score BETWEEN 0 AND 100),
    cheat_flags JSONB DEFAULT '[]',

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_assessments_v2_candidate
ON assessments_v2(candidate_id, status);

CREATE INDEX idx_assessments_v2_status
ON assessments_v2(status)
WHERE status IN ('evaluating', 'evaluation_completed', 'certified');

-- 触发器：更新 updated_at
CREATE TRIGGER trg_assessments_v2_updated_at
BEFORE UPDATE ON assessments_v2
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
