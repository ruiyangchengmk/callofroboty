from __future__ import annotations

import html
from urllib.parse import parse_qs, quote_plus
from wsgiref.simple_server import make_server

from humanoid_fleet.app import FleetControlApp

DEFAULT_TASK = "去给顾客制作一杯卡布奇诺去冰，一杯热美式不加糖"
EXAMPLE_TASKS = [
    "去给顾客制作一杯卡布奇诺去冰，一杯热美式不加糖",
    "去拿杯冰可乐给3号桌顾客",
    "把可乐送给顾客",
]


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


def render_page(task_text: str, result: dict | None = None, error: str | None = None) -> str:
    escaped_task = html.escape(task_text)
    content = ""

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
        }}
        .wrap {{
          max-width: 1180px;
          margin: 0 auto;
          padding: 28px 18px 52px;
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
        @media (max-width: 900px) {{
          .summary-grid, .grid.two, .route-grid {{
            grid-template-columns: 1fr;
          }}
          .section-head, .timeline-head {{
            display: block;
          }}
        }}
      </style>
    </head>
    <body>
      <main class="wrap">
        <section class="hero">
          <span class="eyebrow">Humanoid Fleet Control</span>
          <h1>分级任务编排预览台</h1>
          <p class="subtitle">输入一段门店服务任务，页面会展示任务理解、执行资产命中路径、SOP/Skill/Agent 分流、工作流实例化、机器人分工和用户透明反馈。这个页面现在重点强调“资产优先执行”。</p>
          <div class="examples">
            {_render_example_links()}
          </div>
          <form method="post">
            <label for="task"><strong>任务输入</strong></label>
            <textarea id="task" name="task">{escaped_task}</textarea>
            <button type="submit">生成编排结果</button>
          </form>
        </section>
        {content}
      </main>
    </body>
    </html>
    """


def application(environ, start_response):
    app = FleetControlApp()
    method = environ.get("REQUEST_METHOD", "GET").upper()
    task_text = DEFAULT_TASK
    result = None
    error = None

    if method == "POST":
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            size = 0
        body = environ["wsgi.input"].read(size).decode("utf-8")
        params = parse_qs(body)
        task_text = params.get("task", [DEFAULT_TASK])[0].strip() or DEFAULT_TASK
        try:
            result = app.plan_task(task_text)
        except Exception as exc:
            error = str(exc)
    else:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        task_text = query.get("task", [DEFAULT_TASK])[0].strip() or DEFAULT_TASK
        result = app.plan_task(task_text)

    page = render_page(task_text, result=result, error=error).encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(page)))])
    return [page]


def main() -> None:
    host = "127.0.0.1"
    port = 8000
    with make_server(host, port, application) as httpd:
        print(f"Preview available at http://{host}:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
