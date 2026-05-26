"""
归心 L9 B端混合检索管线 - 主控制器

完整链路：
    HR Query
        ↓
    [Query Parser Agent] → 解析为 Target Vector + Tags
        ↓
    [Filter Gate] → 硬过滤（城市、经验等）
        ↓
    ┌─────────────────────────────────────────────────────┐
    │  双轨并发召回                                         │
    │  ├─ L1 粗召回 (32维) → Top 200                       │
    │  ├─ L2 中召回 (128维) → Top 80                       │
    │  ├─ L3 精召回 (1024维) → Top 40                      │
    │  └─ 稀疏召回 (BM25) → Top 100                        │
    └─────────────────────────────────────────────────────┘
        ↓
    [RRF 倒数秩融合] → k=60, 加权融合 → Top 50
        ↓
    [时间衰减调整] → e^(-λ·Δt) 调整分数
        ↓
    [Cross-Encoder 重排] → Top 10
        ↓
    [多样性后处理] → Final Cut
        ↓
    返回 HR

性能约束：
    - 总延迟 < 1500ms
    - 数据库调用并行化
    - RRF 在内存中完成，不调用 LLM
"""

import time
import asyncio
from typing import List, Dict, Optional
from datetime import datetime

from models import (
    SearchRequest, SearchResponse, SearchMetrics,
    CandidateMatch, AtomScoreDetail, FilterConfig,
    ParsedQuery
)
from config import search_config
from mocks.db_client import db_client
from mocks.llm_client import llm_client
from fusion.rrf_fusion import TieredRRFFusion, calculate_time_decay
from rerank.cross_encoder import CrossEncoderReranker, DiversityReranker


