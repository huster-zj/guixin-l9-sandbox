"""
归心 L9 搜索管线配置
"""

from pydantic_settings import BaseSettings
from typing import List, Dict


class SearchConfig(BaseSettings):
    """搜索管线配置"""

    # 服务配置
    APP_NAME: str = "归心 L9 搜索管线"
    DEBUG: bool = False

    # 分层召回配置
    L1_COARSE_DIMENSION: int = 32          # L1 粗召回维度
    L1_COARSE_LIMIT: int = 200             # L1 返回数量
    L1_MIN_SIMILARITY: float = 0.5         # L1 最小相似度

    L2_MEDIUM_DIMENSION: int = 128         # L2 中召回维度
    L2_MEDIUM_LIMIT: int = 80              # L2 返回数量
    L2_MIN_SIMILARITY: float = 0.6         # L2 最小相似度

    L3_FINE_DIMENSION: int = 1024          # L3 精召回维度
    L3_FINE_LIMIT: int = 40                # L3 返回数量
    L3_MIN_SIMILARITY: float = 0.7         # L3 最小相似度

    # 稀疏召回配置
    SPARSE_LIMIT: int = 100                # 稀疏召回数量
    SPARSE_MIN_SCORE: float = 0.3          # BM25 最小分数

    # RRF 融合配置
    RRF_K: int = 60                        # RRF 常数
    RRF_TOP_K: int = 50                    # RRF 后取 Top K

    # RRF 分层权重
    RRF_WEIGHT_32: float = 0.15            # 32维得分权重
    RRF_WEIGHT_128: float = 0.25           # 128维得分权重
    RRF_WEIGHT_1024: float = 0.40          # 1024维得分权重
    RRF_WEIGHT_SPARSE: float = 0.20        # 稀疏得分权重

    # 重排配置
    RERANK_MODEL: str = "bge-reranker-large"
    RERANK_TOP_K: int = 50                 # 送入重排的候选数
    RERANK_BATCH_SIZE: int = 16            # 重排批大小
    FINAL_CUT: int = 10                    # 最终返回数量

    # 时间衰减配置
    TIME_DECAY_ENABLED: bool = True
    TIME_DECAY_LAMBDA: float = 0.05        # 衰减系数 (约5%/月)
    TIME_DECAY_MAX_MONTHS: int = 24        # 最大衰减月数

    # 性能约束
    MAX_TOTAL_TIME_MS: int = 1500          # 总耗时限制
    QUERY_TIMEOUT_MS: int = 1000           # 查询超时

    class Config:
        env_file = ".env"


# 全局配置实例
search_config = SearchConfig()


# 原子能力映射（简化示例，实际应查询 ability_library 表）
ABILITY_ATOM_MAP: Dict[int, Dict] = {
    42: {"name": "Go并发编程", "clan_id": 5, "macro_id": 2},
    43: {"name": "Go通道模式", "clan_id": 5, "macro_id": 2},
    145: {"name": "Redis分布式锁", "clan_id": 12, "macro_id": 4},
    203: {"name": "Kubernetes运维", "clan_id": 18, "macro_id": 6},
    301: {"name": "微服务通信", "clan_id": 25, "macro_id": 8},
    405: {"name": "系统架构设计", "clan_id": 32, "macro_id": 10},
    512: {"name": "性能优化", "clan_id": 40, "macro_id": 12},
}


# 层级汇聚权重（1024 -> 128 -> 32）
ROLLUP_WEIGHTS: Dict[int, Dict[int, float]] = {
    # atom_id -> {clan_id -> weight}
    42: {5: 0.8, 6: 0.2},
    43: {5: 0.9},
    145: {12: 0.85, 13: 0.15},
    203: {18: 0.9},
}
