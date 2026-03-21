from __future__ import annotations

from humanoid_fleet.domain import ExecutionPlan, SkillExecution, WorkflowPlan


class SkillRuntime:
    def create_execution_plan(self, workflow: WorkflowPlan) -> ExecutionPlan:
        executions = [
            SkillExecution(
                task_id=task.task_id,
                robot_id=task.assigned_robot_id or "unassigned",
                skill_name=task.skill_name,
                parameters=task.parameters,
            )
            for task in workflow.tasks
        ]

        return ExecutionPlan(
            workflow_id=workflow.workflow_id,
            executions=executions,
            user_summary=self._build_user_summary(workflow),
            operator_summary=self._build_operator_summary(workflow),
        )

    @staticmethod
    def _build_user_summary(workflow: WorkflowPlan) -> str:
        if workflow.mode == "multi_robot":
            return "已安排多台机器人协作处理您的订单，并会持续同步进度。"
        return "已安排机器人开始处理您的订单，并会持续同步进度。"

    @staticmethod
    def _build_operator_summary(workflow: WorkflowPlan) -> str:
        lines = [
            f"workflow={workflow.workflow_id}",
            f"template={workflow.template_id}",
            f"mode={workflow.mode}",
        ]
        lines.extend(workflow.explanation)
        return " | ".join(lines)
