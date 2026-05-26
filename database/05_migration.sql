-- =============================================================================
-- 归心 L9 存量数据平滑迁移脚本
-- 从旧表（schema.md 中的51张表）迁移至新 v2 表
--
-- 执行顺序：
--   1. 先执行 01_core_tables.sql（建新表）
--   2. 再执行 02_assessment_tables.sql
--   3. 再执行 03_search_tables.sql
--   4. 再执行 04_indexes.sql
--   5. 最后执行本文件（数据迁移）
--
-- 迁移策略：双写过渡
--   - 旧表保留，新表同步写入
--   - 迁移完毕并验收后再下线旧字段
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Step 1: 迁移 candidates -> candidates_v2
-- 保留核心字段，废弃旧 skill_vector（由新三层向量替代）
-- -----------------------------------------------------------------------------
INSERT INTO candidates_v2 (
    candidate_id,
    user_id,
    preferred_city,
    is_remote_ok,
    work_mode,
    salary_expectations,
    available_date,
    is_visible,
    is_private_pool,
    source_enterprise_id,
    profile_data,
    certification_status,
    created_at,
    updated_at
)
SELECT
    id                          AS candidate_id,
    user_id,
    preferred_city,
    COALESCE(is_remote_ok, FALSE),
    COALESCE(work_mode, 'both'),
    COALESCE(salary_expectations, '{}'),
    available_date,
    COALESCE(is_visible, TRUE),
    COALESCE(is_private_pool, FALSE),
    source_enterprise_id,
    -- 构建初始 profile_data，从旧 JSONB 字段聚合
    jsonb_build_object(
        'education_level',      COALESCE(education_level, 'unknown'),
        'years_of_experience',  COALESCE(years_of_experience, '0'),
        'verified_skills',      '[]'::jsonb,
        'reranker_payload',     ''
    )                           AS profile_data,
    -- 根据旧 assessments 状态推断认证状态
    CASE
        WHEN EXISTS (
            SELECT 1 FROM assessments a
            WHERE a.candidate_id = candidates.id
              AND a.status = 'scored'
        ) THEN 'certified'
        ELSE 'none'
    END                         AS certification_status,
    created_at,
    updated_at
FROM candidates
ON CONFLICT (candidate_id) DO NOTHING;  -- 幂等，支持重复执行

-- -----------------------------------------------------------------------------
-- Step 2: 迁移 assessments -> assessments_v2
-- 需要先确保 role_schemas 中有对应的 schema_id
-- 此处使用 job_cert_id 作为 schema 的代理（新系统上线后需建立正式映射）
-- -----------------------------------------------------------------------------
INSERT INTO assessments_v2 (
    assessment_id,
    candidate_id,
    schema_id,
    status,
    started_at,
    completed_at,
    overall_score,
    trust_score,
    final_report,
    created_at,
    updated_at
)
SELECT
    a.id                        AS assessment_id,
    cv2.candidate_id,
    -- 使用一个"过渡占位" schema_id（需提前在 role_schemas 插入一条默认记录）
    (SELECT schema_id FROM role_schemas WHERE role_code = 'legacy_migration' LIMIT 1),
    -- 状态映射：旧 status -> 新 status
    CASE a.status
        WHEN 'scored'           THEN 'certified'
        WHEN 'failed_cheating'  THEN 'failed'
        WHEN 'abandoned'        THEN 'abandoned'
        WHEN 'processing'       THEN 'evaluating'
        WHEN 'processed'        THEN 'evaluation_completed'
        WHEN 'interview_done'   THEN 'combat_completed'
        WHEN 'interviewing'     THEN 'combat_active'
        WHEN 'started'          THEN 'created'
        ELSE 'created'
    END                         AS status,
    a.started_at,
    a.completed_at,
    -- job_comprehensive_score 映射为 0-1 区间
    CASE
        WHEN a.job_comprehensive_score IS NOT NULL
        THEN a.job_comprehensive_score / 10.0
        ELSE NULL
    END                         AS overall_score,
    a.trust_score,
    a.final_report_structured   AS final_report,
    a.started_at                AS created_at,
    a.updated_at
FROM assessments a
JOIN candidates_v2 cv2 ON cv2.candidate_id = a.candidate_id
ON CONFLICT (assessment_id) DO NOTHING;

