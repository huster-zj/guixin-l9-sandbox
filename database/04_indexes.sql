-- =============================================================================
-- 归心 L9 高性能索引配置
-- HNSW 向量索引 + GIN 倒排索引优化
-- =============================================================================

-- =============================================================================
-- 1. HNSW 向量索引（核心）
-- 用于三层向量的近似最近邻搜索
-- =============================================================================

-- 候选人的 1024 维向量（精召回）
CREATE INDEX idx_candidates_v2_vec1024_hnsw
ON candidates_v2
USING hnsw (vec_1024 vector_cosine_ops)
WITH (
    m = 16,              -- 每个节点的最大连接数（平衡精度与内存）
    ef_construction = 64 -- 构建时的搜索范围（越大越精确但越慢）
);

COMMENT ON INDEX idx_candidates_v2_vec1024_hnsw IS
'1024维精召回HNSW索引，ef_construction=64保证构建质量';

-- 候选人的 128 维向量（中召回）
CREATE INDEX idx_candidates_v2_vec128_hnsw
ON candidates_v2
USING hnsw (vec_128 vector_cosine_ops)
WITH (
    m = 16,
    ef_construction = 64
);

-- 候选人的 32 维向量（粗召回）
CREATE INDEX idx_candidates_v2_vec32_hnsw
ON candidates_v2
USING hnsw (vec_32 vector_cosine_ops)
WITH (
    m = 16,
    ef_construction = 64
);

-- 需求画像的目标向量索引
CREATE INDEX idx_profiles_vec1024_hnsw
ON job_requirement_profiles
USING hnsw (target_vec_1024 vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- =============================================================================
-- 2. GIN 倒排索引（稀疏召回）
-- 用于 JSONB 字段的全文搜索与标签匹配
-- =============================================================================

-- candidates_v2 profile_data GIN 索引（已创建，此处补充说明）
-- CREATE INDEX idx_candidates_v2_profile_gin ON candidates_v2 USING GIN (profile_data);

-- 针对 verified_skills 数组的专门索引
CREATE INDEX idx_candidates_v2_verified_skills_gin
ON candidates_v2
USING GIN ((profile_data->'verified_skills'));

-- 使用 pg_trgm 支持模糊匹配（技能名变体）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX idx_candidates_v2_skills_trgm
ON candidates_v2
USING GIN ((profile_data->>'verified_skills') gin_trgm_ops);

-- 快照表 GIN 索引（支持 JSONB 内查询）
CREATE INDEX idx_snapshots_activated_atoms_gin
ON candidate_ability_snapshots
USING GIN (activated_atoms);

-- =============================================================================
-- 3. B-Tree 复合索引（硬过滤）
-- 用于城市、经验、薪资等精确过滤条件
-- =============================================================================

-- 候选人可见性 + 认证状态复合索引
CREATE INDEX idx_candidates_v2_searchable
ON candidates_v2(is_visible, certification_status, last_certified_at)
WHERE is_visible = TRUE AND certification_status = 'certified';

-- 地理位置索引
CREATE INDEX idx_candidates_v2_location
ON candidates_v2(preferred_city, is_remote_ok);

-- 企业私有池索引
CREATE INDEX idx_candidates_v2_private_pool
ON candidates_v2(source_enterprise_id, is_private_pool)
WHERE is_private_pool = TRUE;

-- =============================================================================
-- 4. 部分索引（Partial Index）
-- 只索引活跃数据，减少索引大小
-- =============================================================================

-- 只索引认证状态为 certified 的候选人
CREATE INDEX idx_candidates_v2_certified_only
ON candidates_v2(candidate_id, vec_1024)
WHERE certification_status = 'certified';

-- 只索引最近1年的快照
CREATE INDEX idx_snapshots_recent
ON candidate_ability_snapshots(candidate_id, created_at)
WHERE created_at > NOW() - INTERVAL '1 year';

-- =============================================================================
-- 5. 搜索性能优化视图（Materialized View）
-- 预计算热门搜索的候选集
-- =============================================================================

-- 活跃候选人简化视图（用于快速粗筛）
CREATE MATERIALIZED VIEW mv_active_candidates AS
SELECT
    candidate_id,
    vec_32,
    vec_128,
    profile_data->'verified_skills' as verified_skills,
    last_certified_at,
    preferred_city,
    is_remote_ok
FROM candidates_v2
WHERE
    is_visible = TRUE
    AND certification_status = 'certified'
    AND last_certified_at > NOW() - INTERVAL '1 year';

-- 创建视图索引
CREATE INDEX idx_mv_active_vec32_hnsw
ON mv_active_candidates USING hnsw (vec_32 vector_cosine_ops);

-- 定期刷新视图（可配置为定时任务）
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_active_candidates;

-- =============================================================================
-- 6. 查询优化提示函数
-- =============================================================================

-- 设置 HNSW 搜索参数（运行时调整 ef_search）
CREATE OR REPLACE FUNCTION set_hnsw_ef_search(ef INTEGER)
RETURNS VOID AS $$
BEGIN
    -- ef_search 控制查询时的搜索范围，越大越精确但越慢
    -- 推荐值：召回Top 10 -> ef=50; 召回Top 50 -> ef=100
    EXECUTE format('SET hnsw.ef_search = %s', ef);
END;
$$ LANGUAGE plpgsql;

-- 向量相似度搜索函数（带时间衰减）
CREATE OR REPLACE FUNCTION search_candidates_with_decay(
    query_vec VECTOR(1024),
    min_similarity FLOAT DEFAULT 0.7,
    decay_lambda FLOAT DEFAULT 0.05
)
RETURNS TABLE (
    candidate_id UUID,
    raw_similarity FLOAT,
    decay_factor FLOAT,
    adjusted_score FLOAT,
    months_since_certified FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.candidate_id,
        1 - (c.vec_1024 <=> query_vec) as raw_similarity,
        EXP(-decay_lambda * EXTRACT(MONTH FROM AGE(NOW(), c.last_certified_at))) as decay_factor,
        (1 - (c.vec_1024 <=> query_vec)) * EXP(-decay_lambda * EXTRACT(MONTH FROM AGE(NOW(), c.last_certified_at))) as adjusted_score,
        EXTRACT(MONTH FROM AGE(NOW(), c.last_certified_at)) as months_since_certified
    FROM candidates_v2 c
    WHERE
        c.is_visible = TRUE
        AND c.certification_status = 'certified'
        AND (1 - (c.vec_1024 <=> query_vec)) >= min_similarity
    ORDER BY adjusted_score DESC;
END;
$$ LANGUAGE plpgsql;
