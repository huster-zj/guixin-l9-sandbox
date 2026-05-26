# Harness Engineering 设计说明

## 一、什么是 Harness Engineering

Harness Engineering（束具工程）是指构建一套严格受控的框架，将大模型能力"束缚"在可预测、可审计、可回滚的范围内。

核心原则：
1. **强类型输出**: 不接收自然语言，只接收 JSON Schema
2. **状态机驱动**: 所有流转必须经过预定义的状态
3. **熔断降级**: 异常时自动切换到安全模式
4. **审计追踪**: 每一步都可追溯、可回放

---

## 二、Agent 工作流架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      L9Orchestrator (编排器)                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐ │
│   │ 状态机调度   │───→│ 熔断器检查   │───→│ Agent 执行          │ │
│   │             │    │             │    │ (带超时控制)         │ │
│   └─────────────┘    └─────────────┘    └─────────────────────┘ │
│                                                  │               │
│                                                  ↓               │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐ │
│   │ 状态持久化   │←───│ 输出校验    │←───│ 结果标准化          │ │
│   │             │    │ (JSON Schema)│    │                     │ │
│   └─────────────┘    └─────────────┘    └─────────────────────┘ │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、关键设计模式

### 1. Circuit Breaker (熔断器)

```python
class CircuitBreaker:
    """熔断器实现"""
    
    # 三种状态
    CLOSED   → 正常状态，允许调用
    OPEN     → 熔断状态，拒绝调用
    HALF_OPEN → 半开状态，试探性调用
    
    # 触发条件
    - 连续失败次数 > failure_threshold
    - 自动恢复时间 > recovery_timeout
    
    # 应用场景
    - LLM API 超时/错误过多时熔断
    - 数据库连接异常时熔断
```

### 2. State Machine (状态机)

```python
class AssessmentState(Enum):
    # 明确定义所有合法状态
    CREATED → INGESTION_PROCESSING → INGESTION_COMPLETED
    → BATTLEFIELD_PROVISIONING → BATTLEFIELD_READY
    → COMBAT_ACTIVE → EVALUATING → CERTIFIED/FAILED
    
# 非法跳转会被自动拦截
# 每个状态转换记录审计日志
```

### 3. Debounce (防抖)

```python
# X-RAG Agent 中使用
DEBOUNCE_SECONDS = 10

# 同一 Checkpoint 10秒内只触发一次追问
# 防止代码频繁保存导致追问泛滥
```

### 4. Fallback (降级)

```python
# Battlefield Agent 中使用
PROVISIONING_TIMEOUT_SECONDS = 15

# 如果战场生成超时，自动降级到预存静态蓝图
# 确保候选人体验不断档
```

---

## 四、异常熔断机制详解

### 4.1 熔断级别

| 级别 | 触发条件 | 恢复策略 |
|------|---------|---------|
| L1-Agent | 单 Agent 连续失败 | 1分钟后半开试探 |
| L2-Global | 全局错误率 > 20% | 全量降级到静态模式 |
| L3-System | 依赖服务宕机 | 切换到灾备集群 |

### 4.2 降级策略矩阵

```
场景                          降级策略
─────────────────────────────────────────────────────────
Ingestion Agent 超时           使用简历关键词简单匹配
Battlefield Agent 超时         使用 Fallback 静态蓝图
X-RAG Agent 熔断              关闭实时追问，仅记录日志
Oracle Judge 超时             人工介入标记，延迟评分
向量数据库超时                 降级到纯 BM25 文本检索
```

---

## 五、审计与回放

### 5.1 状态转换日志

```json
{
  "transition_id": "txn-uuid",
  "assessment_id": "assess-001",
  "from_state": "COMBAT_ACTIVE",
  "to_state": "EVALUATING",
  "triggered_by": "oracle_agent",
  "timestamp": "2024-01-15T10:30:00Z",
  "context": {
    "missions_completed": 3,
    "total_time_minutes": 42
  },
  "evidence_hash": "sha256:abc123..."
}
```

### 5.2 重做机制

```python
# 重做某次 Assessment
async def replay_assessment(assessment_id: str):
    # 1. 查询历史状态转换记录
    transitions = await get_transitions(assessment_id)
    
    # 2. 旧版本标记为 inactive
    await deactivate_old_contributions(assessment_id)
    
    # 3. 重新执行
    new_result = await orchestrator.run_assessment(...)
    
    # 4. 新版本激活
    await activate_new_contributions(new_result)
    
    # 5. 重新聚合候选人画像
    await recompute_candidate_snapshot(candidate_id)
```

---

## 六、性能约束

| 组件 | 约束 | 超限处理 |
|------|------|---------|
| Query Parser | < 500ms | 使用缓存结果 |
| Battlefield Gen | < 15s | 降级到静态蓝图 |
| X-RAG Trigger | < 800ms | 跳过本次追问 |
| Oracle Judge | < 30s | 人工复核队列 |
| 向量搜索 (L1) | < 100ms | 限流保护 |
| RRF Fusion | < 50ms | 内存计算 |
| Cross-Encoder | < 500ms | 批处理优化 |
| 总搜索延迟 | < 1500ms | 提前截断返回 |

---

## 七、验收标准

1. **可审计**: 任一候选人的任一能力值，都能追溯到具体 assessment 和具体题目
2. **可重做**: 同一 assessment 重做后，不会重复累计旧贡献
3. **可熔断**: 任何外部依赖故障时，系统能自动降级继续服务
4. **可回滚**: 发布新版本后，能在5分钟内回滚到旧版本
