"""
Mock LLM 客户端
模拟 Query Parser Agent 和 Cross-Encoder 调用
"""

import random
from typing import List, Dict
import numpy as np


class MockLLMClient:
    """模拟 LLM 客户端"""

    # 关键词到原子能力的映射（简化版）
    KEYWORD_ATOM_MAP = {
        "go": [42, 43, 44],
        "golang": [42, 43, 44],
        "并发": [42, 43, 301],
        "redis": [145, 146, 147],
        "分布式": [145, 203, 301, 405],
        "死锁": [42, 145],
        "微服务": [301, 302, 405],
        "高并发": [42, 301, 512],
        "python": [101, 102, 103],
        "ai": [201, 202, 203],
        "后端": [42, 301, 405],
        "架构": [405, 406],
    }

    async def parse_query(self, query: str) -> Dict:
        """
        模拟 Query Parser Agent
        将自然语言查询解析为结构化表示
        """
        query_lower = query.lower()

        # 提取标签
        extracted_tags = []
        target_atoms = set()

        for keyword, atoms in self.KEYWORD_ATOM_MAP.items():
            if keyword in query_lower:
                extracted_tags.append(keyword)
                target_atoms.update(atoms)

        if not extracted_tags:
            extracted_tags = ["后端开发", "通用"]
            target_atoms = {42, 301, 405}

        # 生成目标向量（简化：在目标维度上设置高值）
        vec_1024 = np.random.beta(2, 5, 1024) * 0.2
        for atom_id in target_atoms:
            vec_1024[atom_id - 1] = random.uniform(0.7, 0.95)

        # 降维
        vec_128 = self._downsample(vec_1024, 128)
        vec_32 = self._downsample(vec_1024, 32)

        # 原子权重
        atom_weights = {atom_id: round(random.uniform(0.2, 0.4), 2) for atom_id in target_atoms}

        return {
            "original_query": query,
            "extracted_tags": list(set(extracted_tags)),
            "target_atoms": list(target_atoms),
            "atom_weights": atom_weights,
            "target_vec_32": vec_32.tolist(),
            "target_vec_128": vec_128.tolist(),
            "target_vec_1024": vec_1024.tolist(),
        }

    async def rerank(
        self,
        query: str,
        candidates: List[Dict],
        top_k: int = 10
    ) -> List[Dict]:
        """
        模拟 Cross-Encoder 重排
        输入查询和候选列表，输出重排后的结果
        """
        results = []

        for cand in candidates:
            payload = cand.get("reranker_payload", "")

            # 模拟 Cross-Encoder 分数（简化：基于关键词匹配 + 噪声）
            base_score = self._compute_cross_encoder_score(query, payload)

            # 添加随机噪声模拟模型不确定性
            noise = random.gauss(0, 0.05)
            score = max(0.0, min(1.0, base_score + noise))

            results.append({
                "candidate_id": cand["candidate_id"],
                "rerank_score": round(score, 4),
                "original_payload": payload
            })

        # 按重排分数排序
        results.sort(key=lambda x: x["rerank_score"], reverse=True)
        return results[:top_k]

    def _compute_cross_encoder_score(self, query: str, payload: str) -> float:
        """模拟 Cross-Encoder 打分"""
        query_lower = query.lower()
        payload_lower = payload.lower()

        score = 0.5  # 基线分数

        # 关键词匹配加分
        keywords = ["redis", "go", "golang", "并发", "分布式", "微服务"]
        for kw in keywords:
            if kw in query_lower and kw in payload_lower:
                score += 0.15

        # 场景匹配加分
        scenarios = {
            "死锁": ["死锁", "锁", "并发"],
            "高并发": ["高并发", "性能", "优化"],
        }
        for scenario, related_terms in scenarios.items():
            if any(term in query_lower for term in related_terms):
                if any(term in payload_lower for term in related_terms):
                    score += 0.1

        return min(0.95, score)

    def _downsample(self, vec: np.ndarray, target_dim: int) -> np.ndarray:
        """降维"""
        ratio = len(vec) // target_dim
        return np.array([vec[i:i+ratio].mean() for i in range(0, len(vec), ratio)][:target_dim])


# 全局实例
llm_client = MockLLMClient()
