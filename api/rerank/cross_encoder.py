"""
Cross-Encoder 重排层

功能：
    将 HR 的原始查询与 Top 50 候选人的 reranker_payload 进行深度交叉注意力计算，
    输出最终相关性得分最高的 Top 10 候选人。

架构位置：
    Filter -> Recall -> RRF Fusion -> Cross-Encoder Rerank -> Diversity Post-process -> Final Cut

注：此处为 Mock 实现，真实场景调用 BGE-Reranker 或类似模型
"""

from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class RerankResult:
    """重排结果"""
    candidate_id: str
    rerank_score: float           # Cross-Encoder 相关性得分
    original_rrf_score: float     # 原始 RRF 得分
    decision_reason: str          # 排序理由
    strength_atoms: List[str]     # 匹配强项
    risk_flags: List[str]         # 风险提示


class CrossEncoderReranker:
    """Cross-Encoder 重排器"""

    def __init__(self, model_name: str = "bge-reranker-large"):
        self.model_name = model_name
        self.mock_client = None

    async def rerank(
        self,
        query: str,
        candidates: List[Dict],
        top_k: int = 10,
        evidence_package: Optional[Dict] = None
    ) -> List[RerankResult]:
        """
        执行 Cross-Encoder 重排

        Args:
            query: HR 原始查询
            candidates: 经 RRF 筛选的候选列表（通常 Top 50）
            top_k: 返回前 K 个
            evidence_package: 额外证据包（如 must_have 能力要求）

        Returns:
            重排后的结果列表
        """
        # Mock 实现：使用 LLM 客户端计算分数
        from mocks.llm_client import llm_client

        # 调用模拟的 Cross-Encoder
        reranked = await llm_client.rerank(query, candidates, top_k=len(candidates))

        # 构建详细结果
        results = []
        for item in reranked[:top_k]:
            candidate = next(
                (c for c in candidates if c["candidate_id"] == item["candidate_id"]),
                None
            )

            if candidate:
                # 生成解释
                reason = self._generate_reason(query, candidate, item["rerank_score"])
                strengths = self._extract_strengths(query, candidate)
                risks = self._assess_risks(candidate, evidence_package)

                results.append(RerankResult(
                    candidate_id=item["candidate_id"],
                    rerank_score=item["rerank_score"],
                    original_rrf_score=candidate.get("rrf_score", 0),
                    decision_reason=reason,
                    strength_atoms=strengths,
                    risk_flags=risks
                ))

        # 按重排分数排序
        results.sort(key=lambda x: x.rerank_score, reverse=True)
        return results[:top_k]

    def _generate_reason(self, query: str, candidate: Dict, score: float) -> str:
        """生成排序理由"""
        query_keywords = ["高并发", "Redis", "分布式", "Go", "微服务"]
        found_keywords = [kw for kw in query_keywords if kw in query]

        payload = candidate.get("reranker_payload", "")
        matched = [kw for kw in found_keywords if kw in payload]

        if score > 0.8:
            return f"强烈推荐：候选人在{', '.join(matched)}方面表现卓越"
        elif score > 0.6:
            return f"推荐：候选人与{', '.join(matched)}需求匹配度良好"
        else:
            return "备选：基本满足要求，但某些方面有待验证"

    def _extract_strengths(self, query: str, candidate: Dict) -> List[str]:
        """提取匹配强项"""
        strengths = []
        payload = candidate.get("reranker_payload", "")

        if "Redis" in query and "Redis" in payload:
            strengths.append("Redis分布式锁实战经验")
        if "并发" in query and ("并发" in payload or "goroutine" in payload):
            strengths.append("高并发系统设计能力")
        if "微服务" in query and "微服务" in payload:
            strengths.append("微服务架构落地经验")

        return strengths if strengths else ["综合技术能力扎实"]

    def _assess_risks(self, candidate: Dict, evidence_package: Optional[Dict]) -> List[str]:
        """评估潜在风险"""
        risks = []

        # 检查 must_have 能力缺失
        if evidence_package:
            must_have = evidence_package.get("must_have_atoms", [])
            # Mock 检查逻辑
            if must_have and len(must_have) > 0:
                if candidate.get("score_l3", 0) < 0.6:
                    risks.append("核心能力匹配度偏低")

        # 认证时间久远
        months = candidate.get("months_since_certified", 0)
        if months > 12:
            risks.append(f"认证时间久远（{months}个月前）")

        return risks if risks else []

    async def batch_rerank(
        self,
        query: str,
        candidates: List[Dict],
        batch_size: int = 16,
        top_k: int = 10
    ) -> List[RerankResult]:
        """
        分批重排（用于大规模候选集）

        真实场景：Cross-Encoder 计算成本较高，需要分批处理
        """
        all_results = []

        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            batch_results = await self.rerank(query, batch, top_k=len(batch))
            all_results.extend(batch_results)

        # 全局排序
        all_results.sort(key=lambda x: x.rerank_score, reverse=True)
        return all_results[:top_k]


class DiversityReranker:
    """
    多样性后处理

    在 Cross-Encoder 之后应用，确保结果多样性
    （例如：不全是同一公司背景的候选人）
    """

    def __init__(self, diversity_weight: float = 0.1):
        self.diversity_weight = diversity_weight

    def apply_diversity(
        self,
        reranked_results: List[RerankResult],
        candidate_details: List[Dict],
        final_cut: int = 10
    ) -> List[RerankResult]:
        """
        应用多样性重排

        策略：
        1. 如果前 N 名中有相似背景（如技能栈高度重合），降低其排名
        2. 确保地域、经验年限有一定分布
        """
        # Mock 实现：简化版，直接返回前 N 个
        return reranked_results[:final_cut]


# 便捷函数
async def rerank_candidates(
    query: str,
    candidates: List[Dict],
    top_k: int = 10,
    use_diversity: bool = True
) -> List[Dict]:
    """
    便捷函数：执行完整的重排流程
    """
    reranker = CrossEncoderReranker()
    results = await reranker.rerank(query, candidates, top_k=top_k)

    if use_diversity:
        diversity = DiversityReranker()
        results = diversity.apply_diversity(results, candidates, final_cut=top_k)

    # 转换为字典列表
    return [
        {
            "candidate_id": r.candidate_id,
            "score_rerank": r.rerank_score,
            "score_original": r.original_rrf_score,
            "reason": r.decision_reason,
            "strengths": r.strength_atoms,
            "risks": r.risk_flags
        }
        for r in results
    ]
