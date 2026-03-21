from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RobotStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


class ResolutionMode(str, Enum):
    SOP = "sop"
    SKILL = "skill"
    AGENT = "agent"


@dataclass
class MediaAttachment:
    media_type: str
    uri: str
    description: str = ""


@dataclass
class UserInput:
    text: str = ""
    attachments: list[MediaAttachment] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class DrinkOrder:
    drink_type: str
    temperature: str
    sugar: str
    ice: str
    quantity: int = 1


@dataclass
class ItemOrder:
    item_type: str
    temperature: str
    quantity: int = 1


@dataclass
class Intent:
    intent_type: str
    drinks: list[DrinkOrder]
    items: list[ItemOrder]
    destination: str
    constraints: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    requires_confirmation: bool = False
    raw_text: str = ""


@dataclass
class TaskTemplate:
    template_id: str
    name: str
    intent_type: str
    workflow_steps: list[str]
    supports_multi_robot: bool = True


@dataclass
class ResolvedExecution:
    mode: ResolutionMode
    template: TaskTemplate | None
    skill_name: str | None = None
    explanation: str = ""


@dataclass
class WorkflowTask:
    task_id: str
    name: str
    skill_name: str
    parameters: dict[str, str]
    assigned_robot_id: str | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class WorkflowPlan:
    workflow_id: str
    intent: Intent
    template_id: str
    tasks: list[WorkflowTask]
    mode: str
    explanation: list[str] = field(default_factory=list)


@dataclass
class RobotCapability:
    robot_id: str
    name: str
    skill_names: set[str]
    location: str
    battery_level: int
    status: RobotStatus = RobotStatus.IDLE


@dataclass
class SkillExecution:
    task_id: str
    robot_id: str
    skill_name: str
    parameters: dict[str, str]
    status: str = "planned"


@dataclass
class ExecutionPlan:
    workflow_id: str
    executions: list[SkillExecution]
    user_summary: str
    operator_summary: str