-- 旧表中插入过渡 schema（仅迁移期间使用）
INSERT INTO role_schemas (role_code, role_name, role_level, target_atoms, atom_weights, description, status)
VALUES (
    'legacy_migration',
    '历史数据迁移占位',
    'L7',
    ARRAY[1],
    ARRAY[1.0]::NUMERIC(8,5)[],
    '迁移脚本自动生成，待人工更新为正确岗位映射',
    'deprecated'
)
ON CONFLICT (role_code) DO NOTHING;

-- -----------------------------------------------------------------------------
-- Step 3: 回填 candidates_v2.last_certified_at
-- 从最新一次 scored 的 assessment 取 completed_at
-- -----------------------------------------------------------------------------
UPDATE candidates_v2 cv2
SET
    last_certified_at = sub.completed_at,
    certification_status = 'certified'
FROM (
    SELECT DISTINCT ON (candidate_id)
        candidate_id,
        completed_at
    FROM assessments
    WHERE status = 'scored'
      AND completed_at IS NOT NULL
    ORDER BY candidate_id, completed_at DESC
) sub
WHERE cv2.candidate_id = sub.candidate_id;

-- -----------------------------------------------------------------------------
-- Step 4: 迁移 sub_skills 中的 embedding -> candidates 的 skill_vector
-- 到 candidates_v2.vec_1024（维度对齐填充）
-- 注意：旧 skill_vector 维度未必是 1024，需检查
-- 如果旧向量维度不匹配，先跳过，待新评估系统运行后自动生成
-- -----------------------------------------------------------------------------
-- 安全检查：只有旧向量维度 = 1024 时才迁移
DO $$
DECLARE
    old_dim INT;
BEGIN
    SELECT vector_dims(skill_vector) INTO old_dim
    FROM candidates
    WHERE skill_vector IS NOT NULL
    LIMIT 1;

    IF old_dim = 1024 THEN
        UPDATE candidates_v2 cv2
        SET vec_1024 = c.skill_vector
        FROM candidates c
        WHERE cv2.candidate_id = c.id
          AND c.skill_vector IS NOT NULL;

        RAISE NOTICE '已迁移 skill_vector -> vec_1024（维度=%）', old_dim;
    ELSE
        RAISE NOTICE '旧 skill_vector 维度=%, 跳过向量迁移，待新评估流程重建', COALESCE(old_dim::TEXT, 'NULL');
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Step 5: 迁移 candidate_subskill_history -> question_ability_scores（部分映射）
-- 原始账本近似迁移：以 sub_skill_id 映射到 atom_id
-- 前提：已建立 sub_skills.id -> ability_library.atom_id 的映射关系
-- -----------------------------------------------------------------------------
-- 此步骤依赖业务方确认映射关系后执行，此处提供模板：
/*
INSERT INTO question_ability_scores (
    question_instance_id,   -- 需要一个占位 instance（历史数据无实例）
    assessment_id,
    candidate_id,
    atom_id,
    normalized_score,
    score_source,
    scored_at
)
SELECT
    gen_random_uuid(),           -- 历史数据无题目实例，生成临时 UUID
    csh.assessment_id,
    csh.candidate_id,
    -- 此处需要 sub_skill -> atom 的映射函数（业务方维护）
    get_atom_id_for_subskill(csh.sub_skill_id),
    LEAST(csh.score / 10.0, 1.0),  -- 假设旧分数 0-10，归一化到 0-1
    'human_override'::VARCHAR,
    csh.assessed_at
FROM candidate_subskill_history csh
WHERE csh.score IS NOT NULL;
*/

-- -----------------------------------------------------------------------------
-- Step 6: 验收查询（迁移后执行以验证数据一致性）
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    old_count  BIGINT;
    new_count  BIGINT;
BEGIN
    SELECT COUNT(*) INTO old_count FROM candidates;
    SELECT COUNT(*) INTO new_count FROM candidates_v2
        WHERE user_id IN (SELECT user_id FROM candidates);

    RAISE NOTICE '迁移验收 candidates: 旧表=%, 新表迁入=%', old_count, new_count;

    IF new_count < old_count THEN
        RAISE WARNING '部分候选人记录未迁移，请检查 user_id 冲突或约束失败';
    END IF;
END $$;
