"""
Mock 数据库客户端
模拟 PostgreSQL + pgvector 的 RPC 调用
"""

import numpy as np
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import random


class MockDBClient:
    """模拟数据库客户端"""

    def __init__(self, seed_candidates: int = 50):
        self.candidates = self._generate_mock_candidates(seed_candidates)

    def _generate_mock_candidates(self, count: int) -> List[Dict]:
        """生成模拟候选人数据"""
        candidates = []

        skills_pool = [
            ["Golang", "Redis", "Kafka", "Microservices"],
            ["Python", "PyTorch", "TensorFlow", "MLOps"],
            ["Java", "Spring", "MySQL", "Redis"],
            ["JavaScript", "React", "Node.js", "TypeScript"],
            ["Rust", "Distributed Systems", "Kubernetes", "gRPC"],
        ]

        cities = ["北京", "上海", "深圳", "杭州", "广州", "成都"]

        for i in range(count):
            # 生成 1024 维向量（稀疏）
            vec_1024 = np.random.beta(2, 5, 1024) * 0.3  # 大部分值在 0.0-0.3 之间

            # 设置几个激活的能力
            activated_atoms = random.sample(range(1, 1025), random.randint(5, 15))
            for atom_id in activated_atoms:
                vec_1024[atom_id - 1] = random.uniform(0.5, 0.95)

            # 从 1024 维聚合出 128 维和 32 维（简化）
            vec_128 = self._downsample(vec_1024, 128)
            vec_32 = self._downsample(vec_1024, 32)

            # 认证时间（随机在过去2年内）
            months_ago = random.randint(0, 24)
            certified_at = datetime.utcnow() - timedelta(days=30 * months_ago)

            candidate = {
                "candidate_id": f"cand-{i+1:03d}",
                "vec_32": vec_32.tolist(),
                "vec_128": vec_128.tolist(),
                "vec_1024": vec_1024.tolist(),
                "verified_skills": random.choice(skills_pool),
                "last_certified_at": certified_at,
                "preferred_city": random.choice(cities),
                "is_remote_ok": random.choice([True, False]),
                "is_visible": True,
                "certification_status": "certified",
                "experience_years": random.randint(1, 10),
                "reranker_payload": self._generate_reranker_payload(i),
            }
            candidates.append(candidate)

        return candidates

    def _downsample(self, vec: np.ndarray, target_dim: int) -> np.ndarray:
        """降维（简化实现：分段平均）"""
        ratio = len(vec) // target_dim
        return np.array([vec[i:i+ratio].mean() for i in range(0, len(vec), ratio)][:target_dim])

    def _generate_reranker_payload(self, idx: int) -> str:
        """生成 reranker payload"""
        payloads = [
            "候选人在包含Redis死锁的Go微服务残卷中，准确识别并重构了分布式锁逻辑，耗时12分钟。",
            "候选人展现了优秀的PyTorch模型优化能力，在显存受限场景下成功实现梯度检查点。",
            "候选人完成Java Spring分布式事务设计，准确处理了TCC补偿逻辑。",
            "候选人快速定位React性能瓶颈，通过虚拟列表优化将渲染时间降低80%。",
            "候选人在Rust异步编程场景表现优异，正确处理了并发竞争条件。",
        ]
        return payloads[idx % len(payloads)]

    # =========================================================================
    # 分层召回接口
    # =========================================================================

    async def recall_l1_coarse(
        self,
        target_vec_32: List[float],
        limit: int = 200,
        filters: Optional[Dict] = None
    ) -> List[Dict]:
        """L1 粗召回：32维向量相似度"""
        target = np.array(target_vec_32)
        results = []

        for c in self.candidates:
            if not self._pass_filters(c, filters):
                continue

            vec = np.array(c["vec_32"])
            similarity = self._cosine_similarity(target, vec)

            results.append({
                "candidate_id": c["candidate_id"],
                "score": float(similarity),
                "source": "l1"
            })

        # 排序取 Top K
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    async def recall_l2_medium(
        self,
        target_vec_128: List[float],
        candidate_ids: List[str],
        limit: int = 80
    ) -> List[Dict]:
        """L2 中召回：128维向量相似度（在L1结果上精筛）"""
        target = np.array(target_vec_128)
        candidate_set = set(candidate_ids)
        results = []

        for c in self.candidates:
            if c["candidate_id"] not in candidate_set:
                continue

            vec = np.array(c["vec_128"])
            similarity = self._cosine_similarity(target, vec)

            results.append({
                "candidate_id": c["candidate_id"],
                "score": float(similarity),
                "source": "l2"
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    async def recall_l3_fine(
        self,
        target_vec_1024: List[float],
        candidate_ids: List[str],
        limit: int = 40
    ) -> List[Dict]:
        """L3 精召回：1024维向量相似度（在L2结果上精筛）"""
        target = np.array(target_vec_1024)
        candidate_set = set(candidate_ids)
        results = []

        for c in self.candidates:
            if c["candidate_id"] not in candidate_set:
                continue

            vec = np.array(c["vec_1024"])
            similarity = self._cosine_similarity(target, vec)

            results.append({
                "candidate_id": c["candidate_id"],
                "score": float(similarity),
                "source": "l3"
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    async def recall_sparse_bm25(
        self,
        query_tags: List[str],
        limit: int = 100,
        filters: Optional[Dict] = None
    ) -> List[Dict]:
        """稀疏召回：基于标签的BM25风格匹配"""
        results = []

        for c in self.candidates:
            if not self._pass_filters(c, filters):
                continue

            # 简化版 BM25 分数计算
            score = self._bm25_score(query_tags, c["verified_skills"])

            if score > 0:
                results.append({
                    "candidate_id": c["candidate_id"],
                    "score": float(score),
                    "source": "sparse"
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    # =========================================================================
    # 辅助方法
    # =========================================================================

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """计算余弦相似度"""
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def _bm25_score(self, query_tags: List[str], candidate_skills: List[str]) -> float:
        """简化 BM25 分数"""
        score = 0.0
        for tag in query_tags:
            for skill in candidate_skills:
                if tag.lower() in skill.lower() or skill.lower() in tag.lower():
                    score += 1.0
        return score

    def _pass_filters(self, candidate: Dict, filters: Optional[Dict]) -> bool:
        """检查是否通过过滤器"""
        if not filters:
            return True

        if filters.get("exclude_private_pool") and not candidate.get("is_visible", True):
            return False

        if filters.get("is_remote_ok") is not None:
            if filters["is_remote_ok"] and not candidate.get("is_remote_ok", False):
                return False

        if filters.get("cities"):
            if candidate.get("preferred_city") not in filters["cities"]:
                return False

        return True

    async def get_candidate_details(self, candidate_ids: List[str]) -> List[Dict]:
        """批量获取候选人详情"""
        id_set = set(candidate_ids)
        return [c for c in self.candidates if c["candidate_id"] in id_set]


# 全局实例
db_client = MockDBClient(seed_candidates=50)
