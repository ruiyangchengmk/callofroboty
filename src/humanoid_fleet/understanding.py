from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from humanoid_fleet.domain import DrinkOrder, Intent, ItemOrder, UserInput


class Interpreter(Protocol):
    def parse(self, user_input: UserInput) -> Intent:
        ...


class ModelClient(Protocol):
    def infer_intent(self, user_input: UserInput, prompt: str) -> dict:
        ...


@dataclass
class InterpretationResult:
    intent: Intent
    source: str


class PromptBuilder:
    @staticmethod
    def build(user_input: UserInput) -> str:
        attachment_lines = []
        for attachment in user_input.attachments:
            attachment_lines.append(
                f"- type={attachment.media_type}, uri={attachment.uri}, description={attachment.description}"
            )

        attachments = "\n".join(attachment_lines) if attachment_lines else "- none"
        return (
            "You are a multimodal service-robot task parser. "
            "Convert the user request into a structured task intent.\n"
            "Return JSON only with keys: intent_type, drinks, items, destination, constraints, "
            "confidence, requires_confirmation.\n"
            "Allowed intent_type values include prepare_and_deliver_drinks and pickup_and_deliver_items.\n"
            f"User text: {user_input.text}\n"
            f"Attachments:\n{attachments}\n"
        )


class MockMultimodalLLMClient:
    """
    Local stand-in for a real multimodal model service.

    This keeps the architecture model-first and multimodal-ready while staying
    runnable without external API calls during prototyping.
    """

    def infer_intent(self, user_input: UserInput, prompt: str) -> dict:
        text = user_input.text
        normalized = text.replace("，", " ").replace("、", " ").replace("。", " ").strip()

        if "卡布奇诺" in normalized or "美式" in normalized:
            drinks = []
            if "卡布奇诺" in normalized:
                drinks.append(
                    {
                        "drink_type": "cappuccino",
                        "temperature": "regular",
                        "sugar": "default",
                        "ice": "no_ice" if "去冰" in normalized else "regular",
                        "quantity": 1,
                    }
                )
            if "美式" in normalized:
                drinks.append(
                    {
                        "drink_type": "americano",
                        "temperature": "hot" if "热美式" in normalized or "热 美式" in normalized else "regular",
                        "sugar": "no_sugar" if "不加糖" in normalized else "default",
                        "ice": "regular",
                        "quantity": 1,
                    }
                )
            return {
                "intent_type": "prepare_and_deliver_drinks",
                "drinks": drinks,
                "items": [],
                "destination": "customer",
                "constraints": {"service_style": "transparent", "priority": "normal"},
                "confidence": 0.94,
                "requires_confirmation": "顾客" not in normalized,
            }

        if "可乐" in normalized:
            destination = "customer"
            table_match = re.search(r"(\d+)\s*号桌", normalized)
            if table_match:
                destination = f"table_{table_match.group(1)}"

            return {
                "intent_type": "pickup_and_deliver_items",
                "drinks": [],
                "items": [
                    {
                        "item_type": "cola",
                        "temperature": "cold" if "冰" in normalized else "regular",
                        "quantity": 1,
                    }
                ],
                "destination": destination,
                "constraints": {"service_style": "transparent", "priority": "normal"},
                "confidence": 0.91,
                "requires_confirmation": False,
            }

        raise ValueError(f"Mock model could not confidently parse task: {text}")


class StructuredIntentNormalizer:
    @staticmethod
    def normalize(payload: dict, raw_text: str) -> Intent:
        drinks = [
            DrinkOrder(
                drink_type=item["drink_type"],
                temperature=item.get("temperature", "regular"),
                sugar=item.get("sugar", "default"),
                ice=item.get("ice", "regular"),
                quantity=item.get("quantity", 1),
            )
            for item in payload.get("drinks", [])
        ]

        items = [
            ItemOrder(
                item_type=item["item_type"],
                temperature=item.get("temperature", "regular"),
                quantity=item.get("quantity", 1),
            )
            for item in payload.get("items", [])
        ]

        return Intent(
            intent_type=payload["intent_type"],
            drinks=drinks,
            items=items,
            destination=payload.get("destination", "customer"),
            constraints=payload.get("constraints", {}),
            confidence=float(payload.get("confidence", 0.0)),
            requires_confirmation=bool(payload.get("requires_confirmation", False)),
            raw_text=raw_text,
        )


class RuleFallbackInterpreter:
    """Small deterministic fallback for prototype resiliency."""

    def parse(self, user_input: UserInput) -> Intent:
        text = user_input.text.replace("，", " ").replace("、", " ").replace("。", " ").strip()

        if "可乐" in text:
            table_match = re.search(r"(\d+)\s*号桌", text)
            destination = f"table_{table_match.group(1)}" if table_match else "customer"
            return Intent(
                intent_type="pickup_and_deliver_items",
                drinks=[],
                items=[ItemOrder(item_type="cola", temperature="cold" if "冰" in text else "regular", quantity=1)],
                destination=destination,
                constraints={"service_style": "transparent", "priority": "normal"},
                confidence=0.8,
                requires_confirmation=False,
                raw_text=user_input.text,
            )

        if "卡布奇诺" in text or "美式" in text:
            drinks = []
            if "卡布奇诺" in text:
                drinks.append(
                    DrinkOrder(
                        drink_type="cappuccino",
                        temperature="regular",
                        sugar="default",
                        ice="no_ice" if "去冰" in text else "regular",
                    )
                )
            if "美式" in text:
                drinks.append(
                    DrinkOrder(
                        drink_type="americano",
                        temperature="hot" if "热美式" in text or "热 美式" in text else "regular",
                        sugar="no_sugar" if "不加糖" in text else "default",
                        ice="regular",
                    )
                )
            return Intent(
                intent_type="prepare_and_deliver_drinks",
                drinks=drinks,
                items=[],
                destination="customer",
                constraints={"service_style": "transparent", "priority": "normal"},
                confidence=0.8,
                requires_confirmation="顾客" not in text,
                raw_text=user_input.text,
            )

        raise ValueError(f"Fallback interpreter does not understand task: {user_input.text}")


class HybridTaskInterpreter:
    def __init__(self, model_client: ModelClient | None = None, fallback: Interpreter | None = None) -> None:
        self.model_client = model_client or MockMultimodalLLMClient()
        self.fallback = fallback or RuleFallbackInterpreter()

    def parse(self, user_input: UserInput) -> Intent:
        prompt = PromptBuilder.build(user_input)
        try:
            payload = self.model_client.infer_intent(user_input, prompt)
            return StructuredIntentNormalizer.normalize(payload, raw_text=user_input.text)
        except Exception:
            return self.fallback.parse(user_input)

    @staticmethod
    def preview_prompt(user_input: UserInput) -> str:
        return PromptBuilder.build(user_input)

    @staticmethod
    def dump_payload(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)
