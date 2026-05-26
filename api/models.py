"""
Pydantic 数据模型定义
"""

from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Optional, Any
from datetime import datetime
from enum import Enum
import uuid


# =============================================================================
# 请求模型
# =============================================================================

class FilterConfig(BaseModel):
    """搜索过滤配置"""
    cities: Optional[List[str]] = None
    is_remote_ok: Optional[bool] = None
    min_experience: Optional[int] = Field(None, ge=0, le=50)
    max_salary: Optional[int] = None
    min_level: Optional[str] = Field(None, pattern=r"^L[6-9]$")
    exclude_private_pool: bool = True


class SearchRequest(BaseModel):
    """搜索请求"""
    query: str = Field(..., min_length=1, max_length=500, description="HR自然语言查询")
    filters: FilterConfig = Field(default_factory=FilterConfig)
    session_id: Optional[str] = None

    # 高级配置（可选覆盖）
    enable_l1: bool = True
    enable_l2: bool = True
    enable_l3: bool = True
    enable_sparse: bool = True
    rerank_top_k: int = Field(default=50, ge=10, le=100)


# =============================================================================
# 响应模型
# =============================================================================

class AtomScoreDetail(BaseModel):
    """原子能力得分详情"""
    atom_id: int = Field(..., ge=1, le=1024)
    atom_name: str
    score: float = Field(..., ge=0, le=1)
    evidence: Optional[str] = None


class CandidateMatch(BaseModel):
    """候选人匹配结果"""
    candidate_id: str
    rank: int

    # 分层得分
    score_l1: Optional[float] = None          # 32维粗召回得分
    score_l2: Optional[float] = None          # 128维中召回得分
    score_l3: Optional[float] = None          # 1024维精召回得分
    score_sparse: Optional[float] = None      # 稀疏召回得分
    score_rrf: float                          # RRF融合得分
    score_rerank: Optional[float] = None      # 重排得分
    final_score: float                        # 最终得分

    # 时间衰减
    months_since_certified: Optional[int] = None
    time_decay_factor: Optional[float] = None

    # 匹配详情
    matched_atoms: List[AtomScoreDetail] = Field(default_factory=list)
    verified_skills: List[str] = Field(default_factory=list)
    match_explanation: Optional[str] = None

    # 候选人摘要
    experience_years: Optional[int] = None
    preferred_city: Optional[str] = None
    is_remote_ok: Optional[bool] = None


class SearchMetrics(BaseModel):
    """搜索性能指标"""
    total_time_ms: int
    l1_time_ms: Optional[int] = None
    l2_time_ms: Optional[int] = None
    l3_time_ms: Optional[int] = None
    sparse_time_ms: Optional[int] = None
    rrf_time_ms: int
    rerank_time_ms: Optional[int] = None

    l1_candidates: Optional[int] = None
    l2_candidates: Optional[int] = None
    l3_candidates: Optional[int] = None
    sparse_candidates: Optional[int] = None
    rrf_candidates: int
    rerank_candidates: Optional[int] = None


class SearchResponse(BaseModel):
    """搜索响应"""
    session_id: str
    query: str
    total_results: int
    candidates: List[CandidateMatch]
    metrics: SearchMetrics

    # 解析结果摘要
    parsed_tags: List[str] = Field(default_factory=list)
    target_atoms: List[int] = Field(default_factory=list)

    generated_at: datetime = Field(default_factory=datetime.utcnow)


# =============================================================================
# 内部模型
# =============================================================================

class RecallResult(BaseModel):
    """召回层内部结果"""
    candidate_id: str
    score: float
    rank: int
    source: str                          # "l1", "l2", "l3", "sparse"


class RRFScore(BaseModel):
    """RRF 融合得分"""
    candidate_id: str
    rrf_score: float
    ranks: Dict[str, Optional[int]]      # 各层排名
    scores: Dict[str, Optional[float]]   # 各层得分


class ParsedQuery(BaseModel):
    """Query Parser 解析结果"""
    original_query: str
    extracted_tags: List[str]
    target_atoms: List[int]
    atom_weights: Dict[int, float]
    target_vec_32: List[float]
    target_vec_128: List[float]
    target_vec_1024: List[float]

    @field_validator('target_vec_1024')
    @classmethod
    def validate_vec_1024(cls, v):
        if len(v) != 1024:
            raise ValueError('vec_1024 must have exactly 1024 dimensions')
        return v
