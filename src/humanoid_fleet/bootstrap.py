from __future__ import annotations

from humanoid_fleet.domain import RobotCapability, RobotStatus


def build_demo_robots() -> list[RobotCapability]:
    return [
        RobotCapability(
            robot_id="robot-greeter-01",
            name="Greeter",
            skill_names={"speak", "deliver_items", "pickup_item"},
            location="front_desk",
            battery_level=88,
            status=RobotStatus.IDLE,
        ),
        RobotCapability(
            robot_id="robot-barista-01",
            name="Barista",
            skill_names={"speak", "operate_coffee_machine", "stage_order"},
            location="coffee_station",
            battery_level=91,
            status=RobotStatus.IDLE,
        ),
    ]
