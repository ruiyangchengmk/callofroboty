from __future__ import annotations

import html
import json
import os
import re
from copy import deepcopy
from threading import RLock
from socketserver import ThreadingMixIn
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote
from urllib.request import Request, urlopen
from wsgiref.simple_server import WSGIServer, make_server

from humanoid_fleet.app import FleetControlApp, _looks_like_confirmation
from humanoid_fleet.memory import TaskMemory
from humanoid_fleet.understanding import PromptBuilder, build_model_client_from_env

DEFAULT_TASK = "去给顾客制作一杯卡布奇诺去冰，一杯热美式不加糖"
SPEECH_SERVICE_URL = "http://127.0.0.1:8010"
EXAMPLE_TASKS = [
    "去给顾客制作一杯卡布奇诺去冰，一杯热美式不加糖",
    "去拿杯冰可乐给3号桌顾客",
    "把可乐送给顾客",
]
CHAT_SYSTEM_PROMPT = (
    "你是一个中文门店服务机器人助手。你可以自然闲聊，也可以在用户提出明确服务任务时协助判断 SOP。"
    "不要把闲聊、测试字符、反问强行解释成任务。"
    "如果用户只是聊天，就简短自然地回应。"
    "如果用户问实时信息而你没有工具或数据，就坦诚说明这个预览页暂时没有接入实时数据。"
    "如果用户开始描述服务任务，可以温和追问缺失信息。每次回复不超过三句话。"
)
OLLAMA_PROMPT_TEMPLATE = (
    "You are a multimodal service-robot task parser. "
    "Convert the user request into a structured task intent.\n"
    "Return JSON only with keys: intent_type, drinks, items, destination, constraints, "
    "confidence, requires_confirmation.\n"
    "Allowed intent_type values include prepare_and_deliver_drinks and pickup_and_deliver_items.\n"
    "User text: {user_text}\n"
    "Attachments:\n{attachments}\n"
)


def _collect_settings() -> dict[str, object]:
    """Snapshot runtime settings so the UI can show what is wired up."""
    backend = os.getenv("HUMANOID_FLEET_LLM_BACKEND", "mock").strip().lower()
    if backend == "ollama":
        llm_provider = {
            "backend": "ollama",
            "model": os.getenv("OLLAMA_MODEL", "qwen3.5:0.8b"),
            "host": os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"),
            "timeout_seconds": float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "20")),
            "thinking_disabled": os.getenv("OLLAMA_DISABLE_THINKING", "true").strip().lower()
            not in {"0", "false", "no"},
        }
    else:
        llm_provider = {
            "backend": "mock",
            "model": "MockMultimodalLLMClient",
            "host": "(in-process)",
            "timeout_seconds": 0,
            "thinking_disabled": True,
        }

    return {
        "llm": llm_provider,
        "speech": {
            "service_url": SPEECH_SERVICE_URL,
            "asr_model": "sherpa-onnx-paraformer-zh-small-2024-03-09",
            "tts_model": "vits-melo-tts-zh_en",
        },
        "prompts": {
            "intent_parser_template": OLLAMA_PROMPT_TEMPLATE,
            "intent_parser_sample": PromptBuilder.build(
                _sample_user_input(DEFAULT_TASK)
            ),
            "chat_system_prompt": CHAT_SYSTEM_PROMPT,
        },
        "execution_policy": [
            "1. 命中已审核 SOP/工作流模板（task-memory-service）",
            "2. 直达原子技能（skill-runtime-service）",
            "3. 代理规划（orchestrator-service，agent 模式）",
        ],
    }


