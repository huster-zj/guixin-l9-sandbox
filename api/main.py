"""
归心 L9 搜索管线 FastAPI 入口

启动命令：
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

测试命令：
    curl -X POST "http://localhost:8000/search" \
      -H "Content-Type: application/json" \
      -d '{"query": "需要一个能在高并发下处理 Redis 分布式死锁的后端，不限语言"}'
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from models import SearchRequest, SearchResponse
from search_pipeline import search_pipeline
from config import search_config


# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期事件"""
    print(f"🚀 启动 {search_config.APP_NAME}")
    print(f"📊 配置: L1={search_config.L1_COARSE_LIMIT}, L2={search_config.L2_MEDIUM_LIMIT}, L3={search_config.L3_FINE_LIMIT}")
    print(f"🔧 RRF: k={search_config.RRF_K}, weights=[{search_config.RRF_WEIGHT_32}, {search_config.RRF_WEIGHT_128}, {search_config.RRF_WEIGHT_1024}, {search_config.RRF_WEIGHT_SPARSE}]")
    yield
    print("👋 关闭服务")


# 创建 FastAPI 应用
app = FastAPI(
    title="归心 L9 搜索管线",
    description="B端工业级混合检索管线 - 分层召回 + RRF融合 + Cross-Encoder重排",
    version="1.0.0",
    lifespan=lifespan
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# API 端点
# =============================================================================

@app.get("/")
async def root():
    """根路径 - 服务健康检查"""
    return {
        "service": "归心 L9 搜索管线",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
        "endpoints": {
            "health": "/health",
            "search": "POST /search",
            "config": "/config"
        }
    }


@app.get("/api")
async def api_root():
    """API 根路径"""
    return {
        "service": "归心 L9 搜索管线 API",
        "version": "1.0.0",
        "description": "B端工业级混合检索管线",
        "endpoints": [
            {"path": "/health", "method": "GET", "description": "健康检查"},
            {"path": "/search", "method": "POST", "description": "候选人搜索"},
            {"path": "/search/debug", "method": "POST", "description": "调试模式搜索"},
            {"path": "/config", "method": "GET", "description": "获取配置"},
            {"path": "/config/validate", "method": "POST", "description": "验证配置"},
        ],
        "docs_url": "/docs",
        "openapi_url": "/openapi.json"
    }


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {
        "status": "healthy",
        "timestamp": "2024-01-15T10:00:00Z"
    }


@app.post("/search", response_model=SearchResponse)
async def search_candidates(request: SearchRequest):
    """
    执行候选人搜索

    完整链路：
    1. Query Parser 解析自然语言查询
    2. Filter Gate 硬过滤
    3. 分层召回 (L1 32维 → L2 128维 → L3 1024维 + 稀疏)
    4. RRF 倒数秩融合
    5. 时间衰减调整
    6. Cross-Encoder 重排
    7. 返回 Top 10 结果
    """
    try:
        response = await search_pipeline.search(request)
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")


@app.post("/search/debug")
async def search_debug(request: SearchRequest):
    """
    调试模式搜索 - 返回各阶段中间结果
    """
    import time
    start = time.time()

    # 执行搜索
    response = await search_pipeline.search(request)

    # 添加调试信息
    return {
        "response": response,
        "debug": {
            "original_query": request.query,
            "filters_applied": request.filters.model_dump(),
            "execution_trace": response.metrics.model_dump(),
            "total_time_ms": response.metrics.total_time_ms
        }
    }


@app.get("/config")
async def get_config():
    """获取当前搜索配置"""
    return {
        "recall": {
            "l1_coarse": {
                "dimension": search_config.L1_COARSE_DIMENSION,
                "limit": search_config.L1_COARSE_LIMIT,
                "min_similarity": search_config.L1_MIN_SIMILARITY
            },
            "l2_medium": {
                "dimension": search_config.L2_MEDIUM_DIMENSION,
                "limit": search_config.L2_MEDIUM_LIMIT,
                "min_similarity": search_config.L2_MIN_SIMILARITY
            },
            "l3_fine": {
                "dimension": search_config.L3_FINE_DIMENSION,
                "limit": search_config.L3_FINE_LIMIT,
                "min_similarity": search_config.L3_MIN_SIMILARITY
            },
            "sparse": {
                "limit": search_config.SPARSE_LIMIT,
                "min_score": search_config.SPARSE_MIN_SCORE
            }
        },
        "fusion": {
            "rrf_k": search_config.RRF_K,
            "rrf_top_k": search_config.RRF_TOP_K,
            "weights": {
                "l1_32d": search_config.RRF_WEIGHT_32,
                "l2_128d": search_config.RRF_WEIGHT_128,
                "l3_1024d": search_config.RRF_WEIGHT_1024,
                "sparse": search_config.RRF_WEIGHT_SPARSE
            }
        },
        "rerank": {
            "model": search_config.RERANK_MODEL,
            "top_k": search_config.RERANK_TOP_K,
            "batch_size": search_config.RERANK_BATCH_SIZE,
            "final_cut": search_config.FINAL_CUT
        },
        "decay": {
            "enabled": search_config.TIME_DECAY_ENABLED,
            "lambda": search_config.TIME_DECAY_LAMBDA,
            "max_months": search_config.TIME_DECAY_MAX_MONTHS
        },
        "constraints": {
            "max_total_time_ms": search_config.MAX_TOTAL_TIME_MS
        }
    }


@app.post("/config/validate")
async def validate_config():
    """验证配置合法性"""
    errors = []
    warnings = []

    # 检查权重和是否为1
    weight_sum = (
        search_config.RRF_WEIGHT_32 +
        search_config.RRF_WEIGHT_128 +
        search_config.RRF_WEIGHT_1024 +
        search_config.RRF_WEIGHT_SPARSE
    )
    if abs(weight_sum - 1.0) > 0.01:
        warnings.append(f"RRF权重和为{weight_sum:.2f}，建议调整为1.0")

    # 检查层级限制合理性
    if search_config.L1_COARSE_LIMIT < search_config.L2_MEDIUM_LIMIT:
        errors.append("L1限制应大于L2限制")
    if search_config.L2_MEDIUM_LIMIT < search_config.L3_FINE_LIMIT:
        errors.append("L2限制应大于L3限制")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }


# =============================================================================
# 示例数据端点（用于测试）
# =============================================================================

@app.get("/examples/search-queries")
async def get_example_queries():
    """获取示例搜索查询"""
    return {
        "examples": [
            {
                "query": "需要一个能在高并发下处理 Redis 分布式死锁的后端，不限语言",
                "expected_tags": ["高并发", "Redis", "死锁", "后端"],
                "expected_atoms": [42, 145, 301]
            },
            {
                "query": "招一个PyTorch深度学习工程师，有模型优化经验",
                "expected_tags": ["PyTorch", "深度学习", "模型优化"],
                "expected_atoms": [101, 102, 201]
            },
            {
                "query": "找Java微服务架构师，5年以上经验，最好有金融背景",
                "expected_tags": ["Java", "微服务", "架构"],
                "expected_atoms": [301, 405]
            }
        ]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
