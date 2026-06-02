import io
import json
import unittest
from urllib.parse import urlencode

from humanoid_fleet.bootstrap import build_demo_robots
from humanoid_fleet.domain import MediaAttachment, UserInput
from humanoid_fleet.app import FleetControlApp, _looks_like_confirmation
from humanoid_fleet.understanding import HybridTaskInterpreter, OllamaModelClient
from humanoid_fleet.web import application


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

    def test_conversation_waits_for_confirmation_after_sop_match(self) -> None:
        review = FleetControlApp().review_conversation(
            [{"role": "user", "content": "去拿杯冰可乐给3号桌顾客"}]
        )
        self.assertEqual(review["state"], "awaiting_confirmation")
        self.assertEqual(review["resolution"]["mode"], "sop")
        self.assertIn("确认后", review["assistant_message"])
        self.assertEqual(review["workflow"]["tasks"][1]["skill_name"], "pickup_item")
        self.assertEqual(review["workflow"]["tasks"][2]["skill_name"], "deliver_items")

    def test_web_shows_workflow_state_graph_after_sop_match(self) -> None:
        body = urlencode(
            {
                "history": "[]",
                "action": "send",
                "message": "去拿杯冰可乐给3号桌顾客",
            }
        ).encode("utf-8")
        status_holder = {}

        def start_response(status, headers):
            status_holder["status"] = status

        response = b"".join(
            application(
                {
                    "REQUEST_METHOD": "POST",
                    "PATH_INFO": "/",
                    "QUERY_STRING": "",
                    "CONTENT_LENGTH": str(len(body)),
                    "wsgi.input": io.BytesIO(body),
                },
                start_response,
            )
        ).decode("utf-8")

        self.assertEqual(status_holder["status"], "200 OK")
        self.assertIn("原子技能状态图", response)
        self.assertIn('data-workflow-id="wf-pickup-service-001"', response)
        self.assertIn("pickup_item", response)
        self.assertIn("deliver_items", response)

    def test_web_chat_confirmation_generates_execution_plan(self) -> None:
        history = [
            {"role": "user", "content": "去拿杯冰可乐给3号桌顾客"},
            {"role": "assistant", "content": "我已命中「Pick up and deliver items」。请确认。"},
        ]
        body = urlencode(
            {
                "history": json.dumps(history, ensure_ascii=False),
                "action": "send",
                "message": "我说我确认你开始执行吧",
            }
        ).encode("utf-8")
        status_holder = {}

        def start_response(status, headers):
            status_holder["status"] = status

        response = b"".join(
            application(
                {
                    "REQUEST_METHOD": "POST",
                    "PATH_INFO": "/",
                    "QUERY_STRING": "",
                    "CONTENT_LENGTH": str(len(body)),
                    "wsgi.input": io.BytesIO(body),
                },
                start_response,
            )
        ).decode("utf-8")

        self.assertEqual(status_holder["status"], "200 OK")
        self.assertIn("已确认。我已经生成机器人分工和执行计划。", response)
        self.assertIn("执行计划已生成", response)
        self.assertIn("原子技能状态图", response)
        self.assertNotIn("Routing Ladder", response)
        self.assertNotIn("Understanding Prompt", response)
        self.assertNotIn("已命中 SOP，等待用户确认", response)

    def test_confirmation_detection_ignores_questions(self) -> None:
        self.assertTrue(_looks_like_confirmation("我说我确认你开始执行吧"))
        self.assertFalse(_looks_like_confirmation("你确认什么？"))

    def test_conversation_does_not_treat_meta_question_as_task_detail(self) -> None:
        review = FleetControlApp().review_conversation(
            [
                {"role": "user", "content": "说话"},
                {"role": "assistant", "content": "我还没有足够信息命中现有 SOP。"},
                {"role": "user", "content": "你命中什么了？"},
            ]
        )
        self.assertEqual(review["state"], "chatting")
        self.assertEqual(review["task_text"], "")
        self.assertNotIn("我已命中", review["assistant_message"])

    def test_conversation_supports_small_talk_before_task_details(self) -> None:
        review = FleetControlApp().review_conversation([{"role": "user", "content": "随便聊聊"}])
        self.assertEqual(review["state"], "chatting")
        self.assertEqual(review["task_text"], "")
        self.assertIn("小派", review["assistant_message"])

    def test_conversation_handles_non_task_acknowledgement_naturally(self) -> None:
        review = FleetControlApp().review_conversation([{"role": "user", "content": "这确实不是个任务"}])
        self.assertEqual(review["state"], "chatting")
        self.assertEqual(review["task_text"], "")
        self.assertNotIn("可执行任务", review["assistant_message"])

    def test_conversation_handles_weather_without_forcing_task_mode(self) -> None:
        review = FleetControlApp().review_conversation([{"role": "user", "content": "天气怎么样"}])
        self.assertEqual(review["state"], "chatting")
        self.assertEqual(review["task_text"], "")
        self.assertIn("实时数据", review["assistant_message"])

    def test_conversation_asks_natural_followup_for_partial_cola_task(self) -> None:
        review = FleetControlApp().review_conversation([{"role": "user", "content": "可乐"}])
        self.assertEqual(review["state"], "chatting")
        self.assertIn("可乐我记下了", review["assistant_message"])
        self.assertNotIn("暂时没有命中已审核 SOP", review["assistant_message"])

    def test_conversation_does_not_confirm_partial_pickup_without_destination(self) -> None:
        review = FleetControlApp().review_conversation([{"role": "user", "content": "去拿杯可乐"}])
        self.assertEqual(review["state"], "chatting")
        self.assertIn("再告诉我送到哪一桌", review["assistant_message"])
        self.assertNotIn("我已命中", review["assistant_message"])

    def test_conversation_can_complete_partial_pickup_with_destination_later(self) -> None:
        review = FleetControlApp().review_conversation(
            [
                {"role": "user", "content": "去拿杯可乐"},
                {"role": "assistant", "content": "可乐我记下了。再告诉我送到哪一桌或哪位顾客就行。"},
                {"role": "user", "content": "送到3号桌"},
            ]
        )
        self.assertEqual(review["state"], "awaiting_confirmation")
        self.assertEqual(review["resolution"]["mode"], "sop")

    def test_conversation_does_not_reconfirm_old_task_after_garbled_non_task_input(self) -> None:
        review = FleetControlApp().review_conversation(
            [
                {"role": "user", "content": "去拿杯可乐"},
                {"role": "assistant", "content": "可乐我记下了。再告诉我送到哪一桌或哪位顾客就行。"},
                {"role": "user", "content": "学校执行任然后就成成机器"},
            ]
        )
        self.assertEqual(review["state"], "chatting")
        self.assertNotIn("我已命中", review["assistant_message"])
        self.assertNotIn("可乐我记下了", review["assistant_message"])
        self.assertIn("不更新到任务里", review["assistant_message"])

    def test_conversation_cancel_clears_pending_candidate_task(self) -> None:
        review = FleetControlApp().review_conversation(
            [
                {"role": "user", "content": "去拿杯可乐"},
                {"role": "assistant", "content": "可乐我记下了。再告诉我送到哪一桌或哪位顾客就行。"},
                {"role": "user", "content": "取消取消取消任务"},
            ]
        )
        self.assertEqual(review["state"], "chatting")
        self.assertEqual(review["task_text"], "")
        self.assertIn("已取消", review["assistant_message"])

    def test_conversation_requires_supported_sop_entities_before_confirmation(self) -> None:
        class UnsupportedDrinkClient:
            def infer_intent(self, user_input, prompt):
                return {
                    "intent_type": "prepare_and_deliver_drinks",
                    "drinks": [{"drink_type": "tea", "temperature": "hot", "sugar": "default", "ice": "regular"}],
                    "items": [],
                    "destination": "customer",
                    "constraints": {},
                    "confidence": 0.9,
                    "requires_confirmation": False,
                }

        app = FleetControlApp()
        app.interpreter = HybridTaskInterpreter(model_client=UnsupportedDrinkClient())
        review = app.review_conversation([{"role": "user", "content": "给顾客做一杯热茶"}])

        self.assertEqual(review["state"], "chatting")
        self.assertIn("不在当前 SOP 范围内", review["assistant_message"])

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

    def test_ollama_json_object_can_be_extracted_from_model_text(self) -> None:
        payload = OllamaModelClient._loads_json_object(
            '<think>reasoning omitted</think>\n{"intent_type": "pickup_and_deliver_items", "drinks": [], "items": []}'
        )
        self.assertEqual(payload["intent_type"], "pickup_and_deliver_items")


if __name__ == "__main__":
    unittest.main()