def _sample_user_input(text: str):
    from humanoid_fleet.domain import UserInput  # local import to avoid cycles

    return UserInput(text=text)


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def _clean_tts_text(text: str) -> str:
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff，。！？；：、,.!?;:（）()《》“”\"' -]", "", text)
    cleaned = re.sub(r"[，。！？；：、,.!?;:（）()《》“”\"']", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _label_resolution(mode: str) -> str:
    return {
        "sop": "SOP 工作流",
        "skill": "Direct Skill",
        "agent": "Agent 规划",
    }.get(mode, mode)


def _render_example_links() -> str:
    return "".join(
        f'<a class="example-chip" href="/?task={quote_plus(task)}">{html.escape(task)}</a>'
        for task in EXAMPLE_TASKS
    )


def _render_route_steps(active_mode: str) -> str:
    steps = [
        ("sop", "01", "SOP", "优先命中已审核工作流/SOP"),
        ("skill", "02", "Skill", "未命中 SOP 时直接调用已有原子能力"),
        ("agent", "03", "Agent", "资产都不覆盖时才进入开放式规划"),
    ]
    cards = []
    reached = True
    for mode, num, title, desc in steps:
        state = "active" if mode == active_mode else "reached" if reached else "idle"
        if mode == active_mode:
            reached = False
        cards.append(
            f"""
            <article class="route-step {state}">
              <div class="route-num">{num}</div>
              <div>
                <h3>{title}</h3>
                <p>{desc}</p>
              </div>
            </article>
            """
        )
    return "".join(cards)


def _render_intent_entities(intent: dict) -> str:
    rows = []
    for drink in intent["drinks"]:
        rows.append(
            f"""
            <li class="entity-card">
              <strong>{html.escape(drink['drink_type'])}</strong>
              <span>温度 {html.escape(drink['temperature'])}</span>
              <span>糖度 {html.escape(drink['sugar'])}</span>
              <span>冰量 {html.escape(drink['ice'])}</span>
            </li>
            """
        )
    for item in intent["items"]:
        rows.append(
            f"""
            <li class="entity-card">
              <strong>{html.escape(item['item_type'])}</strong>
              <span>温度 {html.escape(item['temperature'])}</span>
              <span>数量 {item['quantity']}</span>
            </li>
            """
        )
    return "".join(rows) or '<li class="entity-card empty">当前没有抽取到对象</li>'


def _render_task_timeline(workflow: dict) -> str:
    items = []
    for index, task in enumerate(workflow["tasks"], start=1):
        deps = "、".join(task["depends_on"]) if task["depends_on"] else "无"
        items.append(
            f"""
            <li class="timeline-item">
              <div class="timeline-dot">{index}</div>
              <div class="timeline-body">
                <div class="timeline-head">
                  <strong>{html.escape(task['name'])}</strong>
                  <span class="badge">{html.escape(task['assigned_robot_id'] or '未分配')}</span>
                </div>
                <p>技能：<code>{html.escape(task['skill_name'])}</code></p>
                <p>依赖：{html.escape(deps)}</p>
              </div>
            </li>
            """
        )
    return "".join(items)


def _render_execution_cards(execution_plan: dict) -> str:
    cards = []
    for item in execution_plan["executions"]:
        params = ", ".join(f"{key}={value}" for key, value in item["parameters"].items())
        cards.append(
            f"""
            <article class="mini-card">
              <h3>{html.escape(item['skill_name'])}</h3>
              <p><strong>机器人：</strong>{html.escape(item['robot_id'])}</p>
              <p><strong>参数：</strong>{html.escape(params)}</p>
              <p><strong>状态：</strong>{html.escape(item['status'])}</p>
            </article>
            """
        )
    return "".join(cards)


def _render_template_catalog(catalog: list[dict], active_template: dict | None) -> str:
    cards = []
    active_id = active_template["template_id"] if active_template else ""
    for template in catalog:
        state = "catalog-card active" if template["template_id"] == active_id else "catalog-card"
        steps = " -> ".join(template["workflow_steps"])
        cards.append(
            f"""
            <article class="{state}">
              <h3>{html.escape(template['name'])}</h3>
              <p><strong>ID：</strong>{html.escape(template['template_id'])}</p>
              <p><strong>意图：</strong>{html.escape(template['intent_type'])}</p>
              <p><strong>步骤：</strong>{html.escape(steps)}</p>
            </article>
            """
        )
    return "".join(cards)


def _render_robot_cards(robots: list[dict]) -> str:
    cards = []
    for robot in robots:
        skills = ", ".join(sorted(robot["skill_names"]))
        cards.append(
            f"""
            <article class="mini-card">
              <h3>{html.escape(robot['name'])}</h3>
              <p><strong>ID：</strong>{html.escape(robot['robot_id'])}</p>
              <p><strong>位置：</strong>{html.escape(robot['location'])}</p>
              <p><strong>电量：</strong>{robot['battery_level']}%</p>
              <p><strong>技能：</strong>{html.escape(skills)}</p>
            </article>
            """
        )
    return "".join(cards)


def _render_chat_messages(messages: list[dict[str, str]]) -> str:
    has_real_messages = bool(messages)
    if not messages:
        messages = [
            {
                "role": "assistant",
                "content": "你好，我可以先和你确认服务任务。你可以告诉我要做什么、送到哪里、有什么偏好。",
            }
        ]

    latest_assistant_index = max(
        (index for index, message in enumerate(messages) if message.get("role") == "assistant"),
        default=-1,
    )
    rows = []
    for index, message in enumerate(messages):
        role = message.get("role", "assistant")
        label = "用户" if role == "user" else "LLM"
        content = message.get("content", "")
        audio = ""
        if role == "assistant":
            should_autoplay = "true" if has_real_messages and index == latest_assistant_index else "false"
            audio = (
                f'<audio class="tts-player" controls preload="none" '
                f'data-autoplay="{should_autoplay}" data-tts-text="{html.escape(content)}" '
                f'src="/tts.wav?text={quote_plus(content)}&i={index}"></audio>'
            )
        rows.append(
            f"""
            <div class="chat-row {html.escape(role)}">
              <div class="bubble">
                <span>{label}</span>
                <p>{html.escape(content)}</p>
                {audio}
              </div>
            </div>
            """
        )
    return "".join(rows)


def _render_pending_confirmation(review: dict | None) -> str:
    if not review or review.get("state") != "awaiting_confirmation":
        return ""

    intent = review["intent"]
    template = review["template"]
    resolution = review["resolution"]
    return f"""
    <section class="card confirmation-card">
      <div class="section-head">
        <div>
          <span class="eyebrow">SOP Confirmation</span>
          <h2>已命中 SOP，等待用户确认</h2>
        </div>
        <span class="badge">待确认</span>
      </div>
      <p><strong>模板：</strong>{html.escape(template['name'])}</p>
      <p><strong>模板 ID：</strong>{html.escape(template['template_id'])}</p>
      <p><strong>目的地：</strong>{html.escape(intent['destination'])}</p>
      <p><strong>置信度：</strong>{intent['confidence']}</p>
      <ul class="entity-grid">{_render_intent_entities(intent)}</ul>
      <p class="muted">{html.escape(resolution['explanation'])}</p>
    </section>
    """


def _workflow_node_status(task: dict, mode: str) -> tuple[str, str]:
    if mode == "preview":
        return ("awaiting", "待确认")
    if task.get("depends_on"):
        return ("waiting", "等待依赖")
    return ("queued", "排队中")


def _render_workflow_state_graph(workflow: dict | None, mode: str) -> str:
    if not workflow:
        return ""

    tasks = workflow.get("tasks", [])
    if not tasks:
        return ""

    nodes = []
    for index, task in enumerate(tasks, start=1):
        status, label = _workflow_node_status(task, mode)
        deps = "、".join(task.get("depends_on") or []) or "无"
        robot = task.get("assigned_robot_id") or "未分配"
        params = json.dumps(task.get("parameters") or {}, ensure_ascii=False, indent=2)
        nodes.append(
            f"""
            <li class="workflow-state-node {status}" data-task-id="{html.escape(task.get('task_id', ''))}" data-status="{status}">
              <div class="workflow-node-index">{index}</div>
              <div class="workflow-node-body">
                <div class="workflow-node-head">
                  <div>
                    <h3>{html.escape(task.get('name', 'Unnamed task'))}</h3>
                    <code>{html.escape(task.get('skill_name', 'unknown_skill'))}</code>
                  </div>
                  <span class="workflow-status-badge">{label}</span>
                </div>
                <dl class="workflow-node-meta">
                  <dt>机器人</dt><dd>{html.escape(robot)}</dd>
                  <dt>依赖</dt><dd>{html.escape(deps)}</dd>
                </dl>
                <details>
                  <summary>参数</summary>
                  <pre>{html.escape(params)}</pre>
                </details>
              </div>
            </li>
            """
        )

    return "".join(nodes)


def _render_workflow_state_panel(review: dict | None, result: dict | None) -> str:
    workflow = None
    mode = "preview"
    title_status = "SOP 已命中，等待确认"
    summary = "系统已把 SOP 展开成可执行的原子技能状态图。确认后，机器人会按这个图执行并回写状态。"

    if result and result.get("workflow"):
        workflow = result["workflow"]
        mode = "confirmed"
        title_status = "已确认，等待机器人执行"
        summary = "执行计划已生成。后续接入机器人后，节点状态会从这里同步更新。"
    elif review and review.get("state") == "awaiting_confirmation":
        workflow = review.get("workflow")

    if not workflow:
        return ""

    return f"""
    <aside class="workflow-state-panel" aria-label="Workflow state graph">
      <div class="workflow-state-head">
        <span class="eyebrow">Workflow State</span>
        <h2>原子技能状态图</h2>
        <p>{html.escape(summary)}</p>
      </div>
      <div class="workflow-state-meta">
        <span>{html.escape(title_status)}</span>
        <span>{html.escape(workflow.get('mode', 'unassigned'))}</span>
        <span>{html.escape(workflow.get('template_id', ''))}</span>
      </div>
      <ol class="workflow-state-list" data-workflow-id="{html.escape(workflow.get('workflow_id', ''))}">
        {_render_workflow_state_graph(workflow, mode)}
      </ol>
    </aside>
    """


def render_page(
    messages: list[dict[str, str]],
    review: dict | None = None,
    result: dict | None = None,
    error: str | None = None,
) -> str:
    history_value = html.escape(json.dumps(messages, ensure_ascii=False))
    content = ""
    pending_confirmation = _render_pending_confirmation(review)
    workflow_state_panel = _render_workflow_state_panel(review, result)
    body_class = "workflow-panel-open" if workflow_state_panel else ""

    if error:
        content = f"""
        <section class="card error-card">
          <h2>处理失败</h2>
          <p>{html.escape(error)}</p>
        </section>
        """
    elif result:
        intent = result["intent"]
        template = result["template"]
        resolution = result["resolution"]
        workflow = result["workflow"]
        execution_plan = result["execution_plan"]
        prompt = result["understanding_prompt"]
        robots = result["robots"]
        catalog = result["template_catalog"]

        content = f"""
        <section class="summary-grid">
          <article class="hero-card highlight">
            <span class="eyebrow">Execution Path</span>
            <h2>{_label_resolution(resolution['mode'])}</h2>
            <p>{html.escape(resolution['explanation'])}</p>
            <div class="pill-row">
              <span class="pill">执行模式 {html.escape(workflow['mode'])}</span>
              <span class="pill">目标 {html.escape(intent['intent_type'])}</span>
              <span class="pill">确认 {'需要' if intent['requires_confirmation'] else '无需'}</span>
            </div>
          </article>
          <article class="hero-card">
            <span class="eyebrow">User Transparency</span>
            <h2>用户可见反馈</h2>
            <p>{html.escape(execution_plan['user_summary'])}</p>
            <p class="muted">{html.escape(execution_plan['operator_summary'])}</p>
          </article>
        </section>

        <section class="card">
          <div class="section-head">
            <div>
              <span class="eyebrow">Routing Ladder</span>
              <h2>SOP → Skill → Agent</h2>
            </div>
            <p class="muted">这条链路表达的是执行资产优先级，而不是任务理解方式。</p>
          </div>
          <div class="route-grid">
            {_render_route_steps(resolution['mode'])}
          </div>
        </section>

        <section class="grid two">
          <article class="card">
            <span class="eyebrow">Structured Intent</span>
            <h2>任务理解结果</h2>
            <p><strong>目的地：</strong>{html.escape(intent['destination'])}</p>
            <p><strong>置信度：</strong>{intent['confidence']}</p>
            <ul class="entity-grid">{_render_intent_entities(intent)}</ul>
          </article>
          <article class="card">
            <span class="eyebrow">Task Asset</span>
            <h2>命中的工作流资产</h2>
            <p><strong>模板：</strong>{html.escape(template['name']) if template else '无'}</p>
            <p><strong>模板 ID：</strong>{html.escape(template['template_id']) if template else '无'}</p>
            <p><strong>支持多机器人：</strong>{'是' if template and template['supports_multi_robot'] else '否'}</p>
            <p><strong>直达技能：</strong>{html.escape(resolution['skill_name']) if resolution['skill_name'] else '无'}</p>
          </article>
        </section>

        <section class="card">
          <div class="section-head">
            <div>
              <span class="eyebrow">Workflow</span>
              <h2>任务时间线与机器人分工</h2>
            </div>
            <p class="muted">先命中 SOP，再实例化成当前任务的工作流与技能链。</p>
          </div>
          <ol class="timeline">
            {_render_task_timeline(workflow)}
          </ol>
        </section>

        <section class="grid two">
          <article class="card">
            <span class="eyebrow">Execution Plan</span>
            <h2>技能执行卡片</h2>
            <div class="mini-grid">
              {_render_execution_cards(execution_plan)}
            </div>
          </article>
          <article class="card">
            <span class="eyebrow">Explainability</span>
            <h2>编排解释</h2>
            <ul class="plain-list">
              {''.join(f"<li>{html.escape(line)}</li>" for line in workflow['explanation'])}
            </ul>
          </article>
        </section>

        <section class="grid two">
          <article class="card">
            <div class="section-head">
              <div>
                <span class="eyebrow">SOP Catalog</span>
                <h2>已登记工作流资产</h2>
              </div>
              <p class="muted">当前命中的模板会高亮。</p>
            </div>
            <div class="mini-grid">
              {_render_template_catalog(catalog, template)}
            </div>
          </article>
          <article class="card">
            <div class="section-head">
              <div>
                <span class="eyebrow">Fleet</span>
                <h2>演示机器人资源</h2>
              </div>
              <p class="muted">后续可替换成真实运行时状态。</p>
            </div>
            <div class="mini-grid">
              {_render_robot_cards(robots)}
            </div>
          </article>
        </section>

        <section class="card">
          <span class="eyebrow">Understanding Prompt</span>
          <h2>模型理解提示词预览</h2>
          <pre>{html.escape(prompt)}</pre>
        </section>
        """

    return f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Humanoid Fleet Preview</title>
      <style>
        :root {{
          --bg: #f1eee6;
          --surface: rgba(255, 251, 244, 0.92);
          --surface-strong: #f7eddc;
          --line: #d9c7ac;
          --ink: #1f2933;
          --muted: #576674;
          --accent: #9e432c;
          --accent-2: #d57b3e;
          --accent-soft: #f3d1aa;
          --ok: #245f45;
          --warn: #6d4f1f;
        }}
        * {{ box-sizing: border-box; }}
        body {{
          margin: 0;
          color: var(--ink);
          font-family: Georgia, "Noto Serif SC", "Songti SC", serif;
          background:
            radial-gradient(circle at top left, rgba(245, 208, 154, 0.95), transparent 28%),
            radial-gradient(circle at right 10% top 8%, rgba(220, 152, 108, 0.3), transparent 20%),
            linear-gradient(135deg, #f4f0e8, #e7dfd1 58%, #f8f3ea);
          min-height: 100vh;
        }}
        .wrap {{
          max-width: 1180px;
          margin: 0 auto;
          padding: 28px 18px 52px;
          transition: margin-left 0.22s ease, max-width 0.22s ease;
        }}
        body.workflow-panel-open .wrap {{
          margin-left: 84px;
          margin-right: 430px;
          max-width: 940px;
        }}
        body.sidebar-open .wrap {{
          margin-left: 360px;
          max-width: calc(1180px - 60px);
        }}
        body.sidebar-open.workflow-panel-open .wrap {{
          margin-left: 360px;
          margin-right: 430px;
          max-width: 940px;
        }}
        @media (max-width: 900px) {{
          body.sidebar-open .wrap {{
            margin-left: 0;
            max-width: 1180px;
          }}
        }}
        .hero {{
          padding: 28px;
          border-radius: 28px;
          border: 1px solid var(--line);
          background: linear-gradient(145deg, rgba(255, 251, 244, 0.88), rgba(247, 237, 220, 0.88));
          box-shadow: 0 26px 70px rgba(77, 50, 34, 0.08);
        }}
        h1, h2, h3 {{
          margin: 0;
          font-weight: 700;
        }}
        h1 {{
          font-size: clamp(30px, 5vw, 52px);
          line-height: 1.05;
        }}
        h2 {{
          font-size: 24px;
          margin-top: 8px;
        }}
        h3 {{
          font-size: 18px;
        }}
        p {{
          margin: 8px 0 0;
          line-height: 1.65;
        }}
        .subtitle {{
          max-width: 820px;
          color: var(--muted);
          margin-top: 14px;
        }}
        .examples {{
          display: flex;
          flex-wrap: wrap;
          gap: 10px;
          margin-top: 18px;
        }}
        .example-chip {{
          text-decoration: none;
          color: var(--ink);
          background: rgba(255, 247, 235, 0.96);
          border: 1px solid var(--line);
          padding: 10px 14px;
          border-radius: 999px;
          font-size: 14px;
        }}
        form {{
          display: grid;
          gap: 14px;
          margin-top: 22px;
        }}
        .chat-window {{
          display: grid;
          gap: 12px;
          margin-top: 22px;
          padding: 16px;
          min-height: 260px;
          max-height: 430px;
          overflow: auto;
          border: 1px solid var(--line);
          border-radius: 20px;
          background: rgba(255, 252, 246, 0.76);
        }}
        .chat-row {{
          display: flex;
        }}
        .chat-row.user {{
          justify-content: flex-end;
        }}
        .chat-row.assistant {{
          justify-content: flex-start;
        }}
        .bubble {{
          width: min(680px, 86%);
          padding: 12px 14px;
          border: 1px solid var(--line);
          border-radius: 18px;
          background: rgba(255, 247, 235, 0.9);
        }}
        .chat-row.user .bubble {{
          background: rgba(245, 214, 174, 0.96);
          border-color: #d0a06c;
        }}
        .bubble span {{
          display: block;
          margin-bottom: 4px;
          color: var(--accent);
          font-size: 12px;
          font-weight: 700;
        }}
        .bubble p {{
          margin: 0;
        }}
        .tts-player {{
          display: block;
          width: min(100%, 360px);
          height: 34px;
          margin-top: 10px;
        }}
        .chat-form textarea {{
          min-height: 96px;
        }}
        .input-actions {{
          display: flex;
          flex-wrap: wrap;
          gap: 10px;
          align-items: center;
        }}
        .voice-button {{
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 44px;
          height: 44px;
          padding: 0;
          border-radius: 50%;
          background: var(--surface-strong);
          color: var(--accent);
          border: 1px solid var(--line);
        }}
        .voice-button.recording {{
          background: var(--accent);
          color: white;
          border-color: var(--accent);
        }}
        .wave-wrap {{
          display: grid;
          grid-template-columns: minmax(160px, 280px) 1fr;
          gap: 12px;
          align-items: center;
          margin-top: 6px;
        }}
        #voice-wave {{
          width: 100%;
          height: 44px;
          border: 1px solid var(--line);
          border-radius: 14px;
          background: rgba(255, 252, 246, 0.86);
        }}
        #voice-status {{
          color: var(--muted);
          font-size: 14px;
        }}
        .confirm-form {{
          margin-top: 12px;
        }}
        textarea {{
          width: 100%;
          min-height: 122px;
          padding: 18px;
          border-radius: 20px;
          border: 1px solid var(--line);
          background: rgba(255, 252, 246, 0.95);
          color: var(--ink);
          font: inherit;
          font-size: 16px;
          line-height: 1.7;
          resize: vertical;
        }}
        button {{
          width: fit-content;
          padding: 12px 22px;
          border: 0;
          border-radius: 999px;
          background: linear-gradient(135deg, var(--accent), var(--accent-2));
          color: white;
          font: inherit;
          cursor: pointer;
        }}
        button:disabled {{
          cursor: not-allowed;
          opacity: 0.45;
          filter: grayscale(0.4);
        }}
        .summary-grid, .grid {{
          display: grid;
          gap: 18px;
          margin-top: 20px;
        }}
        .summary-grid {{
          grid-template-columns: 1.3fr 1fr;
        }}
        .grid.two {{
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }}
        .card, .hero-card {{
          border-radius: 24px;
          border: 1px solid var(--line);
          background: var(--surface);
          box-shadow: 0 12px 40px rgba(77, 50, 34, 0.05);
        }}
        .card {{
          padding: 22px;
        }}
        .hero-card {{
          padding: 24px;
        }}
        .highlight {{
          background: linear-gradient(145deg, rgba(162, 72, 47, 0.95), rgba(213, 123, 62, 0.88));
          color: white;
          border-color: rgba(162, 72, 47, 0.55);
        }}
        .highlight .pill {{
          background: rgba(255, 255, 255, 0.16);
          border-color: rgba(255, 255, 255, 0.2);
          color: white;
        }}
        .highlight .eyebrow, .highlight .muted {{
          color: rgba(255, 255, 255, 0.78);
        }}
        .eyebrow {{
          display: inline-block;
          font-size: 12px;
          letter-spacing: 0.12em;
          text-transform: uppercase;
          color: var(--accent);
        }}
        .muted {{
          color: var(--muted);
        }}
        .pill-row {{
          display: flex;
          flex-wrap: wrap;
          gap: 10px;
          margin-top: 16px;
        }}
        .pill, .badge {{
          display: inline-flex;
          align-items: center;
          border: 1px solid var(--line);
          border-radius: 999px;
          padding: 6px 10px;
          background: rgba(255, 247, 235, 0.9);
          font-size: 13px;
        }}
        .section-head {{
          display: flex;
          justify-content: space-between;
          gap: 16px;
          align-items: end;
          margin-bottom: 14px;
        }}
        .route-grid {{
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 14px;
        }}
        .route-step {{
          display: grid;
          grid-template-columns: 48px 1fr;
          gap: 14px;
          padding: 16px;
          border-radius: 18px;
          border: 1px solid var(--line);
          background: rgba(255, 248, 238, 0.78);
        }}
        .route-step.active {{
          background: linear-gradient(145deg, rgba(248, 215, 172, 0.8), rgba(255, 250, 242, 0.95));
          border-color: #c9905f;
        }}
        .route-step.reached {{
          border-style: dashed;
        }}
        .route-step.idle {{
          opacity: 0.62;
        }}
        .route-num {{
          width: 48px;
          height: 48px;
          display: grid;
          place-items: center;
          border-radius: 16px;
          background: var(--surface-strong);
          font-weight: 700;
        }}
        .entity-grid, .plain-list, .timeline {{
          margin: 0;
          padding: 0;
          list-style: none;
        }}
        .entity-grid {{
          display: grid;
          gap: 12px;
          margin-top: 14px;
        }}
        .entity-card {{
          display: flex;
          flex-wrap: wrap;
          gap: 10px;
          align-items: center;
          padding: 12px 14px;
          border: 1px solid var(--line);
          border-radius: 16px;
          background: rgba(255, 247, 235, 0.72);
        }}
        .entity-card span {{
          font-size: 14px;
          color: var(--muted);
        }}
        .entity-card.empty {{
          color: var(--muted);
        }}
        .timeline {{
          display: grid;
          gap: 12px;
        }}
        .timeline-item {{
          display: grid;
          grid-template-columns: 44px 1fr;
          gap: 14px;
        }}
        .timeline-dot {{
          width: 44px;
          height: 44px;
          display: grid;
          place-items: center;
          border-radius: 50%;
          background: linear-gradient(135deg, var(--accent), var(--accent-2));
          color: white;
          font-weight: 700;
        }}
        .timeline-body {{
          padding: 14px 16px;
          border-radius: 18px;
          border: 1px solid var(--line);
          background: rgba(255, 250, 242, 0.9);
        }}
        .timeline-head {{
          display: flex;
          justify-content: space-between;
          gap: 12px;
          align-items: center;
        }}
        .mini-grid {{
          display: grid;
          gap: 12px;
          grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        }}
        .mini-card, .catalog-card {{
          padding: 16px;
          border-radius: 18px;
          border: 1px solid var(--line);
          background: rgba(255, 247, 235, 0.74);
        }}
        .catalog-card.active {{
          border-color: #c9905f;
          box-shadow: inset 0 0 0 1px rgba(201, 144, 95, 0.25);
        }}
        pre {{
          margin: 12px 0 0;
          padding: 16px;
          border-radius: 16px;
          background: var(--surface-strong);
          overflow: auto;
          white-space: pre-wrap;
          line-height: 1.65;
        }}
        code {{
          background: var(--surface-strong);
          padding: 2px 6px;
          border-radius: 8px;
        }}
        .error-card {{
          border-color: #d5aaa0;
          color: #882d21;
        }}
        .confirmation-card {{
          border-color: #c9905f;
          background: rgba(255, 249, 240, 0.96);
        }}
        .workflow-state-panel {{
          position: fixed;
          top: 28px;
          right: 24px;
          bottom: 28px;
          width: 390px;
          z-index: 35;
          overflow: auto;
          padding: 20px;
          border: 1px solid var(--line);
          border-radius: 24px;
          background: rgba(255, 251, 244, 0.94);
          box-shadow: 0 20px 60px rgba(77, 50, 34, 0.12);
        }}
        .workflow-state-head h2 {{
          font-size: 22px;
          margin-top: 6px;
        }}
        .workflow-state-head p {{
          color: var(--muted);
          font-size: 13.5px;
        }}
        .workflow-state-meta {{
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin: 14px 0 16px;
        }}
        .workflow-state-meta span {{
          border: 1px solid var(--line);
          border-radius: 999px;
          background: rgba(255, 247, 235, 0.88);
          color: var(--accent);
          padding: 4px 8px;
          font-size: 11.5px;
        }}
        .workflow-state-list {{
          list-style: none;
          margin: 0;
          padding: 0;
          display: grid;
          gap: 12px;
        }}
        .workflow-state-node {{
          display: grid;
          grid-template-columns: 32px 1fr;
          gap: 10px;
          position: relative;
        }}
        .workflow-state-node:not(:last-child)::after {{
          content: "";
          position: absolute;
          left: 15px;
          top: 34px;
          bottom: -12px;
          width: 2px;
          background: var(--line);
        }}
        .workflow-node-index {{
          width: 32px;
          height: 32px;
          border-radius: 50%;
          display: grid;
          place-items: center;
          background: var(--surface-strong);
          color: var(--accent);
          border: 1px solid var(--line);
          font-size: 13px;
          font-weight: 700;
          z-index: 1;
        }}
        .workflow-state-node.queued .workflow-node-index {{
          background: var(--accent);
          color: white;
          border-color: var(--accent);
        }}
        .workflow-state-node.waiting .workflow-node-index {{
          background: #fffaf2;
        }}
        .workflow-state-node.awaiting .workflow-node-index {{
          background: rgba(245, 214, 174, 0.96);
          border-color: #d0a06c;
        }}
        .workflow-node-body {{
          padding: 12px;
          border: 1px solid var(--line);
          border-radius: 14px;
          background: rgba(255, 247, 235, 0.74);
        }}
        .workflow-node-head {{
          display: flex;
          justify-content: space-between;
          gap: 10px;
          align-items: start;
        }}
        .workflow-node-head h3 {{
          font-size: 14px;
          line-height: 1.35;
        }}
        .workflow-node-head code {{
          display: inline-block;
          margin-top: 5px;
          padding: 0;
          background: transparent;
          color: var(--accent);
          font-size: 11.5px;
        }}
        .workflow-status-badge {{
          flex: 0 0 auto;
          border-radius: 999px;
          padding: 3px 7px;
          background: rgba(36, 95, 69, 0.12);
          color: var(--ok);
          font-size: 11px;
          white-space: nowrap;
        }}
        .workflow-state-node.awaiting .workflow-status-badge {{
          background: rgba(109, 79, 31, 0.12);
          color: var(--warn);
        }}
        .workflow-node-meta {{
          display: grid;
          grid-template-columns: 58px 1fr;
          gap: 3px 8px;
          margin: 8px 0 0;
          font-size: 12px;
        }}
        .workflow-node-meta dt {{
          color: var(--muted);
        }}
        .workflow-node-meta dd {{
          margin: 0;
        }}
        .workflow-node-body details {{
          margin-top: 8px;
          font-size: 12px;
        }}
        .workflow-node-body summary {{
          color: var(--accent);
          cursor: pointer;
        }}
        .workflow-node-body pre {{
          margin-top: 6px;
          padding: 8px;
          border-radius: 10px;
          font-size: 11.5px;
          max-height: 150px;
        }}
        @media (max-width: 900px) {{
          .summary-grid, .grid.two, .route-grid {{
            grid-template-columns: 1fr;
          }}
          .section-head, .timeline-head {{
            display: block;
          }}
        }}
        @media (max-width: 1500px) {{
          body.workflow-panel-open .wrap,
          body.sidebar-open.workflow-panel-open .wrap {{
            margin-left: auto;
            margin-right: auto;
            max-width: 1180px;
            min-width: 0;
          }}
          .workflow-state-panel {{
            position: static;
            width: auto;
            margin: 18px;
            max-height: none;
          }}
        }}
        .hero-top {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }}
        .settings-button {{
          padding: 6px 14px;
          font-size: 13px;
          background: rgba(255, 247, 235, 0.9);
          color: var(--accent);
          border: 1px solid var(--line);
          border-radius: 999px;
          cursor: pointer;
        }}
        .settings-button:hover {{
          background: var(--surface-strong);
        }}
        .settings-backdrop {{
          position: fixed;
          inset: 0;
          background: rgba(31, 41, 51, 0.42);
          display: grid;
          place-items: center;
          z-index: 50;
          padding: 24px;
        }}
        .settings-backdrop[hidden] {{
          display: none;
        }}
        .settings-modal {{
          width: min(820px, 100%);
          max-height: min(86vh, 720px);
          background: var(--surface);
          border: 1px solid var(--line);
          border-radius: 22px;
          box-shadow: 0 30px 80px rgba(77, 50, 34, 0.18);
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }}
        .settings-head {{
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 18px 22px;
          border-bottom: 1px solid var(--line);
          background: linear-gradient(135deg, rgba(255, 251, 244, 0.95), rgba(247, 237, 220, 0.95));
        }}
        .settings-head h2 {{
          margin-top: 4px;
          font-size: 20px;
        }}
        .settings-close {{
          width: 36px;
          height: 36px;
          border-radius: 50%;
          background: transparent;
          color: var(--accent);
          font-size: 22px;
          line-height: 1;
          border: 1px solid var(--line);
          padding: 0;
        }}
        .settings-body {{
          padding: 18px 22px 24px;
          overflow: auto;
          display: grid;
          gap: 18px;
        }}
        .settings-section h3 {{
          margin-bottom: 8px;
          font-size: 16px;
        }}
        .settings-kv {{
          display: grid;
          grid-template-columns: 140px 1fr;
          gap: 6px 14px;
          margin: 0;
        }}
        .settings-kv dt {{
          color: var(--muted);
          font-size: 13px;
        }}
        .settings-kv dd {{
          margin: 0;
          font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
          font-size: 13px;
          word-break: break-all;
        }}
        .settings-list {{
          margin: 0;
          padding-left: 20px;
          color: var(--ink);
          line-height: 1.7;
        }}
        .settings-prompt {{
          margin: 8px 0 0;
          padding: 14px 16px;
          background: #1f2933;
          color: #f7eddc;
          border-radius: 14px;
          font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
          font-size: 12.5px;
          line-height: 1.55;
          white-space: pre-wrap;
          word-break: break-word;
          max-height: 280px;
          overflow: auto;
        }}

        /* Sidebar */
        .sidebar {{
          position: fixed;
          top: 0;
          left: 0;
          height: 100vh;
          width: 64px;
          z-index: 80;
        }}
        .sidebar-rail {{
          position: fixed;
          top: 0;
          left: 0;
          width: 64px;
          height: 100vh;
          background: linear-gradient(180deg, #2b1f17 0%, #1c1410 100%);
          color: #f7eddc;
          display: flex;
          flex-direction: column;
          align-items: center;
          padding: 16px 0 18px;
          gap: 6px;
          z-index: 80;
          box-shadow: 6px 0 24px rgba(31, 20, 12, 0.18);
        }}
        .sidebar-brand {{
          width: 40px;
          height: 40px;
          border-radius: 12px;
          display: grid;
          place-items: center;
          background: linear-gradient(135deg, var(--accent), var(--accent-2));
          margin-bottom: 14px;
          position: relative;
        }}
        .sidebar-brand-dot {{
          width: 14px;
          height: 14px;
          border-radius: 50%;
          background: #f7eddc;
          box-shadow: 0 0 0 4px rgba(247, 237, 220, 0.18);
        }}
        .sidebar-tab {{
          width: 44px;
          height: 44px;
          border-radius: 12px;
          background: transparent;
          color: rgba(247, 237, 220, 0.7);
          border: 1px solid transparent;
          padding: 0;
          display: grid;
          place-items: center;
          cursor: pointer;
          transition: background 0.18s ease, color 0.18s ease, border-color 0.18s ease, transform 0.18s ease;
          position: relative;
        }}
        .sidebar-tab:hover {{
          background: rgba(247, 237, 220, 0.08);
          color: #fff;
        }}
        .sidebar-tab.active {{
          background: rgba(247, 237, 220, 0.16);
          color: #fff;
          border-color: rgba(247, 237, 220, 0.24);
        }}
        .sidebar-tab.active::before {{
          content: "";
          position: absolute;
          left: -10px;
          top: 12px;
          bottom: 12px;
          width: 3px;
          border-radius: 2px;
          background: linear-gradient(180deg, var(--accent-2), #f7eddc);
        }}
        .sidebar-icon {{
          display: block;
          width: 20px;
          height: 20px;
          background-color: currentColor;
          mask-repeat: no-repeat;
          mask-position: center;
          mask-size: contain;
          -webkit-mask-repeat: no-repeat;
          -webkit-mask-position: center;
          -webkit-mask-size: contain;
        }}
        .sidebar-icon[data-icon="chat"] {{
          mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M4 5h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H8l-4 4z' fill='none' stroke='black' stroke-width='2' stroke-linejoin='round'/></svg>");
          -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M4 5h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H8l-4 4z' fill='none' stroke='black' stroke-width='2' stroke-linejoin='round'/></svg>");
        }}
        .sidebar-icon[data-icon="skills"] {{
          mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M12 2 4 6v6c0 5 3.5 8.5 8 10 4.5-1.5 8-5 8-10V6z' fill='none' stroke='black' stroke-width='2' stroke-linejoin='round'/></svg>");
          -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M12 2 4 6v6c0 5 3.5 8.5 8 10 4.5-1.5 8-5 8-10V6z' fill='none' stroke='black' stroke-width='2' stroke-linejoin='round'/></svg>");
        }}
        .sidebar-icon[data-icon="sop"] {{
          mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M8 3h8l3 3v18H5V3z' fill='none' stroke='black' stroke-width='2' stroke-linejoin='round'/><path d='M16 3v6h6M8 13h8M8 17h6' fill='none' stroke='black' stroke-width='2' stroke-linecap='round'/></svg>");
          -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M8 3h8l3 3v18H5V3z' fill='none' stroke='black' stroke-width='2' stroke-linejoin='round'/><path d='M16 3v6h6M8 13h8M8 17h6' fill='none' stroke='black' stroke-width='2' stroke-linecap='round'/></svg>");
        }}
        .sidebar-icon[data-icon="settings"] {{
          mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><circle cx='12' cy='12' r='3' fill='none' stroke='black' stroke-width='2'/><path d='M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.2.6.7 1 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z' fill='none' stroke='black' stroke-width='2' stroke-linejoin='round'/></svg>");
          -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><circle cx='12' cy='12' r='3' fill='none' stroke='black' stroke-width='2'/><path d='M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.2.6.7 1 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z' fill='none' stroke='black' stroke-width='2' stroke-linejoin='round'/></svg>");
        }}

        .sidebar-drawer {{
          position: fixed;
          top: 0;
          left: 64px;
          width: 296px;
          height: 100vh;
          background: var(--surface);
          border-right: 1px solid var(--line);
          transform: translateX(-110%);
          transition: transform 0.22s ease;
          z-index: 70;
          display: flex;
          flex-direction: column;
          box-shadow: 0 12px 40px rgba(77, 50, 34, 0.08);
        }}
        body.sidebar-open .sidebar-drawer {{
          transform: translateX(0);
        }}
        .sidebar-drawer-close {{
          position: absolute;
          top: 14px;
          right: 12px;
          width: 30px;
          height: 30px;
          border-radius: 50%;
          background: var(--surface-strong);
          color: var(--accent);
          font-size: 18px;
          line-height: 1;
          padding: 0;
          border: 1px solid var(--line);
        }}
        .sidebar-drawer-body {{
          padding: 22px 20px 28px;
          overflow: auto;
          height: 100%;
        }}
        .sidebar-panel-head h3 {{
          font-size: 18px;
          margin-bottom: 4px;
        }}
        .sidebar-panel-head {{
          margin-bottom: 14px;
        }}
        .sidebar-panel[hidden] {{
          display: none;
        }}
        .sidebar-kv {{
          display: grid;
          grid-template-columns: 110px 1fr;
          gap: 6px 12px;
          margin: 0 0 14px;
        }}
        .sidebar-kv dt {{
          color: var(--muted);
          font-size: 13px;
        }}
        .sidebar-kv dd {{
          margin: 0;
          font-size: 13.5px;
        }}
        .muted.small {{
          font-size: 12.5px;
        }}
        .skill-filter {{
          display: flex;
          align-items: center;
          gap: 8px;
          font-size: 13px;
          color: var(--muted);
          margin-bottom: 12px;
        }}
        .skill-filter select {{
          flex: 1;
          padding: 6px 10px;
          border-radius: 10px;
          border: 1px solid var(--line);
          background: rgba(255, 252, 246, 0.95);
          color: var(--ink);
          font: inherit;
          font-size: 13px;
        }}
        .asset-toolbar {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
          margin-bottom: 12px;
        }}
        .asset-toolbar .skill-filter {{
          flex: 1;
          margin-bottom: 0;
        }}
        .asset-add-button,
        .asset-action-button,
        .skill-editor button {{
          border-radius: 8px;
          font-size: 12.5px;
          padding: 7px 10px;
        }}
        .asset-add-button {{
          white-space: nowrap;
        }}
        .asset-action-row {{
          display: flex;
          gap: 6px;
          margin-top: 8px;
        }}
        .asset-action-button {{
          flex: 1;
          background: var(--surface-strong);
          color: var(--accent);
          border: 1px solid var(--line);
        }}
        .asset-action-button.danger {{
          color: #9e432c;
        }}
        .skill-editor {{
          border: 1px solid var(--line);
          background: rgba(255, 252, 246, 0.92);
          border-radius: 12px;
          padding: 12px;
          display: grid;
          gap: 9px;
          margin-bottom: 12px;
        }}
        .skill-editor[hidden] {{
          display: none;
        }}
        .skill-editor label {{
          display: grid;
          gap: 4px;
          font-size: 12px;
          color: var(--muted);
        }}
        .skill-editor input,
        .skill-editor select,
        .skill-editor textarea {{
          width: 100%;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: #fffaf2;
          color: var(--ink);
          font: inherit;
          font-size: 12.5px;
          padding: 7px 9px;
        }}
        .skill-editor textarea {{
          min-height: 62px;
          resize: vertical;
        }}
        .skill-editor-actions {{
          display: flex;
          gap: 8px;
        }}
        .skill-count {{
          display: inline-flex;
          align-items: center;
          padding: 4px 10px;
          border-radius: 999px;
          background: var(--surface-strong);
          color: var(--accent);
          font-size: 12px;
          font-weight: 600;
        }}
        .skill-grid {{
          display: grid;
          gap: 10px;
        }}
        .skill-card {{
          padding: 12px 14px;
          border-radius: 14px;
          border: 1px solid var(--line);
          background: rgba(255, 247, 235, 0.78);
        }}
        .skill-card header {{
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 8px;
          margin-bottom: 6px;
        }}
        .skill-card h4 {{
          margin: 0;
          font-size: 14.5px;
        }}
        .skill-card code {{
          font-size: 11.5px;
          color: var(--accent);
          background: transparent;
          padding: 0;
        }}
        .skill-card p {{
          margin: 4px 0 8px;
          font-size: 12.8px;
          line-height: 1.5;
        }}
        .skill-meta {{
          display: grid;
          grid-template-columns: 70px 1fr;
          gap: 3px 8px;
          margin: 0 0 6px;
        }}
        .skill-meta dt {{
          font-size: 11.5px;
          color: var(--muted);
        }}
        .skill-meta dd {{
          margin: 0;
          font-size: 12px;
        }}
        .skill-status {{
          display: inline-flex;
          padding: 1px 8px;
          border-radius: 999px;
          font-size: 11px;
        }}
        .skill-status.status-registered {{
          background: rgba(36, 95, 69, 0.14);
          color: #245f45;
        }}
        .skill-card details {{
          font-size: 12.5px;
        }}
        .skill-card details summary {{
          cursor: pointer;
          color: var(--accent);
          font-size: 12px;
          user-select: none;
        }}
        .skill-card pre {{
          margin: 6px 0 0;
          padding: 8px 10px;
          background: #1f2933;
          color: #f7eddc;
          border-radius: 10px;
          font-size: 11.5px;
          white-space: pre-wrap;
          word-break: break-word;
        }}
        .sop-card {{
          padding: 12px 14px;
          border-radius: 14px;
          border: 1px solid var(--line);
          background: rgba(255, 247, 235, 0.78);
        }}
        .sop-card h4 {{
          margin: 0 0 4px;
          font-size: 14.5px;
        }}
        .sop-card code {{
          display: inline-block;
          font-size: 11.5px;
          color: var(--accent);
          background: transparent;
          padding: 0;
          margin-bottom: 6px;
        }}
        .sop-steps {{
          margin: 8px 0 0;
          padding-left: 18px;
          font-size: 12.5px;
          line-height: 1.55;
        }}
        .sop-steps li::marker {{
          color: var(--accent);
        }}
        .settings-open-button {{
          padding: 10px 16px;
          font-size: 13.5px;
        }}
        .sidebar-tab-tooltip {{
          position: fixed;
          background: #1f2933;
          color: #f7eddc;
          font-size: 12px;
          padding: 4px 10px;
          border-radius: 8px;
          pointer-events: none;
          z-index: 60;
          opacity: 0;
          transform: translateY(-50%);
          transition: opacity 0.16s ease;
          white-space: nowrap;
        }}
        .sidebar-tab-tooltip.show {{
          opacity: 1;
        }}
      </style>
    </head>
    <body class="{body_class}">
      <main class="wrap">
        <section class="hero">
          <div class="hero-top">
            <span class="eyebrow">Humanoid Fleet Control</span>
            <button type="button" class="settings-button" id="settings-button" title="查看设置与提示词">⚙ 设置</button>
          </div>
          <h1>多轮任务确认台</h1>
          <p class="subtitle">先通过对话理解用户意图；当任务命中已审核 SOP 后，系统会暂停在确认节点。用户确认后才生成机器人分工和执行计划。</p>
          <div class="examples">
            {_render_example_links()}
          </div>
          <div class="chat-window">
            {_render_chat_messages(messages)}
          </div>
          <form class="chat-form" id="chat-form" method="post">
            <input type="hidden" name="history" value="{history_value}">
            <input type="hidden" name="action" value="send">
            <label for="message"><strong>继续对话</strong></label>
            <textarea id="message" name="message" placeholder="例如：去拿杯冰可乐给3号桌顾客" autofocus></textarea>
            <div class="input-actions">
              <button type="submit">发送</button>
              <button class="voice-button" id="voice-button" type="button" title="语音输入" aria-label="语音输入">MIC</button>
            </div>
            <div class="wave-wrap">
              <canvas id="voice-wave" width="280" height="44"></canvas>
              <span id="voice-status">点击 MIC，或按住 Shift+Space 语音输入</span>
            </div>
          </form>
          <form class="confirm-form" method="post">
            <input type="hidden" name="history" value="{history_value}">
            <input type="hidden" name="action" value="confirm">
            <button type="submit" {'disabled' if not review or review.get('state') != 'awaiting_confirmation' else ''}>确认并生成执行计划</button>
          </form>
        </section>
        {pending_confirmation}
        {content}
      </main>
      {workflow_state_panel}
      {_render_settings_modal()}
      {_render_sidebar()}
      <script>
        let audioContext = null;
        let recordingContext = null;
        let recordingStream = null;
        let recordingSource = null;
        let recordingProcessor = null;
        let recordingAnalyser = null;
        let recordingBuffers = [];
        let recordingSampleRate = 16000;
        let waveformFrame = null;
        let submitting = false;
        let ttsPlaybackId = 0;
        let activeTtsSource = null;
        let activeTtsAbort = null;
        let activeTtsAborts = new Set();
        let pushToTalkActive = false;

        function getAudioContext() {{
          const AudioContextClass = window.AudioContext || window.webkitAudioContext;
          if (!AudioContextClass) {{
            return null;
          }}
          if (!audioContext) {{
            audioContext = new AudioContextClass();
          }}
          if (audioContext.state === "suspended") {{
            audioContext.resume();
          }}
          return audioContext;
        }}

        function splitTtsText(text) {{
          const normalized = text
            .replace(/[，。！？；：、,.!?;:（）()《》“”"']/g, " ")
            .replace(/\s+/g, " ")
            .trim();
          if (!normalized) {{
            return [];
          }}
          const chunks = [];
          const firstChunkSize = 8;
          const chunkSize = 24;
          chunks.push(normalized.slice(0, firstChunkSize));
          for (let index = firstChunkSize; index < normalized.length; index += chunkSize) {{
            chunks.push(normalized.slice(index, index + chunkSize));
          }}
          return chunks.filter(Boolean);
        }}

        function stopTtsPlayback() {{
          ttsPlaybackId += 1;
          if (activeTtsAbort) {{
            activeTtsAbort.abort();
            activeTtsAbort = null;
          }}
          activeTtsAborts.forEach((controller) => controller.abort());
          activeTtsAborts.clear();
          if (activeTtsSource) {{
            try {{
              activeTtsSource.stop();
            }} catch (error) {{
              // Already stopped.
            }}
            activeTtsSource = null;
          }}
          document.querySelectorAll(".tts-player").forEach((player) => {{
            player.pause();
            player.currentTime = 0;
          }});
        }}

        async function fetchTtsChunk(text, playbackId) {{
          const context = getAudioContext();
          if (!context || !text) {{
            return null;
          }}
          const abortController = new AbortController();
          activeTtsAbort = abortController;
          activeTtsAborts.add(abortController);
          const response = await fetch(`/tts.wav?text=${{encodeURIComponent(text)}}`, {{
            signal: abortController.signal,
          }});
          activeTtsAborts.delete(abortController);
          if (activeTtsAbort === abortController) {{
            activeTtsAbort = null;
          }}
          if (playbackId !== ttsPlaybackId) {{
            return null;
          }}
          if (!response.ok) {{
            return null;
          }}
          const buffer = await response.arrayBuffer();
          return await context.decodeAudioData(buffer);
        }}

        async function playAudioBuffer(audioBuffer, playbackId) {{
          if (playbackId !== ttsPlaybackId) {{
            return false;
          }}
          const context = getAudioContext();
          if (!context) {{
            return false;
          }}
          await new Promise((resolve) => {{
            const source = context.createBufferSource();
            activeTtsSource = source;
            source.buffer = audioBuffer;
            source.connect(context.destination);
            source.onended = () => {{
              if (activeTtsSource === source) {{
                activeTtsSource = null;
              }}
              resolve();
            }};
            source.start();
          }});
          return true;
        }}

        async function playLatestAssistantTts() {{
          const latest = Array.from(document.querySelectorAll(".tts-player[data-autoplay='true']")).pop();
          if (!latest || latest.dataset.played === "true") {{
            return;
          }}
          latest.dataset.played = "true";
          ttsPlaybackId += 1;
          const playbackId = ttsPlaybackId;
          const chunks = splitTtsText(latest.dataset.ttsText || "");
          try {{
            if (chunks.length) {{
              const audioBuffers = [];
              audioBuffers[0] = fetchTtsChunk(chunks[0], playbackId);
              for (let index = 0; index < chunks.length; index += 1) {{
                if (playbackId !== ttsPlaybackId || recordingStream) {{
                  return;
                }}
                const audioBuffer = await audioBuffers[index];
                if (index + 1 < chunks.length && !audioBuffers[index + 1]) {{
                  audioBuffers[index + 1] = fetchTtsChunk(chunks[index + 1], playbackId);
                }}
                if (audioBuffer) {{
                  await playAudioBuffer(audioBuffer, playbackId);
                }}
              }}
              return;
            }}
          }} catch (error) {{
            if (error.name !== "AbortError") {{
              console.warn("TTS chunk playback failed", error);
            }}
          }}
          if (playbackId === ttsPlaybackId && !recordingStream) {{
            latest.play().catch(() => undefined);
          }}
        }}

        function scrollChatToBottom() {{
          const chatWindow = document.querySelector(".chat-window");
          if (chatWindow) {{
            chatWindow.scrollTop = chatWindow.scrollHeight;
            requestAnimationFrame(() => {{
              chatWindow.scrollTop = chatWindow.scrollHeight;
            }});
          }}
        }}

        function updateFromHtml(htmlText) {{
          const parser = new DOMParser();
          const doc = parser.parseFromString(htmlText, "text/html");
          const nextMain = doc.querySelector("main");
          const currentMain = document.querySelector("main");
          if (nextMain && currentMain) {{
            currentMain.innerHTML = nextMain.innerHTML;
            bindUi();
            scrollChatToBottom();
            playLatestAssistantTts();
          }}
        }}

        async function submitPostForm(form) {{
          if (submitting) {{
            return;
          }}
          stopTtsPlayback();
          submitting = true;
          getAudioContext();
          try {{
            const response = await fetch("/", {{
              method: "POST",
              headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
              body: new URLSearchParams(new FormData(form)),
            }});
            updateFromHtml(await response.text());
          }} finally {{
            submitting = false;
          }}
        }}

        async function submitChatForm(form) {{
          const input = form.querySelector("#message");
          if (!input || !input.value.trim()) {{
            return;
          }}
          await submitPostForm(form);
        }}

        function drawWaveform() {{
          const canvas = document.getElementById("voice-wave");
          if (!canvas || !recordingAnalyser) {{
            return;
          }}
          const context = canvas.getContext("2d");
          const data = new Uint8Array(recordingAnalyser.fftSize);
          recordingAnalyser.getByteTimeDomainData(data);
          context.clearRect(0, 0, canvas.width, canvas.height);
          context.strokeStyle = "#9e432c";
          context.lineWidth = 2;
          context.beginPath();
          for (let index = 0; index < data.length; index += 1) {{
            const x = (index / (data.length - 1)) * canvas.width;
            const y = (data[index] / 255) * canvas.height;
            if (index === 0) {{
              context.moveTo(x, y);
            }} else {{
              context.lineTo(x, y);
            }}
          }}
          context.stroke();
          waveformFrame = requestAnimationFrame(drawWaveform);
        }}

        function mergeBuffers(buffers) {{
          const totalLength = buffers.reduce((total, buffer) => total + buffer.length, 0);
          const merged = new Float32Array(totalLength);
          let offset = 0;
          for (const buffer of buffers) {{
            merged.set(buffer, offset);
            offset += buffer.length;
          }}
          return merged;
        }}

        function encodeWav(samples, sampleRate) {{
          const buffer = new ArrayBuffer(44 + samples.length * 2);
          const view = new DataView(buffer);
          function writeString(offset, value) {{
            for (let index = 0; index < value.length; index += 1) {{
              view.setUint8(offset + index, value.charCodeAt(index));
            }}
          }}
          writeString(0, "RIFF");
          view.setUint32(4, 36 + samples.length * 2, true);
          writeString(8, "WAVE");
          writeString(12, "fmt ");
          view.setUint32(16, 16, true);
          view.setUint16(20, 1, true);
          view.setUint16(22, 1, true);
          view.setUint32(24, sampleRate, true);
          view.setUint32(28, sampleRate * 2, true);
          view.setUint16(32, 2, true);
          view.setUint16(34, 16, true);
          writeString(36, "data");
          view.setUint32(40, samples.length * 2, true);
          let offset = 44;
          for (const sample of samples) {{
            const clamped = Math.max(-1, Math.min(1, sample));
            view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
            offset += 2;
          }}
          return new Blob([view], {{type: "audio/wav"}});
        }}

        async function submitAsr(blob) {{
          const status = document.getElementById("voice-status");
          const form = document.getElementById("chat-form");
          const input = document.getElementById("message");
          if (status) {{
            status.textContent = "正在识别语音...";
          }}
          const data = new FormData();
          data.append("audio", blob, "voice.wav");
          const response = await fetch("/asr", {{method: "POST", body: data}});
          const payload = await response.json();
          const text = (payload.text || "").trim();
          if (text && input && form) {{
            input.value = text;
            if (status) {{
              status.textContent = `识别到：${{text}}`;
            }}
            await submitChatForm(form);
          }} else if (status) {{
            status.textContent = "没有识别到内容，可以再试一次";
          }}
        }}

        async function startRecording(trigger = "button") {{
          if (recordingStream) {{
            return;
          }}
          const status = document.getElementById("voice-status");
          const button = document.getElementById("voice-button");
          stopTtsPlayback();
          getAudioContext();
          recordingStream = await navigator.mediaDevices.getUserMedia({{
            audio: {{
              echoCancellation: true,
              noiseSuppression: true,
              autoGainControl: true,
            }}
          }});
          const AudioContextClass = window.AudioContext || window.webkitAudioContext;
          recordingContext = new AudioContextClass();
          recordingSampleRate = recordingContext.sampleRate;
          recordingBuffers = [];
          recordingSource = recordingContext.createMediaStreamSource(recordingStream);
          recordingAnalyser = recordingContext.createAnalyser();
          recordingAnalyser.fftSize = 1024;
          recordingProcessor = recordingContext.createScriptProcessor(4096, 1, 1);
          recordingProcessor.onaudioprocess = (event) => {{
            recordingBuffers.push(new Float32Array(event.inputBuffer.getChannelData(0)));
          }};
          recordingSource.connect(recordingAnalyser);
          recordingSource.connect(recordingProcessor);
          recordingProcessor.connect(recordingContext.createGain());
          if (button) {{
            button.classList.add("recording");
            button.textContent = "STOP";
          }}
          if (status) {{
            status.textContent = trigger === "hotkey"
              ? "按住 Shift+Space 说话，松开结束"
              : "已打断播报，正在听；再次点击结束";
          }}
          drawWaveform();
        }}

        async function stopRecording() {{
          if (!recordingStream) {{
            return;
          }}
          const button = document.getElementById("voice-button");
          if (waveformFrame) {{
            cancelAnimationFrame(waveformFrame);
            waveformFrame = null;
          }}
          if (recordingProcessor) {{
            recordingProcessor.disconnect();
            recordingProcessor.onaudioprocess = null;
          }}
          if (recordingSource) {{
            recordingSource.disconnect();
          }}
          if (recordingStream) {{
            recordingStream.getTracks().forEach((track) => track.stop());
          }}
          if (recordingContext) {{
            recordingContext.close();
          }}
          if (button) {{
            button.classList.remove("recording");
            button.textContent = "MIC";
          }}
          const blob = encodeWav(mergeBuffers(recordingBuffers), recordingSampleRate);
          recordingContext = null;
          recordingStream = null;
          recordingSource = null;
          recordingProcessor = null;
          recordingAnalyser = null;
          recordingBuffers = [];
          await submitAsr(blob);
        }}

        function isPushToTalkStart(event) {{
          return event.shiftKey && (event.code === "Space" || event.key === " ");
        }}

        function isPushToTalkStop(event) {{
          return pushToTalkActive && (event.code === "Space" || event.key === "Shift");
        }}

        function bindUi() {{
          const messageInput = document.getElementById("message");
          const chatForm = document.getElementById("chat-form");
          const voiceButton = document.getElementById("voice-button");
          const confirmForm = document.querySelector(".confirm-form");
          const settingsButton = document.getElementById("settings-button");
          if (messageInput && chatForm) {{
            messageInput.addEventListener("keydown", (event) => {{
              if (event.key === "Enter" && !event.shiftKey) {{
                event.preventDefault();
                submitChatForm(chatForm);
              }}
            }});
            chatForm.addEventListener("submit", (event) => {{
              event.preventDefault();
              submitChatForm(chatForm);
            }});
          }}
          if (confirmForm) {{
            confirmForm.addEventListener("submit", (event) => {{
              event.preventDefault();
              submitPostForm(confirmForm);
            }});
          }}
          if (voiceButton) {{
            voiceButton.addEventListener("click", async () => {{
              try {{
                if (recordingStream) {{
                  pushToTalkActive = false;
                  await stopRecording();
                }} else {{
                  await startRecording();
                }}
              }} catch (error) {{
                const status = document.getElementById("voice-status");
                if (status) {{
                  status.textContent = `语音输入失败：${{error.message || error}}`;
                }}
              }}
            }});
          }}
          if (settingsButton) {{
            settingsButton.addEventListener("click", openSettings);
          }}
          if (!document.body.dataset.pushToTalkBound) {{
            document.body.dataset.pushToTalkBound = "true";
            window.addEventListener("keydown", async (event) => {{
              if (!isPushToTalkStart(event) || pushToTalkActive || recordingStream) {{
                return;
              }}
              event.preventDefault();
              pushToTalkActive = true;
              try {{
                await startRecording("hotkey");
              }} catch (error) {{
                pushToTalkActive = false;
                const status = document.getElementById("voice-status");
                if (status) {{
                  status.textContent = `语音输入失败：${{error.message || error}}`;
                }}
              }}
            }});
            window.addEventListener("keyup", async (event) => {{
              if (!isPushToTalkStop(event)) {{
                return;
              }}
              event.preventDefault();
              pushToTalkActive = false;
              await stopRecording();
            }});
          }}
        }}

        let settingsLoaded = false;
        let settingsLoading = false;

        function fillKvList(dl, values) {{
          const dts = dl.querySelectorAll("dt");
          dts.forEach((dt) => {{
            const key = dt.textContent.trim();
            const dd = dt.nextElementSibling;
            if (dd) {{
              dd.textContent = values[key] ?? "—";
            }}
          }});
        }}

        async function loadSettings() {{
          if (settingsLoaded || settingsLoading) {{
            return;
          }}
          settingsLoading = true;
          const backdrop = document.getElementById("settings-backdrop");
          if (backdrop) {{
            backdrop.dataset.loading = "true";
          }}
          try {{
            const response = await fetch("/settings.json");
            if (!response.ok) {{
              throw new Error(`status ${{response.status}}`);
            }}
            const data = await response.json();
            const llm = data.llm || {{}};
            fillKvList(document.getElementById("settings-llm"), {{
              backend: llm.backend,
              model: llm.model,
              host: llm.host,
              timeout: llm.timeout_seconds ? `${{llm.timeout_seconds}}s` : "—",
              thinking: llm.thinking_disabled === false ? "enabled" : "disabled",
            }});
            const speech = data.speech || {{}};
            fillKvList(document.getElementById("settings-speech"), {{
              "service url": speech.service_url,
              "ASR model": speech.asr_model,
              "TTS model": speech.tts_model,
            }});
            const policy = document.getElementById("settings-policy");
            if (policy) {{
              policy.innerHTML = (data.execution_policy || [])
                .map((line) => `<li>${{line.replace(/</g, "&lt;")}}</li>`)
                .join("");
            }}
            const prompts = data.prompts || {{}};
            const setPre = (id, text) => {{
              const node = document.getElementById(id);
              if (node) {{
                node.textContent = text || "";
              }}
            }};
            setPre("settings-prompt-template", prompts.intent_parser_template);
            setPre("settings-prompt-sample", prompts.intent_parser_sample);
            setPre("settings-chat-prompt", prompts.chat_system_prompt);
            settingsLoaded = true;
          }} catch (error) {{
            console.warn("settings load failed", error);
            const sample = document.getElementById("settings-prompt-sample");
            if (sample) {{
              sample.textContent = `无法加载设置：${{error.message || error}}`;
            }}
          }} finally {{
            settingsLoading = false;
            if (backdrop) {{
              backdrop.dataset.loading = "false";
            }}
          }}
        }}

        function openSettings() {{
          const backdrop = document.getElementById("settings-backdrop");
          if (!backdrop) {{
            return;
          }}
          backdrop.hidden = false;
          loadSettings();
        }}

        function closeSettings() {{
          const backdrop = document.getElementById("settings-backdrop");
          if (backdrop) {{
            backdrop.hidden = true;
          }}
        }}

        document.getElementById("settings-close")?.addEventListener("click", closeSettings);
        document.getElementById("settings-backdrop")?.addEventListener("click", (event) => {{
          if (event.target.id === "settings-backdrop") {{
            closeSettings();
          }}
        }});
        window.addEventListener("keydown", (event) => {{
          if (event.key === "Escape") {{
            closeSettings();
          }}
        }});

        // --- Sidebar logic ---
        const sidebarTabs = document.querySelectorAll(".sidebar-tab");
        const sidebarPanels = document.querySelectorAll(".sidebar-panel");
        const sidebarDrawerClose = document.getElementById("sidebar-drawer-close");
        const sidebarTooltip = document.createElement("div");
        sidebarTooltip.className = "sidebar-tab-tooltip";
        document.body.appendChild(sidebarTooltip);

        let activeTabId = "chat";
        let skillsLoaded = false;
        let skillCatalog = [];
        let sopsLoaded = false;

        function setActiveTab(tabId) {{
          activeTabId = tabId;
          sidebarTabs.forEach((tab) => {{
            tab.classList.toggle("active", tab.dataset.tab === tabId);
          }});
          sidebarPanels.forEach((panel) => {{
            panel.hidden = panel.dataset.panel !== tabId;
          }});
          document.body.classList.add("sidebar-open");
          const currentLabel = document.getElementById("side-current-tab");
          if (currentLabel) {{
            const found = Array.from(sidebarTabs).find((t) => t.dataset.tab === tabId);
            currentLabel.textContent = found ? found.getAttribute("title") : tabId;
          }}
          if (tabId === "skills") {{
            loadSkillCatalog();
          }} else if (tabId === "sop") {{
            loadSopCatalog();
          }} else if (tabId === "settings") {{
            loadSettings();
          }}
        }}

        function escapeHtml(value) {{
          return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
        }}

        function schemaToText(value) {{
          try {{
            return JSON.stringify(value || {{}}, null, 2);
          }} catch (error) {{
            return "{{}}";
          }}
        }}

        function parseSchemaField(id) {{
          const raw = document.getElementById(id)?.value.trim() || "{{}}";
          const parsed = JSON.parse(raw);
          if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {{
            throw new Error("schema 必须是 JSON object");
          }}
          return parsed;
        }}

        function renderSopCards(sops) {{
          const grid = document.getElementById("side-sop-grid");
          const count = document.getElementById("side-sop-count");
          if (count) {{
            count.textContent = `${{sops.length}} 项`;
          }}
          if (!grid) {{
            return;
          }}
          grid.innerHTML = sops.map((sop) => {{
            const steps = (sop.workflow_steps || [])
              .map((step) => `<li>${{escapeHtml(step)}}</li>`)
              .join("");
            return `
              <article class="sop-card">
                <h4>${{escapeHtml(sop.name)}}</h4>
                <code>${{escapeHtml(sop.template_id)}}</code>
                <dl class="skill-meta">
                  <dt>意图类型</dt><dd>${{escapeHtml(sop.intent_type)}}</dd>
                  <dt>步骤数</dt><dd>${{escapeHtml(sop.step_count)}}</dd>
                  <dt>多机器人</dt><dd>${{sop.supports_multi_robot ? "支持" : "不支持"}}</dd>
                </dl>
                <ol class="sop-steps">${{steps}}</ol>
              </article>
            `;
          }}).join("");
        }}

        async function loadSopCatalog() {{
          if (sopsLoaded) {{
            return;
          }}
          try {{
            const response = await fetch("/api/sops");
            if (!response.ok) {{
              throw new Error(`status ${{response.status}}`);
            }}
            const data = await response.json();
            renderSopCards(data.sops || []);
            sopsLoaded = true;
          }} catch (error) {{
            console.warn("sops load failed", error);
          }}
        }}

        function renderSkillCards() {{
          const grid = document.getElementById("side-skill-grid");
          const count = document.getElementById("side-skill-count");
          const filter = document.getElementById("side-skill-filter")?.value || "";
          const visibleSkills = filter
            ? skillCatalog.filter((skill) => skill.category === filter)
            : skillCatalog;
          if (count) {{
            count.textContent = `${{visibleSkills.length}} / ${{skillCatalog.length}} 项`;
          }}
          if (!grid) {{
            return;
          }}
          grid.innerHTML = visibleSkills.map((skill) => {{
            const io = schemaToText({{ input: skill.input_schema, output: skill.output_schema }});
            const statusClass = escapeHtml(skill.status || "registered");
            return `
              <article class="skill-card" data-category="${{escapeHtml(skill.category)}}">
                <header>
                  <h4>${{escapeHtml(skill.name_zh || skill.name)}}</h4>
                  <code>${{escapeHtml(skill.id)}}</code>
                </header>
                <p>${{escapeHtml(skill.summary)}}</p>
                <dl class="skill-meta">
                  <dt>类别</dt><dd>${{escapeHtml(skill.category)}}</dd>
                  <dt>归属机器人</dt><dd>${{escapeHtml(skill.owner_robot)}}</dd>
                  <dt>状态</dt><dd><span class="skill-status status-${{statusClass}}">${{escapeHtml(skill.status)}}</span></dd>
                </dl>
                <details>
                  <summary>输入 / 输出 schema</summary>
                  <pre>${{escapeHtml(io)}}</pre>
                </details>
                <div class="asset-action-row">
                  <button type="button" class="asset-action-button" data-edit-skill="${{escapeHtml(skill.id)}}">修改</button>
                  <button type="button" class="asset-action-button danger" data-delete-skill="${{escapeHtml(skill.id)}}">删除</button>
                </div>
              </article>
            `;
          }}).join("");
        }}

        function resetSkillEditor() {{
          const form = document.getElementById("skill-editor");
          if (!form) {{
            return;
          }}
          form.reset();
          document.getElementById("skill-editing-id").value = "";
          document.getElementById("skill-input-schema").value = "{{\\n  \\"parameter\\": \\"string\\"\\n}}";
          document.getElementById("skill-output-schema").value = "{{\\n  \\"ok\\": \\"bool\\"\\n}}";
          form.hidden = false;
          document.getElementById("skill-id")?.removeAttribute("readonly");
          document.getElementById("skill-id")?.focus();
        }}

        function openSkillEditor(skillId = "") {{
          const form = document.getElementById("skill-editor");
          if (!form) {{
            return;
          }}
          if (!skillId) {{
            resetSkillEditor();
            return;
          }}
          const skill = skillCatalog.find((item) => item.id === skillId);
          if (!skill) {{
            return;
          }}
          form.hidden = false;
          document.getElementById("skill-editing-id").value = skill.id;
          document.getElementById("skill-id").value = skill.id;
          document.getElementById("skill-id").setAttribute("readonly", "readonly");
          document.getElementById("skill-name").value = skill.name || "";
          document.getElementById("skill-name-zh").value = skill.name_zh || "";
          document.getElementById("skill-category").value = skill.category || "agent";
          document.getElementById("skill-owner").value = skill.owner_robot || "";
          document.getElementById("skill-status").value = skill.status || "registered";
          document.getElementById("skill-summary").value = skill.summary || "";
          document.getElementById("skill-input-schema").value = schemaToText(skill.input_schema);
          document.getElementById("skill-output-schema").value = schemaToText(skill.output_schema);
          document.getElementById("skill-name-zh")?.focus();
        }}

        async function saveSkill(event) {{
          event.preventDefault();
          const editingId = document.getElementById("skill-editing-id")?.value || "";
          const payload = {{
            id: document.getElementById("skill-id")?.value.trim(),
            name: document.getElementById("skill-name")?.value.trim(),
            name_zh: document.getElementById("skill-name-zh")?.value.trim(),
            category: document.getElementById("skill-category")?.value,
            owner_robot: document.getElementById("skill-owner")?.value.trim(),
            status: document.getElementById("skill-status")?.value.trim(),
            summary: document.getElementById("skill-summary")?.value.trim(),
            input_schema: parseSchemaField("skill-input-schema"),
            output_schema: parseSchemaField("skill-output-schema"),
          }};
          const response = await fetch(editingId ? `/api/skills/${{encodeURIComponent(editingId)}}` : "/api/skills", {{
            method: editingId ? "PUT" : "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify(payload),
          }});
          const data = await response.json();
          if (!response.ok) {{
            throw new Error(data.error || `status ${{response.status}}`);
          }}
          await loadSkillCatalog(true);
          document.getElementById("skill-editor").hidden = true;
        }}

        async function deleteSkill(skillId) {{
          const response = await fetch(`/api/skills/${{encodeURIComponent(skillId)}}`, {{method: "DELETE"}});
          const data = await response.json();
          if (!response.ok) {{
            throw new Error(data.error || `status ${{response.status}}`);
          }}
          await loadSkillCatalog(true);
        }}

        async function loadSkillCatalog() {{
          const force = arguments[0] === true;
          if (skillsLoaded && !force) {{
            return;
          }}
          try {{
            const response = await fetch("/api/skills");
            if (!response.ok) {{
              throw new Error(`status ${{response.status}}`);
            }}
            const data = await response.json();
            skillCatalog = data.skills || [];
            renderSkillCards();
            skillsLoaded = true;
          }} catch (error) {{
            console.warn("skills load failed", error);
          }}
        }}

        sidebarTabs.forEach((tab) => {{
          tab.addEventListener("mouseenter", (event) => {{
            const target = event.currentTarget;
            const label = target.getAttribute("title") || target.dataset.tab;
            const rect = target.getBoundingClientRect();
            sidebarTooltip.textContent = label;
            sidebarTooltip.style.top = `${{rect.top + rect.height / 2}}px`;
            sidebarTooltip.style.left = `${{rect.right + 12}}px`;
            sidebarTooltip.classList.add("show");
          }});
          tab.addEventListener("mouseleave", () => {{
            sidebarTooltip.classList.remove("show");
          }});
          tab.addEventListener("click", () => {{
            setActiveTab(tab.dataset.tab);
          }});
        }});

        sidebarDrawerClose?.addEventListener("click", () => {{
          document.body.classList.remove("sidebar-open");
        }});

        const sidebarInlineOpen = document.getElementById("settings-open-inline");
        sidebarInlineOpen?.addEventListener("click", () => {{
          openSettings();
        }});

        document.getElementById("side-skill-filter")?.addEventListener("change", renderSkillCards);
        document.getElementById("skill-add-button")?.addEventListener("click", () => {{
          openSkillEditor();
        }});
        document.getElementById("skill-editor-cancel")?.addEventListener("click", () => {{
          const form = document.getElementById("skill-editor");
          if (form) {{
            form.hidden = true;
          }}
        }});
        document.getElementById("skill-editor")?.addEventListener("submit", async (event) => {{
          const errorBox = document.getElementById("side-skill-error");
          if (errorBox) {{
            errorBox.textContent = "";
          }}
          try {{
            await saveSkill(event);
          }} catch (error) {{
            event.preventDefault();
            if (errorBox) {{
              errorBox.textContent = error.message || String(error);
            }}
          }}
        }});
        document.getElementById("side-skill-grid")?.addEventListener("click", async (event) => {{
          const editButton = event.target.closest("[data-edit-skill]");
          const deleteButton = event.target.closest("[data-delete-skill]");
          const errorBox = document.getElementById("side-skill-error");
          if (editButton) {{
            openSkillEditor(editButton.dataset.editSkill);
            return;
          }}
          if (deleteButton) {{
            try {{
              await deleteSkill(deleteButton.dataset.deleteSkill);
              if (errorBox) {{
                errorBox.textContent = "";
              }}
            }} catch (error) {{
              if (errorBox) {{
                errorBox.textContent = error.message || String(error);
              }}
            }}
          }}
        }});

        // Default tab on first load: chat (drawer is closed)
        setActiveTab("chat");
        document.body.classList.remove("sidebar-open");

        bindUi();
        scrollChatToBottom();
        playLatestAssistantTts();
      </script>
    </body>
    </html>
    """


def _load_history(value: str) -> list[dict[str, str]]:
    try:
        messages = json.loads(value) if value else []
    except json.JSONDecodeError:
        return []

    clean_messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "assistant"))
        content = str(message.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            clean_messages.append({"role": role, "content": content})
    return clean_messages


def _append_assistant_once(messages: list[dict[str, str]], content: str) -> None:
    if not messages or messages[-1].get("content") != content:
        messages.append({"role": "assistant", "content": content})


def _serve_tts(environ, start_response):
    query = parse_qs(environ.get("QUERY_STRING", ""))
    text = _clean_tts_text(query.get("text", [""])[0])
    if not text:
        body = b"text is required"
        start_response("400 Bad Request", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
        return [body]

    form_body = f"text={quote_plus(text)}".encode("utf-8")
    request = Request(
        f"{SPEECH_SERVICE_URL}/tts",
        data=form_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            audio = response.read()
    except URLError as exc:
        body = f"TTS service unavailable: {exc}".encode("utf-8")
        start_response("503 Service Unavailable", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
        return [body]

    start_response(
        "200 OK",
        [
            ("Content-Type", "audio/wav"),
            ("Content-Length", str(len(audio))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [audio]


def _serve_settings(environ, start_response):
    settings = _collect_settings()
    body = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
    start_response(
        "200 OK",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [body]


# Atomic skill catalog. Mirrors the names referenced in orchestrator.py / runtime.py
# and the `skill_names` advertised in bootstrap.py. This prototype keeps edits
# in the current web process; restart restores the defaults below.
SKILL_CATALOG: list[dict[str, object]] = [
    {
        "id": "speak",
        "name": "Speak",
        "name_zh": "语音播报",
        "category": "interaction",
        "summary": "通过 TTS 服务向用户播报一段中文文本，常用于确认和进度同步。",
        "input_schema": {"message": "string"},
        "output_schema": {"played": "bool"},
        "owner_robot": "robot-greeter-01 / robot-barista-01",
        "status": "registered",
    },
    {
        "id": "operate_coffee_machine",
        "name": "Operate Coffee Machine",
        "name_zh": "操作咖啡机",
        "category": "manipulation",
        "summary": "调用门店咖啡机接口，按饮品类型 / 温度 / 糖度 / 冰量参数制作一杯饮品。",
        "input_schema": {
            "drink_type": "cappuccino | americano",
            "temperature": "hot | regular | cold",
            "sugar": "no_sugar | default",
            "ice": "no_ice | regular",
        },
        "output_schema": {"drink_id": "string", "ready": "bool"},
        "owner_robot": "robot-barista-01",
        "status": "registered",
    },
    {
        "id": "stage_order",
        "name": "Stage Order",
        "name_zh": "装载订单",
        "category": "manipulation",
        "summary": "把已准备好的饮品或物品按目的地分组并摆到托盘上，等待配送。",
        "input_schema": {"destination": "string", "drink_count": "int"},
        "output_schema": {"tray_id": "string"},
        "owner_robot": "robot-barista-01",
        "status": "registered",
    },
    {
        "id": "deliver_items",
        "name": "Deliver Items",
        "name_zh": "递送物品",
        "category": "navigation",
        "summary": "按目的地（桌号 / 顾客）导航并把托盘送到目标位置。",
        "input_schema": {"destination": "string"},
        "output_schema": {"delivered_at": "string"},
        "owner_robot": "robot-greeter-01 / robot-barista-01",
        "status": "registered",
    },
    {
        "id": "pickup_item",
        "name": "Pickup Item",
        "name_zh": "取物品",
        "category": "manipulation",
        "summary": "到指定 source（默认 service_station）取一件物品，准备递送。",
        "input_schema": {"item_type": "string", "temperature": "string", "source": "string"},
        "output_schema": {"item_id": "string"},
        "owner_robot": "robot-greeter-01",
        "status": "registered",
    },
    {
        "id": "agent_plan",
        "name": "Agent Plan",
        "name_zh": "代理规划",
        "category": "agent",
        "summary": "当任务未命中 SOP / Skill 时，调用 LLM 做开放式任务分解（仅规划，不直接驱动机器人）。",
        "input_schema": {"goal": "string"},
        "output_schema": {"plan": "string"},
        "owner_robot": "(central orchestrator)",
        "status": "registered",
    },
    {
        "id": "validate_plan",
        "name": "Validate Plan",
        "name_zh": "校验计划",
        "category": "agent",
        "summary": "校验 agent_plan 输出的工作流结构是否合法，避免危险动作进入执行链路。",
        "input_schema": {"intent_type": "string"},
        "output_schema": {"valid": "bool", "reasons": "string[]"},
        "owner_robot": "(central orchestrator)",
        "status": "registered",
    },
]
SKILL_CATALOG_LOCK = RLock()


def _json_response(start_response, payload: object, status: str = "200 OK"):
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [body]


def _read_json_body(environ) -> dict[str, object]:
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        size = 0
    if size <= 0:
        return {}
    raw = environ["wsgi.input"].read(size).decode("utf-8")
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise ValueError("JSON object is required")
    return data


def _parse_skill_id(path: str, query: str) -> str:
    prefix = "/api/skills/"
    if path.startswith(prefix):
        return unquote(path[len(prefix) :]).strip()
    return parse_qs(query).get("id", [""])[0].strip()


def _normalize_schema(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _normalize_skill_payload(data: dict[str, object], existing_id: str | None = None) -> dict[str, object]:
    skill_id = str(data.get("id") or existing_id or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", skill_id):
        raise ValueError("skill id must use letters, numbers, _ or -")
    name = str(data.get("name") or skill_id.replace("_", " ").title()).strip()
    name_zh = str(data.get("name_zh") or name).strip()
    category = str(data.get("category") or "agent").strip()
    summary = str(data.get("summary") or "").strip()
    owner_robot = str(data.get("owner_robot") or "(unassigned)").strip()
    status = str(data.get("status") or "registered").strip()
    return {
        "id": skill_id,
        "name": name,
        "name_zh": name_zh,
        "category": category,
        "summary": summary,
        "input_schema": _normalize_schema(data.get("input_schema")),
        "output_schema": _normalize_schema(data.get("output_schema")),
        "owner_robot": owner_robot,
        "status": status,
    }


def _serve_sops(environ, start_response):
    templates = [
        {
            "template_id": template.template_id,
            "name": template.name,
            "intent_type": template.intent_type,
            "workflow_steps": template.workflow_steps,
            "supports_multi_robot": template.supports_multi_robot,
            "step_count": len(template.workflow_steps),
        }
        for template in TaskMemory().templates
    ]
    return _json_response(start_response, {"sops": templates})


def _serve_skills(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "")

    try:
        if method == "GET":
            with SKILL_CATALOG_LOCK:
                return _json_response(start_response, {"skills": deepcopy(SKILL_CATALOG)})

        if method == "POST":
            data = _read_json_body(environ)
            skill = _normalize_skill_payload(data)
            with SKILL_CATALOG_LOCK:
                if any(existing["id"] == skill["id"] for existing in SKILL_CATALOG):
                    return _json_response(start_response, {"error": "skill id already exists"}, "409 Conflict")
                SKILL_CATALOG.append(skill)
                return _json_response(start_response, {"skill": skill}, "201 Created")

        if method == "PUT":
            skill_id = _parse_skill_id(path, environ.get("QUERY_STRING", ""))
            data = _read_json_body(environ)
            skill = _normalize_skill_payload({**data, "id": skill_id or data.get("id")}, skill_id)
            with SKILL_CATALOG_LOCK:
                for index, existing in enumerate(SKILL_CATALOG):
                    if existing["id"] == skill["id"]:
                        SKILL_CATALOG[index] = skill
                        return _json_response(start_response, {"skill": skill})
                return _json_response(start_response, {"error": "skill not found"}, "404 Not Found")

        if method == "DELETE":
            skill_id = _parse_skill_id(path, environ.get("QUERY_STRING", ""))
            with SKILL_CATALOG_LOCK:
                for index, existing in enumerate(SKILL_CATALOG):
                    if existing["id"] == skill_id:
                        removed = SKILL_CATALOG.pop(index)
                        return _json_response(start_response, {"removed": removed})
                return _json_response(start_response, {"error": "skill not found"}, "404 Not Found")
    except (json.JSONDecodeError, ValueError) as exc:
        return _json_response(start_response, {"error": str(exc)}, "400 Bad Request")

    return _json_response(start_response, {"error": f"method {method} is not supported"}, "405 Method Not Allowed")


def _render_settings_modal() -> str:
    return """
    <div class="settings-backdrop" id="settings-backdrop" hidden>
      <div class="settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <header class="settings-head">
          <div>
            <span class="eyebrow">Settings</span>
            <h2 id="settings-title">运行设置与提示词</h2>
          </div>
          <button type="button" class="settings-close" id="settings-close" aria-label="关闭">×</button>
        </header>
        <div class="settings-body">
          <section class="settings-section">
            <h3>LLM 后端</h3>
            <dl class="settings-kv" id="settings-llm">
              <dt>backend</dt><dd>—</dd>
              <dt>model</dt><dd>—</dd>
              <dt>host</dt><dd>—</dd>
              <dt>timeout</dt><dd>—</dd>
              <dt>thinking</dt><dd>—</dd>
            </dl>
          </section>

          <section class="settings-section">
            <h3>语音服务</h3>
            <dl class="settings-kv" id="settings-speech">
              <dt>service url</dt><dd>—</dd>
              <dt>ASR model</dt><dd>—</dd>
              <dt>TTS model</dt><dd>—</dd>
            </dl>
          </section>

          <section class="settings-section">
            <h3>执行优先级</h3>
            <ol class="settings-list" id="settings-policy"></ol>
          </section>

          <section class="settings-section">
            <h3>给 Qwen 的意图解析提示词模板</h3>
            <p class="muted">
              这是发送给 Ollama <code>/api/generate</code> 的 <code>prompt</code> 字段（Ollama 端会再追加 <code>/no_think</code>）。
              实际运行时由 <code>PromptBuilder.build(UserInput)</code> 拼接。
            </p>
            <pre class="settings-prompt" id="settings-prompt-template"></pre>
          </section>

          <section class="settings-section">
            <h3>运行时样例（使用默认任务拼接）</h3>
            <p class="muted">下面这一段是 <code>PromptBuilder.build(UserInput(text=DEFAULT_TASK))</code> 的真实输出。</p>
            <pre class="settings-prompt" id="settings-prompt-sample"></pre>
          </section>

          <section class="settings-section">
            <h3>闲聊系统提示词（用于 /api/chat）</h3>
            <pre class="settings-prompt" id="settings-chat-prompt"></pre>
          </section>
        </div>
      </div>
    </div>
    """


def _serve_asr(environ, start_response):
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        size = 0
    body = environ["wsgi.input"].read(size)
    content_type = environ.get("CONTENT_TYPE", "")
    if not body or "multipart/form-data" not in content_type:
        payload = b'{"error":"multipart audio is required"}'
        start_response("400 Bad Request", [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))])
        return [payload]

    request = Request(
        f"{SPEECH_SERVICE_URL}/asr",
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            payload = response.read()
            status = "200 OK"
    except HTTPError as exc:
        payload = exc.read() or json.dumps({"error": str(exc)}).encode("utf-8")
        status = f"{exc.code} {exc.reason}"
    except URLError as exc:
        payload = json.dumps({"error": f"ASR service unavailable: {exc}"}).encode("utf-8")
        status = "503 Service Unavailable"

    start_response(
        status,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(payload))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [payload]


SIDEBAR_TABS = [
    {
        "id": "chat",
        "label": "当前对话",
        "title": "Current conversation",
        "icon": "chat",
    },
    {
        "id": "skills",
        "label": "原子能力",
        "title": "Atomic skill catalog",
        "icon": "skills",
    },
    {
        "id": "sop",
        "label": "SOP",
        "title": "SOP catalog",
        "icon": "sop",
    },
    {
        "id": "settings",
        "label": "设置",
        "title": "Settings & prompts",
        "icon": "settings",
    },
]


def _render_sidebar_icons() -> str:
    parts = []
    for tab in SIDEBAR_TABS:
        parts.append(
            f"""
            <button type="button" class="sidebar-tab" data-tab="{tab['id']}" title="{html.escape(tab['label'])}" aria-label="{html.escape(tab['label'])}">
              <span class="sidebar-icon" data-icon="{tab['icon']}" aria-hidden="true"></span>
            </button>
            """
        )
    return "".join(parts)


def _render_sidebar_panels() -> str:
    skill_cards = "".join(
        f"""
        <article class="skill-card" data-category="{html.escape(str(skill['category']))}">
          <header>
            <h4>{html.escape(str(skill['name_zh']))}</h4>
            <code>{html.escape(str(skill['id']))}</code>
          </header>
          <p>{html.escape(str(skill['summary']))}</p>
          <dl class="skill-meta">
            <dt>类别</dt><dd>{html.escape(str(skill['category']))}</dd>
            <dt>归属机器人</dt><dd>{html.escape(str(skill['owner_robot']))}</dd>
            <dt>状态</dt><dd><span class="skill-status status-registered">{html.escape(str(skill['status']))}</span></dd>
          </dl>
          <details>
            <summary>输入 / 输出 schema</summary>
            <pre>{html.escape(json.dumps({'input': skill['input_schema'], 'output': skill['output_schema']}, ensure_ascii=False, indent=2))}</pre>
          </details>
        </article>
        """
        for skill in SKILL_CATALOG
    )

    return f"""
    <section class="sidebar-panel" data-panel="chat" hidden>
      <header class="sidebar-panel-head">
        <h3>当前对话</h3>
        <p class="muted">主对话面板仍在上方，此处列出关键状态。</p>
      </header>
      <dl class="sidebar-kv">
        <dt>当前 tab</dt><dd id="side-current-tab">—</dd>
        <dt>未读消息</dt><dd id="side-unread">0</dd>
        <dt>最近一次活动</dt><dd id="side-last-event">—</dd>
      </dl>
      <p class="muted small">提示：返回主面板继续对话，或切换到「原子能力」查看当前可调度的技能清单。</p>
    </section>

    <section class="sidebar-panel" data-panel="skills" hidden>
      <header class="sidebar-panel-head">
        <div>
          <h3>原子能力清单</h3>
          <p class="muted">当前 Web 进程内可添加、修改和删除，重启后恢复代码默认值。</p>
        </div>
        <span class="skill-count" id="side-skill-count">{len(SKILL_CATALOG)} 项</span>
      </header>
      <div class="asset-toolbar">
        <div class="skill-filter">
          <label for="side-skill-filter">类别</label>
          <select id="side-skill-filter">
            <option value="">全部</option>
            <option value="interaction">interaction</option>
            <option value="manipulation">manipulation</option>
            <option value="navigation">navigation</option>
            <option value="agent">agent</option>
          </select>
        </div>
        <button type="button" class="asset-add-button" id="skill-add-button">添加</button>
      </div>
      <form class="skill-editor" id="skill-editor" hidden>
        <input type="hidden" id="skill-editing-id">
        <label>技能 ID
          <input id="skill-id" name="id" placeholder="new_skill" required>
        </label>
        <label>英文名
          <input id="skill-name" name="name" placeholder="New Skill">
        </label>
        <label>中文名
          <input id="skill-name-zh" name="name_zh" placeholder="新技能" required>
        </label>
        <label>类别
          <select id="skill-category" name="category">
            <option value="interaction">interaction</option>
            <option value="manipulation">manipulation</option>
            <option value="navigation">navigation</option>
            <option value="agent">agent</option>
          </select>
        </label>
        <label>归属机器人
          <input id="skill-owner" name="owner_robot" placeholder="robot-greeter-01">
        </label>
        <label>状态
          <input id="skill-status" name="status" value="registered">
        </label>
        <label>摘要
          <textarea id="skill-summary" name="summary" placeholder="描述这个原子能力做什么"></textarea>
        </label>
        <label>输入 schema
          <textarea id="skill-input-schema" name="input_schema"></textarea>
        </label>
        <label>输出 schema
          <textarea id="skill-output-schema" name="output_schema"></textarea>
        </label>
        <div class="skill-editor-actions">
          <button type="submit">保存</button>
          <button type="button" id="skill-editor-cancel">取消</button>
        </div>
      </form>
      <p class="muted small" id="side-skill-error"></p>
      <div class="skill-grid" id="side-skill-grid">
        {skill_cards}
      </div>
    </section>

    <section class="sidebar-panel" data-panel="sop" hidden>
      <header class="sidebar-panel-head">
        <div>
          <h3>SOP 细节</h3>
          <p class="muted">展示 task-memory-service 当前登记的工作流模板和步骤。</p>
        </div>
        <span class="skill-count" id="side-sop-count">—</span>
      </header>
      <div class="skill-grid" id="side-sop-grid"></div>
    </section>

    <section class="sidebar-panel" data-panel="settings" hidden>
      <header class="sidebar-panel-head">
        <h3>设置与提示词</h3>
        <p class="muted">查看 LLM 后端、语音服务、执行优先级和 Qwen 提示词模板。</p>
      </header>
      <p class="muted small">设置面板已迁到这里：点击下方「打开设置」弹出原模态框（保持历史 API 不变）。</p>
      <button type="button" class="settings-open-button" id="settings-open-inline">⚙ 打开设置面板</button>
    </section>
    """


def _render_sidebar() -> str:
    return f"""
    <aside class="sidebar" id="sidebar" aria-label="主导航">
      <div class="sidebar-rail">
        <div class="sidebar-brand" title="Humanoid Fleet">
          <span class="sidebar-brand-dot"></span>
        </div>
        {_render_sidebar_icons()}
      </div>
      <div class="sidebar-drawer" id="sidebar-drawer">
        <button type="button" class="sidebar-drawer-close" id="sidebar-drawer-close" aria-label="收起侧栏">›</button>
        <div class="sidebar-drawer-body">
          {_render_sidebar_panels()}
        </div>
      </div>
    </aside>
    """


def application(environ, start_response):
    if environ.get("PATH_INFO", "") == "/tts.wav":
        return _serve_tts(environ, start_response)
    if environ.get("PATH_INFO", "") == "/asr":
        return _serve_asr(environ, start_response)
    if environ.get("PATH_INFO", "") == "/settings.json":
        return _serve_settings(environ, start_response)
    if environ.get("PATH_INFO", "").startswith("/api/sops"):
        return _serve_sops(environ, start_response)
    if environ.get("PATH_INFO", "").startswith("/api/skills"):
        return _serve_skills(environ, start_response)

    app = FleetControlApp()
    method = environ.get("REQUEST_METHOD", "GET").upper()
    messages: list[dict[str, str]] = []
    review = None
    result = None
    error = None

    if method == "POST":
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            size = 0
        body = environ["wsgi.input"].read(size).decode("utf-8")
        params = parse_qs(body)
        messages = _load_history(params.get("history", [""])[0])
        action = params.get("action", ["send"])[0]

        if action == "confirm":
            try:
                review = app.review_conversation(messages)
                if review.get("state") == "awaiting_confirmation":
                    result = app.plan_task(review["task_text"])
                    _append_assistant_once(messages, "已确认。我已经生成机器人分工和执行计划。")
                    review = None
                else:
                    _append_assistant_once(messages, "当前还没有命中可确认的 SOP，请继续补充任务信息。")
            except Exception as exc:
                error = str(exc)
        else:
            message_text = params.get("message", [""])[0].strip()
            if message_text:
                messages.append({"role": "user", "content": message_text})
            try:
                review = app.review_conversation(messages)
                if _looks_like_confirmation(message_text) and review.get("state") == "awaiting_confirmation":
                    result = app.plan_task(review["task_text"])
                    _append_assistant_once(messages, "已确认。我已经生成机器人分工和执行计划。")
                    review = None
                else:
                    _append_assistant_once(messages, review["assistant_message"])
            except Exception as exc:
                error = str(exc)
    else:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        task_text = query.get("task", [""])[0].strip()
        if task_text:
            messages.append({"role": "user", "content": task_text})
            try:
                review = app.review_conversation(messages)
                _append_assistant_once(messages, review["assistant_message"])
            except Exception as exc:
                error = str(exc)

    page = render_page(messages, review=review, result=result, error=error).encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(page)))])
    return [page]


def main() -> None:
    host = "127.0.0.1"
    port = 8000
    with make_server(host, port, application, server_class=ThreadingWSGIServer) as httpd:
        print(f"Preview available at http://{host}:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
