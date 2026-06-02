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
from humanoid_fleet.understanding import HybridTaskInterpreter, build_model_client_from_env


TASK_KEYWORDS = (
    "卡布奇诺",
    "美式",
    "可乐",
    "咖啡",
    "饮料",
    "饮品",
    "杯",
    "拿",
    "取",
    "端",
    "送",
    "制作",
    "做",
    "准备",
    "顾客",
    "号桌",
)
META_QUESTION_KEYWORDS = ("命中", "sop", "SOP", "什么意思", "为什么", "确认", "解释")
CANCEL_KEYWORDS = ("取消", "不用了", "算了", "停止", "撤销", "别执行")
CONFIRM_KEYWORDS = ("确认", "可以", "对", "没错", "开始", "开始执行")
DESTINATION_KEYWORDS = ("顾客", "号桌", "桌", "前台", "吧台", "送到", "送给", "给")
SMALL_TALK_KEYWORDS = (
    "随便聊",
    "聊聊",
    "聊天",
    "你好",
    "hello",
    "hi",
    "哈哈",
    "没事",
    "无聊",
)
SUPPORTED_DRINKS = {"cappuccino", "americano"}
SUPPORTED_ITEMS = {"cola"}
INVALID_DESTINATIONS = {"", "none", "null", "unknown", "未知"}
CHAT_SYSTEM_PROMPT = (
    "你是「小派」，一家线下门店的拟人化服务机器人助手，正在和到店的顾客或店员轻松地打招呼、闲聊。\n"
    "\n"
    "## 你的身份\n"
    "- 你叫小派，在咖啡饮品门店工作，常见任务是给顾客制作并递送卡布奇诺、美式等饮品，或帮忙取送可乐等物品。\n"
    "- 店里通常有两位机器人协作：一位前台接待（Greeter），一位吧台咖啡师（Barista）。\n"
    "- 你会自然地聊聊门店的招牌、咖啡知识、当下心情，但不要假装你是真人。\n"
    "\n"
    "## 聊天风格\n"
    "- 语气温暖、口语化、像在吧台边聊天，少用「您好/请问」这种客服腔。\n"
    "- 回复 1-3 句话，能用问句收尾就更自然。\n"
    "- 喜欢用 1-2 个 emoji（☕️ 🙂 😄 🤔）点缀，但不要堆砌。\n"
    "- 避免「这是个任务/不是任务」「命中 SOP」这类术语，用户没问就别主动说。\n"
    "\n"
    "## 能聊的话题（顺着话题聊，不要生硬列举）\n"
    "- 你能做什么：制作并递送咖啡（卡布奇诺、美式）、取送可乐等小物品、招呼顾客到座位。\n"
    "- 门店氛围：早八点最忙，下午比较清闲；周末家庭客人多。\n"
    "- 咖啡冷知识：卡布奇诺奶泡厚、美式是浓缩兑水、冰美式要先放冰。\n"
    "- 机器人趣事：第一次端盘子差点洒、被小朋友摸头。\n"
    "\n"
    "## 不能做的事（如实告知，不要编造）\n"
    "- 没有实时数据：天气、新闻、股价、比分、当前时间、查询物流等都说「这个我没接到数据」并建议换个话题。\n"
    "- 不能离开门店：跨城外卖、远程操控都不行。\n"
    "- 不能假装有人：不要说「我问下同事」「我帮你查」后编答案。\n"
    "\n"
    "## 任务识别（轻处理）\n"
    "- 当用户明确说出「帮我做一杯 X」「拿杯可乐到 3 号桌」这类话，简短追问缺失项（送到哪、口味偏好），自然过渡到 SOP 即可。\n"
    "- 不要在闲聊里反复提醒「我可以帮你做任务」，用户问到了再答。\n"
)


def _conversation_text(messages: list[dict[str, str]]) -> str:
    relevant_messages = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content", "").strip()
        if _looks_like_cancel(content):
            relevant_messages = []
            continue
        if _looks_like_task_detail(content):
            relevant_messages.append(content)
    return "\n".join(
        relevant_messages
    ).strip()


