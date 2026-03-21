import unittest

from humanoid_fleet.bootstrap import build_demo_robots
from humanoid_fleet.domain import MediaAttachment, UserInput
from humanoid_fleet.app import FleetControlApp
from humanoid_fleet.understanding import HybridTaskInterpreter


class DemoFlowTest(unittest.TestCase):
    def test_beverage_flow_uses_template_and_multi_robot_schedule(self) -> None:
        result = FleetControlApp().plan_task("去给顾客制作一杯卡布奇诺去冰，一杯热美式不加糖")
        self.assertEqual(result["resolution"]["mode"], "sop")
        self.assertEqual(result["template"]["template_id"], "tmpl_prepare_deliver_drinks")
        self.assertEqual(result["workflow"]["mode"], "multi_robot")
        self.assertTrue(result["execution_plan"]["executions"][0]["parameters"]["message"].startswith("我理解的任务是：卡布奇诺 去冰"))

    def test_pickup_delivery_flow_supports_table_service(self) -> None:
        result = FleetControlApp().plan_task("去拿杯冰可乐给3号桌顾客")
        self.assertEqual(result["intent"]["intent_type"], "pickup_and_deliver_items")
        self.assertEqual(result["intent"]["destination"], "table_3")
        self.assertEqual(result["resolution"]["mode"], "sop")
        self.assertEqual(result["template"]["template_id"], "tmpl_pickup_deliver_items")
        self.assertEqual(result["workflow"]["mode"], "single_robot")
        self.assertEqual(result["execution_plan"]["executions"][1]["skill_name"], "pickup_item")

    def test_direct_skill_resolution_is_used_before_agent(self) -> None:
        result = FleetControlApp().plan_task("把可乐送给顾客")
        self.assertEqual(result["resolution"]["mode"], "skill")
        self.assertEqual(result["resolution"]["skill_name"], "deliver_items")

    def test_multimodal_input_is_accepted_by_understanding_layer(self) -> None:
        interpreter = HybridTaskInterpreter()
        user_input = UserInput(
            text="把这瓶饮料送给3号桌顾客",
            attachments=[
                MediaAttachment(
                    media_type="image",
                    uri="file:///tmp/drink.jpg",
                    description="A chilled bottle of cola on the front desk",
                )
            ],
        )

        with self.assertRaises(ValueError):
            interpreter.fallback.parse(user_input)

        prompt = interpreter.preview_prompt(user_input)
        self.assertIn("Attachments:", prompt)
        self.assertIn("file:///tmp/drink.jpg", prompt)


if __name__ == "__main__":
    unittest.main()
