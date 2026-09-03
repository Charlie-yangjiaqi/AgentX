"""AgentX Runtime：解释层 + 观察层。

- context.py ：RuntimeContext（Index 状态 / 指纹 / 决策解释 / workflow 阶段）
- events.py  ：EventCollector（结构化 workflow 事件，业务逻辑直接 emit，不解析文本）

设计原则：
- RuntimeContext 只解释，不改变 Index 状态机
- EventStream 只观察，不影响业务流程
"""

from agentx.runtime.context import RuntimeContext, build_runtime_context, decide_index_action
from agentx.runtime.events import (
    DEFAULT_HEARTBEAT_INTERVAL,
    STAGES,
    EventCollector,
    Heartbeat,
    WorkflowEvent,
)
from agentx.runtime.progress import (
    STAGE_PROGRESS,
    TOTAL_PROGRESS,
    ProgressAdapter,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "EventCollector",
    "Heartbeat",
    "ProgressAdapter",
    "RuntimeContext",
    "STAGES",
    "STAGE_PROGRESS",
    "TOTAL_PROGRESS",
    "WorkflowEvent",
    "build_runtime_context",
    "decide_index_action",
]
