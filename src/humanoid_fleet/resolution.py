from __future__ import annotations

from humanoid_fleet.domain import Intent, ResolvedExecution, ResolutionMode, TaskTemplate
from humanoid_fleet.memory import TaskMemory


class ExecutionResolver:
    """
    Resolve task execution in the preferred order:
    1. Existing SOP/workflow template
    2. Direct skill invocation
    3. Agentic decomposition
    """

    def __init__(self, task_memory: TaskMemory) -> None:
        self.task_memory = task_memory

    def resolve(self, intent: Intent) -> ResolvedExecution:
        template = self.task_memory.find_template(intent)
        if template is not None:
            return ResolvedExecution(
                mode=ResolutionMode.SOP,
                template=template,
                explanation=f"Matched existing SOP workflow template: {template.template_id}.",
            )

        skill_name = self._resolve_direct_skill(intent)
        if skill_name is not None:
            return ResolvedExecution(
                mode=ResolutionMode.SKILL,
                template=None,
                skill_name=skill_name,
                explanation=f"No SOP matched, but a direct skill can handle the request: {skill_name}.",
            )

        return ResolvedExecution(
            mode=ResolutionMode.AGENT,
            template=self._build_agent_template(intent),
            explanation="No SOP or direct skill matched, falling back to agentic task decomposition.",
        )

    @staticmethod
    def _resolve_direct_skill(intent: Intent) -> str | None:
        if intent.intent_type == "pickup_and_deliver_items" and len(intent.items) == 1 and intent.destination == "customer":
            return "deliver_items"
        return None

    @staticmethod
    def _build_agent_template(intent: Intent) -> TaskTemplate:
        return TaskTemplate(
            template_id=f"agent_generated_{intent.intent_type}",
            name="Agent generated workflow",
            intent_type=intent.intent_type,
            workflow_steps=["understand_goal", "decompose_task", "validate_plan", "execute_plan"],
            supports_multi_robot=True,
        )
