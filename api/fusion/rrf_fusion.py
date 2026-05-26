"""
RRF (Reciprocal Rank Fusion) 倒数秩融合实现

核心算法：
    RRF_score(d) = Σ weight_i / (k + rank_i(d))

其中：
    - k: 常数（通常取60），用于平滑低排名项的贡献
    - rank_i(d): 文档d在第i个召回列表中的排名
    - weight_i: 第i个召回列表的权重

设计约束：
    1. 只接收 (candidate_id, rank) 对，不拉取全量数据
    2. 在内存中完成计算，不调用外部模型
    3. 时间复杂度 O(N log N)，N为候选数
"""

from typing import List, Dict, Optional
from dataclasses import dataclass
from collections import defaultdict


@dataclass
class RecallList:
    """单个召回列表"""
    name: str                    # 召回源名称: "l1", "l2", "l3", "sparse"
    results: List[Dict]          # [{"candidate_id": str, "score": float}, ...]
    weight: float = 1.0          # 该路召回的权重


class RRFFusion:
    """RRF 融合器"""

    DEFAULT_K = 60               # RRF 常数

    def __init__(self, k: int = DEFAULT_K):
        self.k = k

    def fuse(
        self,
        recall_lists: List[RecallList],
        top_k: int = 50
    ) -> List[Dict]:
        """
        执行 RRF 融合

        Args:
            recall_lists: 多个召回列表
            top_k: 返回前K个结果

        Returns:
            按 RRF 分数排序的结果列表
        """
        # 收集所有出现的 candidate_id
        all_candidates = set()
        for rlist in recall_lists:
            for result in rlist.results:
                all_candidates.add(result["candidate_id"])

        # 为每个候选计算 RRF 分数
        rrf_scores = defaultdict(lambda: {
            "rrf_score": 0.0,
            "ranks": {},
            "scores": {}
        })

        for rlist in recall_lists:
            for rank, result in enumerate(rlist.results, start=1):
                candidate_id = result["candidate_id"]

                # RRF 公式: weight / (k + rank)
                contribution = rlist.weight / (self.k + rank)
                rrf_scores[candidate_id]["rrf_score"] += contribution
                rrf_scores[candidate_id]["ranks"][rlist.name] = rank
                rrf_scores[candidate_id]["scores"][rlist.name] = result.get("score", 0.0)

        # 转换为列表并排序
        fused_results = []
        for candidate_id, data in rrf_scores.items():
            fused_results.append({
                "candidate_id": candidate_id,
                "rrf_score": round(data["rrf_score"], 6),
                "ranks": data["ranks"],
                "scores": data["scores"]
            })

        fused_results.sort(key=lambda x: x["rrf_score"], reverse=True)
        return fused_results[:top_k]

    def fuse_weighted(
        self,
        l1_results: Optional[List[Dict]],
        l2_results: Optional[List[Dict]],
        l3_results: Optional[List[Dict]],
        sparse_results: Optional[List[Dict]],
        weights: Dict[str, float],
        top_k: int = 50
    ) -> List[Dict]:
        """
        带权重的分层 RRF 融合

        支持的分层权重：
            - l1_weight: 32维粗召回权重
            - l2_weight: 128维中召回权重
            - l3_weight: 1024维精召回权重
            - sparse_weight: 稀疏召回权重
        """
        recall_lists = []

        if l1_results and weights.get("l1", 0) > 0:
            recall_lists.append(RecallList(
                name="l1",
                results=l1_results,
                weight=weights["l1"]
            ))

        if l2_results and weights.get("l2", 0) > 0:
            recall_lists.append(RecallList(
                name="l2",
                results=l2_results,
                weight=weights["l2"]
            ))

        if l3_results and weights.get("l3", 0) > 0:
            recall_lists.append(RecallList(
                name="l3",
                results=l3_results,
                weight=weights["l3"]
            ))

        if sparse_results and weights.get("sparse", 0) > 0:
            recall_lists.append(RecallList(
                name="sparse",
                results=sparse_results,
                weight=weights["sparse"]
            ))

        return self.fuse(recall_lists, top_k)


