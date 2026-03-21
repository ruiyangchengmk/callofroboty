from __future__ import annotations

from dataclasses import asdict
from typing import Any

from humanoid_fleet.bootstrap import build_demo_robots
from humanoid_fleet.memory import TaskMemory
from humanoid_fleet.orchestrator import HierarchicalOrchestrator
from humanoid_fleet.resolution import ExecutionResolver
from humanoid_fleet.runtime import SkillRuntime
from humanoid_fleet.scheduler import Scheduler
from humanoid_fleet.domain import MediaAttachment, UserInput
from humanoid_fleet.understanding import HybridTaskInterpreter


class FleetControlApp:
    def __init__(self) -> None:
        self.interpreter = HybridTaskInterpreter()
        self.memory = TaskMemory()
        self.resolver = ExecutionResolver(self.memory)
        self.orchestrator = HierarchicalOrchestrator()
        self.scheduler = Scheduler()
        self.runtime = SkillRuntime()

    def plan_task(self, task_text: str, attachments: list[dict[str, str]] | None = None) -> dict[str, Any]:
        robots = build_demo_robots()
        user_input = UserInput(
            text=task_text,
            attachments=[
                MediaAttachment(
                    media_type=item["media_type"],
                    uri=item["uri"],
                    description=item.get("description", ""),
                )
                for item in (attachments or [])
            ],
        )

        intent = self.interpreter.parse(user_input)
        resolved = self.resolver.resolve(intent)
        workflow = self.orchestrator.build_workflow(intent, resolved)
        scheduled_workflow = self.scheduler.assign(workflow, robots)
        execution_plan = self.runtime.create_execution_plan(scheduled_workflow)

        return {
            "input": task_text,
            "attachments": [asdict(item) for item in user_input.attachments],
            "robots": [asdict(robot) for robot in robots],
            "template_catalog": [asdict(template) for template in self.memory.templates],
            "understanding_prompt": self.interpreter.preview_prompt(user_input),
            "intent": asdict(intent),
            "resolution": {
                "mode": resolved.mode.value,
                "skill_name": resolved.skill_name,
                "explanation": resolved.explanation,
            },
            "template": asdict(resolved.template) if resolved.template is not None else None,
            "workflow": asdict(scheduled_workflow),
            "execution_plan": asdict(execution_plan),
        }