def _latest_user_message(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content", "").strip()
    return ""


def _looks_like_task_detail(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if _looks_like_small_talk(normalized):
        return False
    if _looks_like_meta_question(normalized) and not any(keyword in normalized for keyword in TASK_KEYWORDS):
        return False
    return any(keyword in normalized for keyword in TASK_KEYWORDS)


def _looks_like_cancel(text: str) -> bool:
    return any(keyword in text for keyword in CANCEL_KEYWORDS)


def _looks_like_confirmation(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if "?" in normalized or "？" in normalized:
        return False
    if any(keyword in normalized for keyword in ("确认什么", "为什么", "解释", "命中什么")):
        return False
    return any(keyword in normalized for keyword in CONFIRM_KEYWORDS)


def _has_explicit_destination(text: str) -> bool:
    return any(keyword in text for keyword in DESTINATION_KEYWORDS)


def _looks_like_meta_question(text: str) -> bool:
    normalized = text.strip()
    return (
        "?" in normalized
        or "？" in normalized
        or any(keyword in normalized for keyword in META_QUESTION_KEYWORDS)
    )


def _looks_like_small_talk(text: str) -> bool:
    normalized = text.strip().lower()
    return any(keyword in normalized for keyword in SMALL_TALK_KEYWORDS)


class FleetControlApp:
    def __init__(self) -> None:
        self.interpreter = HybridTaskInterpreter(model_client=build_model_client_from_env())
        self.memory = TaskMemory()
        self.resolver = ExecutionResolver(self.memory)
        self.orchestrator = HierarchicalOrchestrator()
        self.scheduler = Scheduler()
        self.runtime = SkillRuntime()

    def review_conversation(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        latest_message = _latest_user_message(messages)
        if _looks_like_cancel(latest_message):
            return {
                "state": "chatting",
                "task_text": "",
                "assistant_message": "好的，当前候选任务已取消。我们可以先聊聊；如果你之后还想让机器人做事，再重新告诉我任务就行。",
            }

        task_text = _conversation_text(messages)
        if task_text and latest_message and not _looks_like_task_detail(latest_message) and not _looks_like_confirmation(latest_message):
            should_chat = (
                _asks_about_weather(latest_message)
                or _looks_like_small_talk(latest_message)
                or _asks_about_capability(latest_message)
                or _looks_like_meta_question(latest_message)
            )
            assistant_message = (
                self._build_general_chat_response([{"role": "user", "content": latest_message}])
                if should_chat
                else "这句我先不更新到任务里，刚才的候选任务也不会自动确认。我们可以先聊着；如果你想继续那个任务，再告诉我送到哪一桌或哪位顾客。"
            )
            return {
                "state": "chatting",
                "task_text": task_text,
                "assistant_message": assistant_message,
            }

        if not task_text:
            if _asks_about_capability(latest_message):
                assistant_message = self._build_general_chat_response(messages)
            elif _asks_about_weather(latest_message):
                assistant_message = self._build_general_chat_response(messages)
            elif _looks_like_meta_question(latest_message):
                assistant_message = self._build_general_chat_response(messages)
            elif latest_message:
                assistant_message = self._build_general_chat_response(messages)
            else:
                assistant_message = (
                    "你好呀 ☕ 我是小派，在吧台这边上班。"
                    "想喝点什么，或者有什么要帮忙的，直接告诉我就行。"
                )
            return {
                "state": "chatting",
                "task_text": "",
                "assistant_message": assistant_message,
            }

        try:
            user_input = UserInput(text=task_text)
            intent = self.interpreter.parse(user_input)
            resolved = self.resolver.resolve(intent)
        except Exception:
            return {
                "state": "chatting",
                "task_text": task_text,
                "assistant_message": self._build_slot_followup(task_text),
            }

        validity_error = self._validate_intent_for_confirmation(intent)
        if validity_error is not None:
            return {
                "state": "chatting",
                "task_text": task_text,
                "assistant_message": validity_error,
                "intent": asdict(intent),
                "resolution": {
                    "mode": resolved.mode.value,
                    "skill_name": resolved.skill_name,
                    "explanation": resolved.explanation,
                },
                "template": asdict(resolved.template) if resolved.template is not None else None,
            }

        if resolved.mode.value != "sop":
            return {
                "state": "chatting",
                "task_text": task_text,
                "assistant_message": self._build_slot_followup(task_text),
                "intent": asdict(intent),
                "resolution": {
                    "mode": resolved.mode.value,
                    "skill_name": resolved.skill_name,
                    "explanation": resolved.explanation,
                },
                "template": asdict(resolved.template) if resolved.template is not None else None,
            }

        return {
            "state": "awaiting_confirmation",
            "task_text": task_text,
            "assistant_message": self._build_confirmation_message(intent, resolved.template.name if resolved.template else "SOP"),
            "intent": asdict(intent),
            "resolution": {
                "mode": resolved.mode.value,
                "skill_name": resolved.skill_name,
                "explanation": resolved.explanation,
            },
            "template": asdict(resolved.template) if resolved.template is not None else None,
            "understanding_prompt": self.interpreter.preview_prompt(user_input),
            "workflow": self._build_workflow_preview(intent, resolved),
        }

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

    def _build_workflow_preview(self, intent, resolved) -> dict[str, Any]:
        robots = build_demo_robots()
        workflow = self.orchestrator.build_workflow(intent, resolved)
        scheduled_workflow = self.scheduler.assign(workflow, robots)
        return asdict(scheduled_workflow)

    @staticmethod
    def _build_confirmation_message(intent, template_name: str) -> str:
        pieces = []
        for drink in intent.drinks:
            pieces.append(f"{drink.quantity}杯{drink.drink_type}，温度{drink.temperature}，糖度{drink.sugar}，冰量{drink.ice}")
        for item in intent.items:
            pieces.append(f"{item.quantity}份{item.item_type}，温度{item.temperature}")

        target = intent.destination or "customer"
        summary = "；".join(pieces) if pieces else intent.raw_text
        return f"我已命中「{template_name}」。请确认：{summary}，送达 {target}。确认后我再生成机器人分工和执行计划。"

    @staticmethod
    def _build_slot_followup(task_text: str) -> str:
        if "可乐" in task_text:
            return "可乐我记下了。你是想让机器人去拿一杯可乐，还是把已有的可乐送出去？再告诉我送到哪一桌或哪位顾客就行。"
        if "卡布奇诺" in task_text or "美式" in task_text or "咖啡" in task_text:
            return "饮品方向我明白了。再补一下具体杯数、冷热/糖冰要求，以及送给谁或送到哪一桌，我就能判断是否命中 SOP。"
        if any(keyword in task_text for keyword in ("送", "顾客", "号桌")):
            return "我看到你提到了送达相关信息。再告诉我要送什么物品或饮品，我就能继续判断 SOP。"
        return "我大概听到任务苗头了，但信息还不够。告诉我要处理什么、数量或口味要求、送到哪里，我再帮你匹配 SOP。"

    def _build_general_chat_response(self, messages: list[dict[str, str]]) -> str:
        latest_message = _latest_user_message(messages)

        if _asks_about_weather(latest_message):
            return (
                "天气这种实时数据我这边没接上，报不准 😥 不过你来店里的时候我倒是能告诉你今天吧台冷气开多大。"
                "你今天打算喝点啥？"
            )

        chat = getattr(self.interpreter.model_client, "chat", None)
        if callable(chat):
            try:
                reply = chat(messages[-8:], CHAT_SYSTEM_PROMPT)
                if reply:
                    return reply
            except Exception:
                pass

        if _looks_like_small_talk(latest_message):
            return (
                "可以啊 ☕ 我先自我介绍下：我是小派，在吧台这边上班。"
                "平时负责做咖啡、递饮品，偶尔也帮忙跑个腿。"
                "你想聊点啥？门店的、咖啡的、还是今天的心情？"
            )

        if _asks_about_capability(latest_message):
            return (
                "我现在能干的主要是这些 ☕\n"
                "• 制作并递送卡布奇诺、美式（冷热、冰量、糖度都能调）\n"
                "• 取送可乐等小物品到指定桌号或顾客手上\n"
                "• 在吧台和前台之间招呼一下客人\n"
                "不过我离不了店，天气新闻这种实时数据也接不上。你要试试看吗？"
            )

        return (
            "嗯嗯，我听着呢 🙂 你要是有想喝的就直接说，比如「来杯热美式不加糖」；"
            "要是只想随便聊，那就继续，咱不急。"
        )

    @staticmethod
    def _validate_intent_for_confirmation(intent) -> str | None:
        destination = str(intent.destination or "").strip().lower()
        if destination in INVALID_DESTINATIONS or not _has_explicit_destination(intent.raw_text):
            if intent.intent_type == "pickup_and_deliver_items" and intent.items:
                return "可乐我记下了。你是想让机器人去拿一杯可乐，还是把已有的可乐送出去？再告诉我送到哪一桌或哪位顾客就行。"
            if intent.intent_type == "prepare_and_deliver_drinks" and intent.drinks:
                return "饮品方向我明白了。再补一下送给谁或送到哪一桌，我就能判断是否命中 SOP。"
            return "我还不能进入 SOP 确认，因为送达位置不明确。请补充是送给顾客、几号桌，或者具体位置。"

        if intent.intent_type == "prepare_and_deliver_drinks":
            if not intent.drinks:
                return "我还不能进入饮品制作 SOP，因为没有识别到具体饮品。请补充卡布奇诺、美式等饮品和口味要求。"
            unknown = [drink.drink_type for drink in intent.drinks if drink.drink_type not in SUPPORTED_DRINKS]
            if unknown:
                return "我识别到的饮品还不在当前 SOP 范围内。现在支持卡布奇诺和美式，请重新说明要制作的饮品。"
            return None

        if intent.intent_type == "pickup_and_deliver_items":
            if not intent.items:
                return "我还不能进入取送 SOP，因为没有识别到具体物品。请补充要拿什么，以及送到哪里。"
            unknown = [item.item_type for item in intent.items if item.item_type not in SUPPORTED_ITEMS]
            if unknown:
                return "我识别到的物品还不在当前 SOP 范围内。现在支持可乐取送，请重新说明要取送的物品。"
            return None

        return "我理解了你的话，但它还不是当前已登记 SOP 支持的服务任务。请说明要制作饮品或取送物品。"


def _asks_about_weather(text: str) -> bool:
    return "天气" in text or "下雨" in text or "气温" in text


def _asks_about_capability(text: str) -> bool:
    normalized = text.lower()
    keywords = (
        "能做什么", "能干嘛", "能干吗", "能干啥", "你会什么", "你会啥",
        "有什么功能", "有什么能力", "能做啥", "能做啥事", "你做啥",
        "你能", "can you", "what can you do", "你会做", "能帮我",
    )
    return any(keyword in normalized for keyword in keywords)
