from __future__ import annotations

from humanoid_fleet.domain import Intent, TaskTemplate


class TaskMemory:
    def __init__(self) -> None:
        self.templates = [
            TaskTemplate(
                template_id="tmpl_prepare_deliver_drinks",
                name="Prepare and deliver drinks",
                intent_type="prepare_and_deliver_drinks",
                workflow_steps=[
                    "confirm_order",
                    "prepare_drink",
                    "prepare_drink",
                    "stage_order",
                    "deliver_order",
                    "confirm_completion",
                ],
            )
            ,
            TaskTemplate(
                template_id="tmpl_pickup_deliver_items",
                name="Pick up and deliver items",
                intent_type="pickup_and_deliver_items",
                workflow_steps=[
                    "confirm_order",
                    "pickup_item",
                    "deliver_order",
                    "confirm_completion",
                ],
            ),
        ]

    def match_template(self, intent: Intent) -> TaskTemplate:
        for template in self.templates:
            if template.intent_type == intent.intent_type:
                return template
        raise LookupError(f"No template matched intent type: {intent.intent_type}")

    def find_template(self, intent: Intent) -> TaskTemplate | None:
        for template in self.templates:
            if template.intent_type != intent.intent_type:
                continue
            if template.intent_type == "pickup_and_deliver_items":
                raw = intent.raw_text
                if not any(keyword in raw for keyword in ("拿", "取", "端", "取来", "拿来")):
                    continue
            return template
        return None
