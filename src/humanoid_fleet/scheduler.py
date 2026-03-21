from __future__ import annotations

from humanoid_fleet.domain import RobotCapability, RobotStatus, WorkflowPlan


class Scheduler:
    def assign(self, workflow: WorkflowPlan, robots: list[RobotCapability]) -> WorkflowPlan:
        available = [robot for robot in robots if robot.status != RobotStatus.OFFLINE]
        if len(available) < 1:
            raise RuntimeError("No robots available for scheduling")

        if workflow.intent.intent_type == "pickup_and_deliver_items":
            courier = self._find_robot(available, "pickup_item") or self._find_robot(available, "deliver_items") or available[0]
            speaker = self._find_robot(available, "speak") or courier
            for task in workflow.tasks:
                if task.skill_name == "speak":
                    task.assigned_robot_id = speaker.robot_id
                else:
                    task.assigned_robot_id = courier.robot_id
            workflow.mode = "single_robot"
            workflow.explanation.append(
                "Selected single-robot mode for a short pickup-and-delivery request."
            )
            return workflow

        barista = self._find_robot(available, "operate_coffee_machine") or available[0]
        runner = self._find_robot(available, "deliver_items") or barista
        greeter = self._find_robot(available, "speak") or runner

        use_multi_robot = (
            len(available) >= 2
            and barista.robot_id != runner.robot_id
            and len(workflow.intent.drinks) >= 2
        )

        for task in workflow.tasks:
            if task.skill_name == "speak":
                task.assigned_robot_id = greeter.robot_id if use_multi_robot else barista.robot_id
            elif task.skill_name in {"operate_coffee_machine", "stage_order"}:
                task.assigned_robot_id = barista.robot_id
            elif task.skill_name == "deliver_items":
                task.assigned_robot_id = runner.robot_id if use_multi_robot else barista.robot_id
            else:
                task.assigned_robot_id = barista.robot_id

        workflow.mode = "multi_robot" if use_multi_robot else "single_robot"
        workflow.explanation.append(
            "Selected multi-robot mode to parallelize beverage preparation and delivery."
            if use_multi_robot
            else "Selected single-robot mode because one robot can complete the task efficiently."
        )
        return workflow

    @staticmethod
    def _find_robot(robots: list[RobotCapability], skill_name: str) -> RobotCapability | None:
        for robot in robots:
            if skill_name in robot.skill_names and robot.status == RobotStatus.IDLE:
                return robot
        return None
