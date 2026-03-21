from __future__ import annotations

from humanoid_fleet.domain import Intent, ResolutionMode, ResolvedExecution, TaskTemplate, WorkflowPlan, WorkflowTask


class HierarchicalOrchestrator:
    DRINK_LABELS = {
        "cappuccino": "卡布奇诺",
        "americano": "美式",
    }
    ITEM_LABELS = {
        "cola": "可乐",
    }

    def build_workflow(self, intent: Intent, resolved: ResolvedExecution) -> WorkflowPlan:
        if resolved.mode == ResolutionMode.SKILL:
            return self._build_skill_direct_workflow(intent, resolved)
        if resolved.mode == ResolutionMode.AGENT:
            return self._build_agent_workflow(intent, resolved)
        template = resolved.template
        if template is None:
            raise ValueError("SOP execution must include a workflow template")
        if intent.intent_type == "pickup_and_deliver_items":
            return self._build_pickup_delivery_workflow(intent, template)

        tasks: list[WorkflowTask] = []
        task_index = 1

        tasks.append(
            WorkflowTask(
                task_id=f"task-{task_index}",
                name="Confirm parsed order with the customer",
                skill_name="speak",
                parameters={"message": self._build_confirmation_message(intent)},
            )
        )
        task_index += 1

        prep_task_ids: list[str] = []
        for drink in intent.drinks:
            current_id = f"task-{task_index}"
            tasks.append(
                WorkflowTask(
                    task_id=current_id,
                    name=f"Prepare {drink.drink_type}",
                    skill_name="operate_coffee_machine",
                    parameters={
                        "drink_type": drink.drink_type,
                        "temperature": drink.temperature,
                        "sugar": drink.sugar,
                        "ice": drink.ice,
                    },
                    depends_on=["task-1"],
                )
            )
            prep_task_ids.append(current_id)
            task_index += 1

        stage_task_id = f"task-{task_index}"
        tasks.append(
            WorkflowTask(
                task_id=stage_task_id,
                name="Stage drinks on tray",
                skill_name="stage_order",
                parameters={"destination": intent.destination, "drink_count": str(len(intent.drinks))},
                depends_on=prep_task_ids,
            )
        )
        task_index += 1

        deliver_task_id = f"task-{task_index}"
        tasks.append(
            WorkflowTask(
                task_id=deliver_task_id,
                name="Deliver tray to customer",
                skill_name="deliver_items",
                parameters={"destination": intent.destination},
                depends_on=[stage_task_id],
            )
        )
        task_index += 1

        tasks.append(
            WorkflowTask(
                task_id=f"task-{task_index}",
                name="Confirm delivery completion",
                skill_name="speak",
                parameters={"message": "您的饮品已送达，请慢用。"},
                depends_on=[deliver_task_id],
            )
        )

        return WorkflowPlan(
            workflow_id="wf-drinks-service-001",
            intent=intent,
            template_id=template.template_id,
            tasks=tasks,
            mode="unassigned",
            explanation=[
                "Matched a known service template for beverage preparation and delivery.",
                "Expanded the task into intent, workflow, and skill layers.",
            ],
        )

    def _build_skill_direct_workflow(self, intent: Intent, resolved: ResolvedExecution) -> WorkflowPlan:
        return WorkflowPlan(
            workflow_id="wf-skill-direct-001",
            intent=intent,
            template_id=resolved.skill_name or "direct_skill",
            tasks=[
                WorkflowTask(
                    task_id="task-1",
                    name="Execute direct skill",
                    skill_name=resolved.skill_name or "unknown_skill",
                    parameters={"destination": intent.destination},
                )
            ],
            mode="unassigned",
            explanation=[resolved.explanation, "Resolved directly to a single skill without workflow expansion."],
        )

    def _build_agent_workflow(self, intent: Intent, resolved: ResolvedExecution) -> WorkflowPlan:
        return WorkflowPlan(
            workflow_id="wf-agent-plan-001",
            intent=intent,
            template_id=resolved.template.template_id,
            tasks=[
                WorkflowTask(
                    task_id="task-1",
                    name="Ask agent planner to decompose request",
                    skill_name="agent_plan",
                    parameters={"goal": intent.raw_text},
                ),
                WorkflowTask(
                    task_id="task-2",
                    name="Validate generated plan",
                    skill_name="validate_plan",
                    parameters={"intent_type": intent.intent_type},
                    depends_on=["task-1"],
                ),
            ],
            mode="unassigned",
            explanation=[resolved.explanation, "Generated a provisional agent workflow because no approved SOP exists."],
        )

    def _build_pickup_delivery_workflow(self, intent: Intent, template: TaskTemplate) -> WorkflowPlan:
        item = intent.items[0]
        tasks = [
            WorkflowTask(
                task_id="task-1",
                name="Confirm pickup order with the customer",
                skill_name="speak",
                parameters={"message": self._build_pickup_confirmation_message(intent)},
            ),
            WorkflowTask(
                task_id="task-2",
                name="Pick up item from service station",
                skill_name="pickup_item",
                parameters={
                    "item_type": item.item_type,
                    "temperature": item.temperature,
                    "source": "service_station",
                },
                depends_on=["task-1"],
            ),
            WorkflowTask(
                task_id="task-3",
                name="Deliver item to destination",
                skill_name="deliver_items",
                parameters={"destination": intent.destination},
                depends_on=["task-2"],
            ),
            WorkflowTask(
                task_id="task-4",
                name="Confirm delivery completion",
                skill_name="speak",
                parameters={"message": "您要的饮品已经送到，请慢用。"},
                depends_on=["task-3"],
            ),
        ]

        return WorkflowPlan(
            workflow_id="wf-pickup-service-001",
            intent=intent,
            template_id=template.template_id,
            tasks=tasks,
            mode="unassigned",
            explanation=[
                "Matched a known pickup-and-delivery template.",
                "Expanded the request into confirmation, pickup, delivery, and completion steps.",
            ],
        )

    @staticmethod
    def _build_confirmation_message(intent: Intent) -> str:
        parts = []
        for drink in intent.drinks:
            item = HierarchicalOrchestrator.DRINK_LABELS.get(drink.drink_type, drink.drink_type)
            if drink.ice == "no_ice":
                item += " 去冰"
            if drink.temperature == "hot":
                item = "热" + item
            if drink.sugar == "no_sugar":
                item += " 不加糖"
            parts.append(item)
        return "我理解的任务是：" + "，".join(parts) + "。现在开始为您处理。"

    @staticmethod
    def _build_pickup_confirmation_message(intent: Intent) -> str:
        item = intent.items[0]
        label = HierarchicalOrchestrator.ITEM_LABELS.get(item.item_type, item.item_type)
        if item.temperature == "cold":
            label = "冰" + label

        destination = "顾客"
        if intent.destination.startswith("table_"):
            destination = intent.destination.replace("table_", "") + "号桌顾客"

        return f"我理解的任务是：取一杯{label}送给{destination}。现在开始为您处理。"
