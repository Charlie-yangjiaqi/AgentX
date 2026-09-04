"""Human Project Knowledge（Phase 8.3）：把 AgentX 已建立的工程认知整理成
工程师可读、可交接、可审查的项目知识文档。

唯一事实源是 Project Index（index.json + module_responsibilities.json）；
Human Index 只是其人类可读表达层，Markdown 不成为独立事实来源。

目录：<project>_codebase_index/human/{PROJECT_OVERVIEW,ARCHITECTURE,MODULES}.md + manifest.json
"""

from agentx.human.bundle import HumanKnowledgeBundle, collect_bundle
from agentx.human.service import HumanKnowledgeService

__all__ = [
    "HumanKnowledgeBundle",
    "HumanKnowledgeService",
    "collect_bundle",
]