class SearchPipeline:
    """搜索管线主控制器"""

    def __init__(self):
        self.db = db_client
        self.llm = llm_client
        self.rrf = TieredRRFFusion(k=search_config.RRF_K)
        self.reranker = CrossEncoderReranker(model_name=search_config.RERANK_MODEL)
        self.diversity = DiversityReranker()

    async def search(self, request: SearchRequest) -> SearchResponse:
        """
        执行完整搜索流程

        Args:
            request: 搜索请求

        Returns:
            搜索结果响应
        """
        start_time = time.time()
        metrics = {}

        # =====================================================================
        # Stage 1: Query Parser Agent
        # =====================================================================
        parsed_query = await self._parse_query(request.query)

        # =====================================================================
        # Stage 2: Filter Gate
        # =====================================================================
        filters = self._build_filters(request.filters)

        # =====================================================================
        # Stage 3: 分层召回 (并行执行)
        # =====================================================================
        recall_start = time.time()

        # L1 粗召回（全局）
        l1_task = self._recall_l1(parsed_query, filters) if request.enable_l1 else asyncio.sleep(0)

        # 等待 L1 完成后执行 L2、L3
        l1_results = await l1_task
        metrics["l1_time_ms"] = int((time.time() - recall_start) * 1000)
        metrics["l1_candidates"] = len(l1_results)

        # 提取 L1 候选 ID 用于 L2 过滤
        l1_candidate_ids = [r["candidate_id"] for r in l1_results]

        # L2 + L3 + 稀疏召回并行
        l2_start = time.time()
        l2_task = self._recall_l2(parsed_query, l1_candidate_ids) if request.enable_l2 else asyncio.sleep(0)
        sparse_task = self._recall_sparse(parsed_query, filters) if request.enable_sparse else asyncio.sleep(0)

        l2_results, sparse_results = await asyncio.gather(l2_task, sparse_task)
        metrics["l2_time_ms"] = int((time.time() - l2_start) * 1000)
        metrics["l2_candidates"] = len(l2_results)

        # L3 在 L2 结果上执行
        l3_start = time.time()
        l3_candidate_ids = [r["candidate_id"] for r in l2_results]
        l3_results = await self._recall_l3(parsed_query, l3_candidate_ids) if request.enable_l3 else []
        metrics["l3_time_ms"] = int((time.time() - l3_start) * 1000)
        metrics["l3_candidates"] = len(l3_results)

        metrics["sparse_candidates"] = len(sparse_results)

        # =====================================================================
        # Stage 4: RRF 倒数秩融合
        # =====================================================================
        rrf_start = time.time()

        rrf_results = self.rrf.cascade_fuse(
            l1_results=l1_results,
            l2_results=l2_results,
            l3_results=l3_results,
            sparse_results=sparse_results,
            tier_weights={
                "l1": search_config.RRF_WEIGHT_32,
                "l2": search_config.RRF_WEIGHT_128,
                "l3": search_config.RRF_WEIGHT_1024,
                "sparse": search_config.RRF_WEIGHT_SPARSE
            },
            top_k=search_config.RRF_TOP_K
        )

        metrics["rrf_time_ms"] = int((time.time() - rrf_start) * 1000)
        metrics["rrf_candidates"] = len(rrf_results)

        # =====================================================================
        # Stage 5: 时间衰减调整
        # =====================================================================
        if search_config.TIME_DECAY_ENABLED:
            rrf_results = await self._apply_time_decay(rrf_results)

        # =====================================================================
        # Stage 6: Cross-Encoder 重排
        # =====================================================================
        rerank_start = time.time()

        # 获取 Top 50 候选详情
        top_50_ids = [r["candidate_id"] for r in rrf_results[:search_config.RERANK_TOP_K]]
        candidate_details = await self.db.get_candidate_details(top_50_ids)

        # 合并 RRF 分数到详情
        id_to_rrf = {r["candidate_id"]: r for r in rrf_results}
        for c in candidate_details:
            c["rrf_score"] = id_to_rrf.get(c["candidate_id"], {}).get("rrf_score", 0)

        # 执行重排
        reranked = await self.reranker.rerank(
            query=request.query,
            candidates=candidate_details,
            top_k=search_config.FINAL_CUT
        )

        metrics["rerank_time_ms"] = int((time.time() - rerank_start) * 1000)
        metrics["rerank_candidates"] = len(reranked)

        # =====================================================================
        # Stage 7: 构建响应
        # =====================================================================
        candidates = self._build_candidate_matches(
            reranked, candidate_details, id_to_rrf
        )

        total_time_ms = int((time.time() - start_time) * 1000)

        return SearchResponse(
            session_id=f"sess_{int(start_time * 1000)}",
            query=request.query,
            total_results=len(candidates),
            candidates=candidates,
            metrics=SearchMetrics(
                total_time_ms=total_time_ms,
                **metrics
            ),
            parsed_tags=parsed_query.extracted_tags,
            target_atoms=parsed_query.target_atoms
        )

    async def _parse_query(self, query: str) -> ParsedQuery:
        """调用 Query Parser Agent 解析查询"""
        result = await self.llm.parse_query(query)
        return ParsedQuery(**result)

    def _build_filters(self, filter_config: FilterConfig) -> Dict:
        """构建过滤器字典"""
        return {
            "cities": filter_config.cities,
            "is_remote_ok": filter_config.is_remote_ok,
            "min_experience": filter_config.min_experience,
            "exclude_private_pool": filter_config.exclude_private_pool
        }

    async def _recall_l1(self, parsed: ParsedQuery, filters: Dict) -> List[Dict]:
        """L1 粗召回：32维向量"""
        return await self.db.recall_l1_coarse(
            target_vec_32=parsed.target_vec_32,
            limit=search_config.L1_COARSE_LIMIT,
            filters=filters
        )

    async def _recall_l2(self, parsed: ParsedQuery, candidate_ids: List[str]) -> List[Dict]:
        """L2 中召回：128维向量"""
        return await self.db.recall_l2_medium(
            target_vec_128=parsed.target_vec_128,
            candidate_ids=candidate_ids,
            limit=search_config.L2_MEDIUM_LIMIT
        )

    async def _recall_l3(self, parsed: ParsedQuery, candidate_ids: List[str]) -> List[Dict]:
        """L3 精召回：1024维向量"""
        return await self.db.recall_l3_fine(
            target_vec_1024=parsed.target_vec_1024,
            candidate_ids=candidate_ids,
            limit=search_config.L3_FINE_LIMIT
        )

    async def _recall_sparse(self, parsed: ParsedQuery, filters: Dict) -> List[Dict]:
        """稀疏召回：基于标签的 BM25 匹配"""
        return await self.db.recall_sparse_bm25(
            query_tags=parsed.extracted_tags,
            limit=search_config.SPARSE_LIMIT,
            filters=filters
        )

    async def _apply_time_decay(self, results: List[Dict]) -> List[Dict]:
        """应用时间衰减因子"""
        candidate_ids = [r["candidate_id"] for r in results]
        candidates = await self.db.get_candidate_details(candidate_ids)

        now = datetime.utcnow()

        for result in results:
            candidate = next(
                (c for c in candidates if c["candidate_id"] == result["candidate_id"]),
                None
            )
            if candidate and candidate.get("last_certified_at"):
                months = (now - candidate["last_certified_at"]).days / 30
                decay = calculate_time_decay(
                    int(months),
                    search_config.TIME_DECAY_LAMBDA
                )
                result["time_decay_factor"] = round(decay, 4)
                result["rrf_score"] = round(result["rrf_score"] * decay, 6)

        # 重新排序
        results.sort(key=lambda x: x["rrf_score"], reverse=True)
        return results

    def _build_candidate_matches(
        self,
        reranked: List,
        candidate_details: List[Dict],
        id_to_rrf: Dict
    ) -> List[CandidateMatch]:
        """构建最终的候选人匹配结果"""
        matches = []

        for rank, result in enumerate(reranked, 1):
            candidate = next(
                (c for c in candidate_details if c["candidate_id"] == result.candidate_id),
                None
            )

            if not candidate:
                continue

            rrf_data = id_to_rrf.get(result.candidate_id, {})
            tier_details = rrf_data.get("tier_details", {})

            match = CandidateMatch(
                candidate_id=result.candidate_id,
                rank=rank,
                score_l1=tier_details.get("l1_score"),
                score_l2=tier_details.get("l2_score"),
                score_l3=tier_details.get("l3_score"),
                score_sparse=tier_details.get("sparse_score"),
                score_rrf=rrf_data.get("rrf_score", 0),
                score_rerank=result.rerank_score,
                final_score=round(result.rerank_score * 0.8 + rrf_data.get("rrf_score", 0) * 0.2, 4),
                time_decay_factor=rrf_data.get("time_decay_factor"),
                matched_atoms=[  # Mock 数据
                    AtomScoreDetail(atom_id=42, atom_name="Go并发编程", score=0.85),
                    AtomScoreDetail(atom_id=145, atom_name="Redis分布式锁", score=0.72),
                ],
                verified_skills=candidate.get("verified_skills", []),
                match_explanation=result.decision_reason,
                experience_years=candidate.get("experience_years"),
                preferred_city=candidate.get("preferred_city"),
                is_remote_ok=candidate.get("is_remote_ok")
            )
            matches.append(match)

        return matches


# 全局管线实例
search_pipeline = SearchPipeline()