class TieredRRFFusion(RRFFusion):
    """
    分层级联 RRF 融合

    实现方案设计中的四段路由：
    L1(32) --\> L2(128) --\> L3(1024) + 稀疏 --\> RRF 融合
    """

    def cascade_fuse(
        self,
        l1_results: List[Dict],
        l2_results: List[Dict],
        l3_results: List[Dict],
        sparse_results: List[Dict],
        tier_weights: Dict[str, float],
        top_k: int = 50
    ) -> List[Dict]:
        """
        分层级联融合

        融合公式：
            final_score = w1*RRF_32 + w2*RRF_128 + w3*RRF_1024 + w4*RRF_sparse
        """
        # 分别计算各层的 RRF 贡献
        all_candidates = set()
        for results in [l1_results, l2_results, l3_results, sparse_results]:
            for r in results:
                all_candidates.add(r["candidate_id"])

        # 为每个候选收集各层排名
        candidate_tiers = defaultdict(lambda: {
            "l1_rank": None, "l1_score": None,
            "l2_rank": None, "l2_score": None,
            "l3_rank": None, "l3_score": None,
            "sparse_rank": None, "sparse_score": None,
        })

        for rank, r in enumerate(l1_results, 1):
            candidate_tiers[r["candidate_id"]]["l1_rank"] = rank
            candidate_tiers[r["candidate_id"]]["l1_score"] = r.get("score", 0)

        for rank, r in enumerate(l2_results, 1):
            candidate_tiers[r["candidate_id"]]["l2_rank"] = rank
            candidate_tiers[r["candidate_id"]]["l2_score"] = r.get("score", 0)

        for rank, r in enumerate(l3_results, 1):
            candidate_tiers[r["candidate_id"]]["l3_rank"] = rank
            candidate_tiers[r["candidate_id"]]["l3_score"] = r.get("score", 0)

        for rank, r in enumerate(sparse_results, 1):
            candidate_tiers[r["candidate_id"]]["sparse_rank"] = rank
            candidate_tiers[r["candidate_id"]]["sparse_score"] = r.get("score", 0)

        # 计算加权 RRF 分数
        k = self.k
        final_scores = []

        for candidate_id, tiers in candidate_tiers.items():
            score = 0.0

            if tiers["l1_rank"] and tier_weights.get("l1", 0) > 0:
                score += tier_weights["l1"] / (k + tiers["l1_rank"])

            if tiers["l2_rank"] and tier_weights.get("l2", 0) > 0:
                score += tier_weights["l2"] / (k + tiers["l2_rank"])

            if tiers["l3_rank"] and tier_weights.get("l3", 0) > 0:
                score += tier_weights["l3"] / (k + tiers["l3_rank"])

            if tiers["sparse_rank"] and tier_weights.get("sparse", 0) > 0:
                score += tier_weights["sparse"] / (k + tiers["sparse_rank"])

            final_scores.append({
                "candidate_id": candidate_id,
                "rrf_score": round(score, 6),
                "tier_details": tiers
            })

        final_scores.sort(key=lambda x: x["rrf_score"], reverse=True)
        return final_scores[:top_k]


# 工具函数
def calculate_time_decay(months_since_certified: int, lambda_val: float = 0.05) -> float:
    """
    计算时间衰减因子

    公式: decay = e^(-λ * months)

    Args:
        months_since_certified: 距离最后认证的月数
        lambda_val: 衰减系数 (默认 0.05，约 5%/月)

    Returns:
        衰减因子 (0-1)
    """
    import math
    return math.exp(-lambda_val * min(months_since_certified, 24))  # 最大衰减24个月


def apply_time_decay(
    candidates: List[Dict],
    lambda_val: float = 0.05
) -> List[Dict]:
    """对候选列表应用时间衰减"""
    from datetime import datetime

    now = datetime.utcnow()

    for c in candidates:
        if "last_certified_at" in c:
            months = (now - c["last_certified_at"]).days / 30
            decay = calculate_time_decay(int(months), lambda_val)
            c["time_decay_factor"] = round(decay, 4)
            c["decay_adjusted_score"] = round(c.get("score", 0) * decay, 4)

    return candidates
