"""
搜索功能测试脚本
"""

import requests
import json

BASE_URL = "http://localhost:8000"

def test_root():
    """测试根路径"""
    print("=" * 60)
    print("测试 1: 访问根路径 /")
    print("-" * 60)

    try:
        response = requests.get(f"{BASE_URL}/")
        print(f"状态码: {response.status_code}")
        print(f"响应: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
        return response.status_code == 200
    except Exception as e:
        print(f"错误: {e}")
        return False

def test_api():
    """测试 API 目录"""
    print()
    print("=" * 60)
    print("测试 2: 访问 API 目录 /api")
    print("-" * 60)

    try:
        response = requests.get(f"{BASE_URL}/api")
        print(f"状态码: {response.status_code}")
        data = response.json()
        print(f"服务: {data.get('service')}")
        print(f"版本: {data.get('version')}")
        print(f"可用端点: {len(data.get('endpoints', []))} 个")
        for ep in data.get('endpoints', [])[:3]:
            print(f"  - {ep['method']} {ep['path']}: {ep['description']}")
        return True
    except Exception as e:
        print(f"错误: {e}")
        return False

def test_search():
    """测试搜索功能"""
    print()
    print("=" * 60)
    print("测试 3: 执行搜索 /search")
    print("-" * 60)

    query = "需要一个能在高并发下处理 Redis 分布式死锁的后端，不限语言"

    try:
        response = requests.post(
            f"{BASE_URL}/search",
            headers={"Content-Type": "application/json"},
            json={"query": query}
        )
        print(f"状态码: {response.status_code}")

        data = response.json()
        print(f"Session ID: {data.get('session_id')}")
        print(f"解析标签: {data.get('parsed_tags')}")
        print(f"目标能力: {data.get('target_atoms')}")
        print()

        # 性能指标
        metrics = data.get('metrics', {})
        print("性能指标:")
        print(f"  总耗时: {metrics.get('total_time_ms')}ms")
        print(f"  L1 召回: {metrics.get('l1_candidates')} 人 ({metrics.get('l1_time_ms')}ms)")
        print(f"  L2 召回: {metrics.get('l2_candidates')} 人 ({metrics.get('l2_time_ms')}ms)")
        print(f"  L3 召回: {metrics.get('l3_candidates')} 人 ({metrics.get('l3_time_ms')}ms)")
        print(f"  稀疏召回: {metrics.get('sparse_candidates')} 人")
        print(f"  RRF 融合: {metrics.get('rrf_candidates')} 人 ({metrics.get('rrf_time_ms')}ms)")
        print(f"  重排: {metrics.get('rerank_candidates')} 人 ({metrics.get('rerank_time_ms')}ms)")
        print()

        # 候选人结果
        candidates = data.get('candidates', [])
        print(f"返回候选人: {len(candidates)} 人")
        print()

        for i, cand in enumerate(candidates[:3], 1):
            print(f"【Top {i}】")
            print(f"  ID: {cand.get('candidate_id')}")
            print(f"  最终得分: {cand.get('final_score')}")
            print(f"  RRF 得分: {cand.get('score_rrf')}")
            print(f"  重排得分: {cand.get('score_rerank')}")
            print(f"  经验: {cand.get('experience_years')} 年")
            print(f"  城市: {cand.get('preferred_city')}")
            print(f"  技能: {', '.join(cand.get('verified_skills', [])[:3])}")
            print(f"  匹配说明: {cand.get('match_explanation')}")
            print()

        return len(candidates) > 0

    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_config():
    """测试配置端点"""
    print()
    print("=" * 60)
    print("测试 4: 获取配置 /config")
    print("-" * 60)

    try:
        response = requests.get(f"{BASE_URL}/config")
        data = response.json()

        recall = data.get('recall', {})
        print("分层召回配置:")
        print(f"  L1 (32维): limit={recall.get('l1_coarse', {}).get('limit')}")
        print(f"  L2 (128维): limit={recall.get('l2_medium', {}).get('limit')}")
        print(f"  L3 (1024维): limit={recall.get('l3_fine', {}).get('limit')}")

        fusion = data.get('fusion', {})
        print(f"\nRRF 配置:")
        print(f"  k={fusion.get('rrf_k')}")
        print(f"  weights={fusion.get('weights')}")

        return True
    except Exception as e:
        print(f"错误: {e}")
        return False

def main():
    """主函数"""
    print()
    print("归心 L9 搜索管线测试")
    print("=" * 60)
    print(f"目标地址: {BASE_URL}")
    print()

    # 等待服务启动
    import time
    time.sleep(1)

    results = []
    results.append(("根路径", test_root()))
    results.append(("API 目录", test_api()))
    results.append(("搜索功能", test_search()))
    results.append(("配置获取", test_config()))

    print()
    print("=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status} {name}")

    passed_count = sum(1 for _, p in results if p)
    print(f"\n总计: {passed_count}/{len(results)} 通过")

if __name__ == "__main__":
    main()
