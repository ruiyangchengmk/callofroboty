from __future__ import annotations

from pprint import pprint

from humanoid_fleet.app import FleetControlApp


def run_demo(task_text: str) -> None:
    app = FleetControlApp()
    result = app.plan_task(task_text)

    print("\n=== Intent ===")
    pprint(result["intent"])
    print("\n=== Resolution ===")
    pprint(result["resolution"])
    print("\n=== Workflow ===")
    pprint(result["workflow"])
    print("\n=== Execution Plan ===")
    pprint(result["execution_plan"])


if __name__ == "__main__":
    run_demo("去给顾客制作一杯卡布奇诺去冰，一杯热美式不加糖")
