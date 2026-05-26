-- =============================================================================
-- 归心 L9 核心表结构重构 v2.0
-- 基于 PostgreSQL 15+ + pgvector 插件
-- 设计原则：1024原子能力锁死、三层向量资产、事件账本模型
-- =============================================================================

-- 启用必要扩展
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================================
-- 1. 原子能力库 (The 1024 Atoms)
-- 绝对静态，由迁移脚本维护，禁止运行时修改
-- =============================================================================

CREATE TABLE ability_library (
    atom_id SMALLINT PRIMARY KEY CHECK (atom_id BETWEEN 1 AND 1024),
    atom_code VARCHAR(32) UNIQUE NOT NULL,           -- 如: GO_CONC_001
    atom_name VARCHAR(100) NOT NULL,                 -- 如: Go并发编程
    domain VARCHAR(50) NOT NULL,                     -- 如: backend, ai, product
    category VARCHAR(50) NOT NULL,                   -- 如: language, framework, soft_skill
    description TEXT,

    -- 层级映射（用于32/128维向量汇聚）
    clan_id SMALLINT,                                -- 所属能力族（1-128）
    macro_id SMALLINT,                               -- 所属宏观能力（1-32）

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE ability_library IS '1024原子能力库，绝对静态';

-- =============================================================================
-- 2. 能力层级映射关系
-- 显式描述 1024->128->32 的汇聚关系
-- =============================================================================

CREATE TABLE ability_clans (
    clan_id SMALLINT PRIMARY KEY CHECK (clan_id BETWEEN 1 AND 128),
    clan_name VARCHAR(100) NOT NULL,
    clan_description TEXT,
    macro_id SMALLINT NOT NULL CHECK (macro_id BETWEEN 1 AND 32),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE ability_macros (
    macro_id SMALLINT PRIMARY KEY CHECK (macro_id BETWEEN 1 AND 32),
    macro_name VARCHAR(100) NOT NULL,
    macro_description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 汇聚权重表（支持跨族映射）
CREATE TABLE ability_rollup_weights (
    source_atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),
    target_clan_id SMALLINT NOT NULL REFERENCES ability_clans(clan_id),
    weight NUMERIC(6,5) NOT NULL CHECK (weight BETWEEN 0 AND 1),
    rollup_mode VARCHAR(20) DEFAULT 'weighted_mean', -- weighted_mean, max, min

    PRIMARY KEY (source_atom_id, target_clan_id)
);

-- =============================================================================
-- 3. 岗位能力映射视图 (Role Schemas)
-- 预设各核心岗位的考核维度
-- =============================================================================

CREATE TABLE role_schemas (
    schema_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    role_code VARCHAR(50) UNIQUE NOT NULL,           -- 如: ai_backend_l8
    role_name VARCHAR(100) NOT NULL,                 -- 如: AI后端工程师(L8)
    role_level VARCHAR(10) NOT NULL CHECK (role_level IN ('L6', 'L7', 'L8', 'L9')),

    -- 核心考核维度（最多6个）
    target_atoms INTEGER[] NOT NULL,                 -- 如: {42, 145, 203, 301, 405, 512}
    atom_weights NUMERIC(6,5)[],                     -- 各维度权重，和=1

    description TEXT,
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'deprecated')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE role_schemas IS '岗位能力映射，定义各角色的核心考核维度';

-- 校验：target_atoms 必须在 1-1024 范围内
CREATE OR REPLACE FUNCTION check_role_atoms()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM UNNEST(NEW.target_atoms) AS atom
        WHERE atom < 1 OR atom > 1024
    ) THEN
        RAISE EXCEPTION 'target_atoms must be within 1-1024 range';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_role_atoms
BEFORE INSERT OR UPDATE ON role_schemas
FOR EACH ROW EXECUTE FUNCTION check_role_atoms();

-- =============================================================================
-- 4. 候选人主表 v2 (重构版)
-- 废弃旧 skill_vector，引入三层向量资产
-- =============================================================================

CREATE TABLE candidates_v2 (
    candidate_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL UNIQUE,                    -- 关联 auth.users

    -- 基础信息（脱敏）
    real_name_hash VARCHAR(64),                      -- 姓名哈希，非明文
    contact_hash VARCHAR(64),                        -- 联系方式哈希

    -- 偏好设置
    preferred_city VARCHAR(100),
    is_remote_ok BOOLEAN DEFAULT FALSE,
    work_mode VARCHAR(20) DEFAULT 'both' CHECK (work_mode IN ('onsite', 'remote', 'both')),
    salary_expectations JSONB DEFAULT '{}',
    available_date VARCHAR(20),

    -- 三层能力向量资产（核心）
    vec_32 VECTOR(32),                               -- 宏观能力向量
    vec_128 VECTOR(128),                             -- 能力族向量
    vec_1024 VECTOR(1024),                           -- 原子能力向量

    -- 辅助检索数据（JSONB）
    profile_data JSONB NOT NULL DEFAULT '{}',
    -- profile_data 结构:
    -- {
    --   "verified_skills": ["Golang", "Redis"],
    --   "education_level": "bachelor",
    --   "years_of_experience": 5,
    --   "reranker_payload": "...",
    --   "combat_highlights": [...]
    -- }

    -- 认证状态
    last_certified_at TIMESTAMPTZ,                   -- 最后一次认证时间
    certification_status VARCHAR(20) DEFAULT 'none'
        CHECK (certification_status IN ('none', 'pending', 'certified', 'expired')),
    certification_level VARCHAR(10),

    -- 向量化版本（用于追踪算法迭代）
    vector_version VARCHAR(20) DEFAULT '1.0',

    -- 可见性控制
    is_visible BOOLEAN DEFAULT TRUE,
    is_private_pool BOOLEAN DEFAULT FALSE,
    source_enterprise_id UUID,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE candidates_v2 IS '候选人主表v2，支持三层向量资产';

-- 创建复合索引
CREATE INDEX idx_candidates_v2_certified
ON candidates_v2(last_certified_at)
WHERE certification_status = 'certified';

CREATE INDEX idx_candidates_v2_visible
ON candidates_v2(is_visible, is_private_pool)
WHERE is_visible = TRUE;

CREATE INDEX idx_candidates_v2_city
ON candidates_v2(preferred_city);

-- GIN 索引用于 JSONB 查询
CREATE INDEX idx_candidates_v2_profile_gin
ON candidates_v2 USING GIN (profile_data);

-- 专门用于 BM25 的 GIN 索引（verified_skills 字段）
CREATE INDEX idx_candidates_v2_skills_gin
ON candidates_v2 USING GIN ((profile_data->'verified_skills'));

-- =============================================================================
-- 5. 能力快照表（用于重做机制）
-- 每次认证产生一条记录，支持版本回溯
-- =============================================================================

CREATE TABLE candidate_ability_snapshots (
    snapshot_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    candidate_id UUID NOT NULL REFERENCES candidates_v2(candidate_id) ON DELETE CASCADE,
    assessment_id UUID NOT NULL,

    -- 快照向量
    vec_32 VECTOR(32),
    vec_128 VECTOR(128),
    vec_1024 VECTOR(1024),

    -- 激活的能力（稀疏存储，只存非基线值）
    activated_atoms JSONB,  -- { "42": 0.85, "145": 0.72, ... }

    -- 聚合模式
    aggregation_mode VARCHAR(20) DEFAULT 'mean_of_assessments',

    -- 版本控制
    snapshot_version INTEGER NOT NULL DEFAULT 1,
    is_active BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(candidate_id, snapshot_version)
);

COMMENT ON TABLE candidate_ability_snapshots IS '候选人能力快照，支持重做与版本回溯';

CREATE INDEX idx_snapshots_candidate
ON candidate_ability_snapshots(candidate_id, is_active)
WHERE is_active = TRUE;

-- =============================================================================
-- 6. 更新触发器
-- =============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_candidates_v2_updated_at
BEFORE UPDATE ON candidates_v2
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trg_role_schemas_updated_at
BEFORE UPDATE ON role_schemas
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
