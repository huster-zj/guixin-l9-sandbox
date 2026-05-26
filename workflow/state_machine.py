"""
归心 L9 Agent 工作流状态机与熔断机制

核心设计原则：
1. 每个 Agent 都是独立的状态机，具备明确的输入/输出契约
2. 全局编排器负责任务路由、状态持久化与异常熔断
3. 所有状态转换必须记录审计日志，支持回放与重做
4. 引入降级机制，确保核心链路可用性

Author: HeartCert L9 Team
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta
import asyncio
import json
import uuid


# =============================================================================
# 状态定义
# =============================================================================

class AssessmentState(Enum):
    """全局评估生命周期状态"""
    # 初始化阶段
    CREATED = auto()                    # 评估任务创建

    # Ingestion Agent 阶段
    INGESTION_PENDING = auto()          # 等待简历解析
    INGESTION_PROCESSING = auto()       # DNA提取中
    INGESTION_COMPLETED = auto()        # DNA提取完成
    INGESTION_FAILED = auto()           # DNA提取失败

    # Battlefield Agent 阶段
    BATTLEFIELD_PENDING = auto()        # 等待战场生成
    BATTLEFIELD_PROVISIONING = auto()   # 捏造私有框架中
    BATTLEFIELD_PROVISIONING_TIMEOUT = auto()  # 生成超时，进入降级
    BATTLEFIELD_READY = auto()          # 战场准备就绪
    BATTLEFIELD_FAILED = auto()         # 战场生成失败

    # 考试阶段
    COMBAT_ACTIVE = auto()              # 沙盒对抗中
    COMBAT_PAUSED = auto()              # 考试暂停
    COMBAT_TIMEOUT = auto()             # 考试超时
    COMBAT_ABANDONED = auto()           # 候选人放弃

    # X-RAG Agent 阶段
    XRAG_LISTENING = auto()             # 监听代码Diff
    XRAG_TRIGGERED = auto()             # 触发追问
    XRAG_RESPONDING = auto()            # 生成追问中
    XRAG_CIRCUIT_BREAKER_OPEN = auto()  # 熔断器打开

    # Oracle Judge 阶段
    EVALUATING = auto()                 # 评分中
    EVALUATION_COMPLETED = auto()       # 评分完成
    EVALUATION_FAILED = auto()          # 评分失败

    # 终态
    CERTIFIED = auto()                  # 认证通过
    FAILED = auto()                     # 认证失败


class AgentType(Enum):
    """Agent 类型"""
    INGESTION = "ingestion"
    BATTLEFIELD = "battlefield"
    XRAG = "xrag"
    ORACLE = "oracle"


# =============================================================================
# 数据模型
# =============================================================================

@dataclass
class StateTransition:
    """状态转换记录"""
    transition_id: str
    from_state: AssessmentState
    to_state: AssessmentState
    triggered_by: AgentType
    timestamp: datetime
    context: Dict[str, Any] = field(default_factory=dict)
    evidence_hash: Optional[str] = None


@dataclass
class AgentTask:
    """Agent 任务定义"""
    task_id: str
    agent_type: AgentType
    assessment_id: str
    payload: Dict[str, Any]
    timeout_seconds: int
    max_retries: int = 3
    retry_count: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""
    failure_threshold: int = 5          # 连续失败次数阈值
    recovery_timeout_seconds: int = 60  # 熔断后恢复时间
    half_open_max_calls: int = 3        # 半开状态最大试探调用


class CircuitBreaker:
    """熔断器实现"""

    class State(Enum):
        CLOSED = auto()      # 正常状态
        OPEN = auto()        # 熔断状态
        HALF_OPEN = auto()   # 半开状态

    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.state = self.State.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        # 修复死锁：使用独立锁保护状态读写，不在持有锁时调用业务函数
        self._state_lock = asyncio.Lock()

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """执行受熔断保护的调用"""
        # 检查状态（持有锁的时间尽量短）
        async with self._state_lock:
            if self.state == self.State.OPEN:
                if self._should_attempt_reset():
                    self.state = self.State.HALF_OPEN
                    self.success_count = 0
                else:
                    raise CircuitBreakerOpenError("熔断器打开，拒绝调用")

        # 在锁外执行业务函数，避免死锁
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            await self._on_failure()
            raise e

    async def _on_success(self):
        async with self._state_lock:
            if self.state == self.State.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.half_open_max_calls:
                    self.state = self.State.CLOSED
                    self.failure_count = 0
            else:
                self.failure_count = 0

    async def _on_failure(self):
        async with self._state_lock:
            self.failure_count += 1
            self.last_failure_time = datetime.utcnow()

            if self.state == self.State.HALF_OPEN:
                self.state = self.State.OPEN
            elif self.failure_count >= self.config.failure_threshold:
                self.state = self.State.OPEN

    def _should_attempt_reset(self) -> bool:
        if self.last_failure_time is None:
            return True
        elapsed = (datetime.utcnow() - self.last_failure_time).total_seconds()
        return elapsed >= self.config.recovery_timeout_seconds


class CircuitBreakerOpenError(Exception):
    pass


# =============================================================================
# Agent 状态机基类
# =============================================================================

class AgentStateMachine:
    """Agent 状态机基类"""

    def __init__(
        self,
        agent_type: AgentType,
        circuit_breaker: Optional[CircuitBreaker] = None
    ):
        self.agent_type = agent_type
        self.cb = circuit_breaker
        self.state_handlers: Dict[AssessmentState, Callable] = {}
        self.transitions: List[StateTransition] = []

    def register_handler(
        self,
        state: AssessmentState,
        handler: Callable[[AgentTask], Any]
    ):
        """注册状态处理器"""
        self.state_handlers[state] = handler

    async def execute(self, task: AgentTask) -> Dict[str, Any]:
        """执行任务"""
        handler = self.state_handlers.get(self._get_initial_state())
        if not handler:
            raise StateMachineError(f"未找到初始状态处理器: {self.agent_type}")

        if self.cb:
            return await self.cb.call(handler, task)
        else:
            return await handler(task)

    def _get_initial_state(self) -> AssessmentState:
        """获取初始状态（子类重写）"""
        raise NotImplementedError

    def record_transition(
        self,
        from_state: AssessmentState,
        to_state: AssessmentState,
        context: Dict[str, Any]
    ) -> StateTransition:
        """记录状态转换"""
        transition = StateTransition(
            transition_id=str(uuid.uuid4()),
            from_state=from_state,
            to_state=to_state,
            triggered_by=self.agent_type,
            timestamp=datetime.utcnow(),
            context=context
        )
        self.transitions.append(transition)
        return transition


class StateMachineError(Exception):
    pass


# =============================================================================
# 具体 Agent 状态机实现
# =============================================================================

class IngestionStateMachine(AgentStateMachine):
    """Ingestion Agent 状态机"""

    def __init__(self):
        super().__init__(
            AgentType.INGESTION,
            CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        )
        self.register_handler(
            AssessmentState.INGESTION_PENDING,
            self._handle_pending
        )

    def _get_initial_state(self) -> AssessmentState:
        return AssessmentState.INGESTION_PENDING

    async def _handle_pending(self, task: AgentTask) -> Dict[str, Any]:
        """处理简历解析"""
        self.record_transition(
            AssessmentState.INGESTION_PENDING,
            AssessmentState.INGESTION_PROCESSING,
            {"task_id": task.task_id}
        )

        # 模拟 LLM 调用
        await asyncio.sleep(0.1)

        # 解析结果
        dna_result = {
            "candidate_dna": {
                "dna_version": "1.0",
                "extracted_at": datetime.utcnow().isoformat(),
                "confidence_score": 0.85,
                "atom_activations": [
                    {"atom_id": 42, "activation": 0.88, "name": "Go并发编程"}
                ]
            }
        }

        self.record_transition(
            AssessmentState.INGESTION_PROCESSING,
            AssessmentState.INGESTION_COMPLETED,
            {"dna_confidence": dna_result["candidate_dna"]["confidence_score"]}
        )

        return dna_result


class BattlefieldStateMachine(AgentStateMachine):
    """Battlefield Agent 状态机"""

    PROVISIONING_TIMEOUT_SECONDS = 15  # 战场生成超时阈值

    def __init__(self):
        super().__init__(
            AgentType.BATTLEFIELD,
            CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))
        )
        self.fallback_blueprints: Dict[str, Any] = {}  # 降级备用蓝图
        self.register_handler(
            AssessmentState.BATTLEFIELD_PENDING,
            self._handle_provisioning
        )

    def _get_initial_state(self) -> AssessmentState:
        return AssessmentState.BATTLEFIELD_PENDING

    async def _handle_provisioning(self, task: AgentTask) -> Dict[str, Any]:
        """生成战场场景（带超时降级）"""
        self.record_transition(
            AssessmentState.BATTLEFIELD_PENDING,
            AssessmentState.BATTLEFIELD_PROVISIONING,
            {"task_id": task.task_id}
        )

        try:
            # 带超时的战场生成
            result = await asyncio.wait_for(
                self._generate_blueprint(task),
                timeout=self.PROVISIONING_TIMEOUT_SECONDS
            )

            self.record_transition(
                AssessmentState.BATTLEFIELD_PROVISIONING,
                AssessmentState.BATTLEFIELD_READY,
                {"framework_name": result.get("private_framework", {}).get("framework_name")}
            )

            return result

        except asyncio.TimeoutError:
            # 超时降级
            self.record_transition(
                AssessmentState.BATTLEFIELD_PROVISIONING,
                AssessmentState.BATTLEFIELD_PROVISIONING_TIMEOUT,
                {"timeout_seconds": self.PROVISIONING_TIMEOUT_SECONDS}
            )

            fallback = self._get_fallback_blueprint(task)

            self.record_transition(
                AssessmentState.BATTLEFIELD_PROVISIONING_TIMEOUT,
                AssessmentState.BATTLEFIELD_READY,
                {"fallback_used": True, "framework_name": fallback["private_framework"]["framework_name"]}
            )

            return fallback

    async def _generate_blueprint(self, task: AgentTask) -> Dict[str, Any]:
        """生成私有框架蓝图（Mock）"""
        await asyncio.sleep(0.2)  # 模拟 LLM 调用

        return {
            "battlefield_manifest": {
                "manifest_version": "1.0",
                "estimated_duration_minutes": 45,
                "difficulty_level": "L8.5"
            },
            "private_framework": {
                "framework_name": "HeartRPC",
                "framework_purpose": "归心内部微服务通信框架",
                "documentation": "# HeartRPC 使用文档..."
            },
            "combat_missions": [...]
        }

    def _get_fallback_blueprint(self, task: AgentTask) -> Dict[str, Any]:
        """获取降级备用蓝图"""
        return {
            "battlefield_manifest": {
                "manifest_version": "1.0-fallback",
                "estimated_duration_minutes": 30,
                "difficulty_level": "L7",
                "note": "使用预存静态蓝图（降级模式）"
            },
            "private_framework": {
                "framework_name": "BasicRPC",
                "framework_purpose": "标准RPC框架（静态残卷）",
                "documentation": "# BasicRPC 标准文档..."
            }
        }


class XRAGStateMachine(AgentStateMachine):
    """X-RAG Agent 状态机"""

    DEBOUNCE_SECONDS = 10  # 防抖间隔
    MAX_ROUNDS = 3         # 每题最大追问轮数

    def __init__(self):
        super().__init__(
            AgentType.XRAG,
            CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
        )
        self.last_trigger_time: Optional[datetime] = None
        self.current_round = 0
        self.register_handler(
            AssessmentState.XRAG_LISTENING,
            self._handle_trigger
        )

    def _get_initial_state(self) -> AssessmentState:
        return AssessmentState.XRAG_LISTENING

    async def _handle_trigger(self, task: AgentTask) -> Optional[Dict[str, Any]]:
        """处理 Checkpoint 触发"""
        trigger_event = task.payload.get("trigger_event", {})

        # 1. Debounce 检查
        if not self._should_trigger():
            return None

        # 2. 轮数限制检查
        if self.current_round >= self.MAX_ROUNDS:
            self.record_transition(
                AssessmentState.XRAG_LISTENING,
                AssessmentState.XRAG_CIRCUIT_BREAKER_OPEN,
                {"reason": "max_rounds_reached", "rounds": self.current_round}
            )
            return {"status": "circuit_breaker_open", "reason": "max_rounds_reached"}

        self.current_round += 1
        self.last_trigger_time = datetime.utcnow()

        self.record_transition(
            AssessmentState.XRAG_LISTENING,
            AssessmentState.XRAG_TRIGGERED,
            {
                "checkpoint_id": trigger_event.get("checkpoint_id"),
                "round": self.current_round
            }
        )

        # 3. 生成追问
        injection = await self._generate_injection(task)

        self.record_transition(
            AssessmentState.XRAG_TRIGGERED,
            AssessmentState.XRAG_RESPONDING,
            {"injection_type": injection.get("xrag_response", {}).get("response_type")}
        )

        return injection

    def _should_trigger(self) -> bool:
        """防抖检查"""
        if self.last_trigger_time is None:
            return True
        elapsed = (datetime.utcnow() - self.last_trigger_time).total_seconds()
        return elapsed >= self.DEBOUNCE_SECONDS

    async def _generate_injection(self, task: AgentTask) -> Dict[str, Any]:
        """生成异常注入（Mock）"""
        await asyncio.sleep(0.1)  # 模拟 LLM 调用，目标 <800ms

        return {
            "xrag_response": {
                "response_type": "environment_failure",
                "injection": {
                    "type": "modal_dialog",
                    "severity": "critical",
                    "title": "【异常注入】Redis 主节点失联",
                    "content": "你的服务在运行过程中，Redis 主节点突然宕机..."
                }
            }
        }


class OracleStateMachine(AgentStateMachine):
    """Oracle Judge Agent 状态机"""

    def __init__(self):
        super().__init__(
            AgentType.ORACLE,
            CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))
        )
        self.register_handler(
            AssessmentState.EVALUATING,
            self._handle_evaluation
        )

    def _get_initial_state(self) -> AssessmentState:
        return AssessmentState.EVALUATING

    async def _handle_evaluation(self, task: AgentTask) -> Dict[str, Any]:
        """执行评分裁决"""
        battle_log = task.payload.get("battle_log", {})
        role_schema = task.payload.get("role_schema", {})

        # 生成 1024 维向量
        vector_1024 = self._build_1024_vector(battle_log, role_schema)

        # 生成 JSONB 输出
        verified_skills = self._extract_verified_skills(battle_log)
        reranker_payload = self._generate_reranker_payload(battle_log)

        judgment = {
            "oracle_judgment": {
                "judgment_version": "1.0",
                "judged_at": datetime.utcnow().isoformat(),
                "overall_combat_score": 0.82,
                "confidence": 0.91,
                "vector_output": {
                    "full_1024_vector": vector_1024,
                    "vector_hash": self._hash_vector(vector_1024)
                },
                "jsonb_outputs": {
                    "verified_skills": verified_skills,
                    "reranker_payload": reranker_payload
                }
            }
        }

        self.record_transition(
            AssessmentState.EVALUATING,
            AssessmentState.EVALUATION_COMPLETED,
            {"overall_score": judgment["oracle_judgment"]["overall_combat_score"]}
        )

        return judgment

    def _build_1024_vector(
        self,
        battle_log: Dict,
        role_schema: Dict
    ) -> List[float]:
        """构建 1024 维向量"""
        # 初始化基线值 0.05
        vector = [0.05] * 1024

        target_atoms = role_schema.get("target_atoms", [])
        atom_scores = {42: 0.85, 145: 0.72}  # Mock 评分

        # 目标能力赋值
        for atom_id in target_atoms:
            if atom_id in atom_scores:
                vector[atom_id - 1] = atom_scores[atom_id]

        # 强相关能力平滑（简化实现）
        # 实际实现需要查询 ability_correlation_matrix

        return vector

    def _extract_verified_skills(self, battle_log: Dict) -> List[Dict]:
        """提取验证过的技能"""
        return [
            {"skill": "Golang", "verified_in": "sandbox", "proficiency": "expert"},
            {"skill": "Redis", "verified_in": "sandbox", "proficiency": "proficient"}
        ]

    def _generate_reranker_payload(self, battle_log: Dict) -> str:
        """生成 reranker payload（150词以内）"""
        return (
            "候选人在包含Redis死锁的Go微服务残卷中，准确识别并重构了分布式锁逻辑。"
            "展现了极强的并发控制能力和故障降级思维，耗时12分钟，无内存泄漏。"
        )

    def _hash_vector(self, vector: List[float]) -> str:
        """计算向量哈希"""
        import hashlib
        vector_str = json.dumps(vector, separators=(',', ':'))
        return f"sha256:{hashlib.sha256(vector_str.encode()).hexdigest()[:16]}"


# =============================================================================
# 全局编排器
# =============================================================================

class L9Orchestrator:
    """L9 全流程编排器"""

    def __init__(self):
        self.agents: Dict[AgentType, AgentStateMachine] = {
            AgentType.INGESTION: IngestionStateMachine(),
            AgentType.BATTLEFIELD: BattlefieldStateMachine(),
            AgentType.XRAG: XRAGStateMachine(),
            AgentType.ORACLE: OracleStateMachine()
        }
        self.state_persistence: Dict[str, List[StateTransition]] = {}

    async def run_assessment(
        self,
        assessment_id: str,
        resume_text: str,
        job_cert_id: str
    ) -> Dict[str, Any]:
        """运行完整评估流程"""
        results = {"assessment_id": assessment_id, "stages": {}}

        # Stage 1: Ingestion
        ingestion_task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent_type=AgentType.INGESTION,
            assessment_id=assessment_id,
            payload={"resume_text": resume_text, "job_cert_id": job_cert_id},
            timeout_seconds=30
        )
        dna_result = await self.agents[AgentType.INGESTION].execute(ingestion_task)
        results["stages"]["ingestion"] = dna_result

        # Stage 2: Battlefield
        battlefield_task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent_type=AgentType.BATTLEFIELD,
            assessment_id=assessment_id,
            payload={"candidate_dna": dna_result["candidate_dna"]},
            timeout_seconds=20
        )
        blueprint = await self.agents[AgentType.BATTLEFIELD].execute(battlefield_task)
        results["stages"]["battlefield"] = blueprint

        # Stage 3: Combat + X-RAG 实时对抗
        # 模拟候选人完成第一个任务触发 Checkpoint
        xrag_task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent_type=AgentType.XRAG,
            assessment_id=assessment_id,
            payload={
                "trigger_event": {
                    "type": "test_passed",
                    "timestamp": datetime.utcnow().isoformat(),
                    "checkpoint_id": "M1-implementation-complete"
                },
                "candidate_context": {
                    "session_id": assessment_id,
                    "current_mission": "M1",
                    "code_snapshot": "// candidate code snapshot",
                    "diff_since_last": "+ func handleLock() { ... }",
                    "execution_log": "PASS: TestConcurrentAccess",
                    "time_spent_minutes": 18
                },
                "battlefield_context": blueprint
            },
            timeout_seconds=10
        )
        xrag_result = await self.agents[AgentType.XRAG].execute(xrag_task)
        results["stages"]["combat"] = {
            "status": "completed",
            "missions": 2,
            "xrag_injection": xrag_result
        }

        # Stage 4: Oracle Judge
        oracle_task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent_type=AgentType.ORACLE,
            assessment_id=assessment_id,
            payload={
                "battle_log": {"missions": results["stages"]["combat"]},
                "role_schema": {"target_atoms": [42, 145, 203]}
            },
            timeout_seconds=60
        )
        judgment = await self.agents[AgentType.ORACLE].execute(oracle_task)
        results["stages"]["oracle"] = judgment

        # 持久化状态转换日志
        self._persist_transitions(assessment_id)

        return results

    def _persist_transitions(self, assessment_id: str):
        """持久化所有状态转换记录"""
        all_transitions = []
        for agent in self.agents.values():
            all_transitions.extend(agent.transitions)
        self.state_persistence[assessment_id] = all_transitions


# =============================================================================
# 使用示例
# =============================================================================

async def main():
    """示例：运行完整评估流程"""
    orchestrator = L9Orchestrator()

    result = await orchestrator.run_assessment(
        assessment_id="assess-001",
        resume_text="5年Go后端开发经验，熟悉Redis、Kafka...",
        job_cert_id="job-001"
    )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
