# 人形机器人集群控制系统架构文档

> **架构风格**：微服务 + 事件驱动 + 分层编排（事件驱动的领域微服务架构）
> **目标读者**：架构师、后端研发、平台 SRE、产品经理

---

## 0. 文档目的

本文档定义门店人形机器人集群控制系统的微服务架构，涵盖服务拆分、领域边界、通信协议、数据契约、部署拓扑、容错机制和演进路线。读者阅读后应能：

- 清楚系统由哪些微服务组成、各自职责
- 理解服务间通过什么方式协作（同步 gRPC / 异步事件）
- 知道门店边缘节点与中心云端的部署形态
- 知道当前原型已实现哪些服务、哪些仍在规划中

---

## 1. 架构总览

### 1.1 一句话架构描述

**以"任务理解 → 任务记忆 → 分级编排 → 协同调度 → 技能运行时 → 实时控制"为执行链路，以事件总线和 gRPC 为服务骨架，部署形态为"中心云控 + 门店边缘 + 机器人本体"三层。**

### 1.2 架构风格

- **微服务架构**：每个核心能力独立成服务，进程级隔离
- **事件驱动**：跨服务状态变更通过事件总线解耦
- **CQRS 友好**：执行链路（写）与查询/回放链路（读）可分离
- **边云协同**：高频/低时延逻辑放边缘，全局/重逻辑放中心

### 1.3 微服务全景图

系统由 **10 个微服务 + 2 个 BFF（API 聚合层）** 组成：

```
┌─────────────────────────────────────────────────────────────┐
│                     接入层（BFF）                              │
│  interaction-bff (8000)        │  ops-bff (8090)            │
└────────┬───────────────────────┴──────────┬─────────────────┘
         │                                  │
┌────────┴──────────────────────────────────┴────────────────┐
│                     核心领域微服务（中心 + 边缘）               │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │ interaction   │  │ intent       │  │ task-memory  │        │
│  │ service       │  │ service      │  │ service      │        │
│  │ (交互接入)    │  │ (意图理解)    │  │ (模板记忆)    │        │
│  └──────────────┘  └──────────────┘  └──────────────┘        │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │ orchestrator  │  │ scheduler    │  │ skill-runtime│        │
│  │ service       │  │ service      │  │ service      │        │
│  │ (任务编排)    │  │ (协同调度)    │  │ (技能执行)    │        │
│  └──────────────┘  └──────────────┘  └──────────────┘        │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │ resource     │  │ observability│  │ robot-gateway│        │
│  │ service      │  │ service      │  │              │        │
│  │ (资源管理)    │  │ (可观测性)    │  │ (机器人接入)  │        │
│  └──────────────┘  └──────────────┘  └──────────────┘        │
│                                                               │
│  ┌──────────────┐                                              │
│  │ speech       │  (ASR/TTS 独立服务，CPU 友好)               │
│  │ service      │                                              │
│  └──────────────┘                                              │
└─────────────────────────────────────────────────────────────┘
                ↕                  ↕                  ↕
        ┌──────────────┐    ┌──────────────┐   ┌──────────────┐
        │   Postgres   │    │   Kafka/NATS │   │   Redis      │
        │   (事务)     │    │   (事件总线) │   │   (热状态)   │
        └──────────────┘    └──────────────┘   └──────────────┘
```

---

## 2. 微服务拆分原则

### 2.1 拆分依据

按 **业务能力（Business Capability）** 拆分，而非按技术层：

- 一个微服务 = 一个领域 = 一个独立可部署单元
- 服务之间通过 **gRPC（同步）** + **事件总线（异步）** 协作
- 每个服务有自己的数据库 / 存储（**不共享数据库**）
- 每个服务无状态化，状态外置到 Redis / Postgres

### 2.2 拆分原则

| 原则 | 说明 |
|------|------|
| 单一职责 | 一个服务只负责一个领域能力 |
| 独立部署 | 可独立发布、扩容、灰度 |
| 数据自治 | 不跨服务直接访问数据库 |
| 异步优先 | 跨服务状态流转走事件 |
| 同步最小化 | gRPC 只用于必要的请求-响应 |
| 失败隔离 | 单服务故障不级联 |

### 2.3 服务粒度判断

- **过粗**（单体）：修改一处需要全量回归
- **过细**（纳米服务）：分布式事务 / 链路复杂度爆炸
- **当前粒度**：按领域划 10 个服务，每个服务 1-3 个核心实体，符合团队 3-5 人的服务负责制

---

## 3. 微服务详细定义

### 3.1 interaction-service（交互接入服务）

| 项 | 内容 |
|----|------|
| **职责** | 语音 / 文本 / 触屏输入接入；维护会话上下文；渲染用户反馈 |
| **核心实体** | `Session`, `UserTurn`, `AssistantTurn` |
| **依赖服务** | speech-service（转写）、intent-service（解析） |
| **存储** | Redis（会话状态，TTL 30 分钟） |
| **通信** | 上游 BFF：HTTP；下游 intent-service：gRPC |
| **关键接口** | `POST /sessions/{id}/turns`, `GET /sessions/{id}/tts` |
| **当前原型** | `web.py` + `speech_service.py` 合并实现 |

### 3.2 speech-service（语音服务） ✅ 已实现

| 项 | 内容 |
|----|------|
| **职责** | 中文 ASR / TTS，独立部署，CPU 友好（aarch64） |
| **技术栈** | sherpa-onnx + FastAPI |
| **模型** | Paraformer small int8（ASR）、MeloTTS VITS（TTS） |
| **端口** | 8010 |
| **关键接口** | `POST /asr`（multipart audio）、`POST /tts`（form text） |
| **当前实现** | `src/humanoid_fleet/speech_service.py` |

### 3.3 intent-service（意图理解服务） ✅ 已实现

| 项 | 内容 |
|----|------|
| **职责** | 自然语言 → 结构化 Intent；多模态输入解析 |
| **核心实体** | `Intent`, `DrinkOrder`, `ItemOrder` |
| **依赖服务** | Ollama 或 Mock 模型（通过 `ModelClient` 接口注入） |
| **存储** | 无状态 |
| **通信** | 上游：gRPC；内部推理：HTTP → Ollama |
| **关键接口** | `POST /interpret`（UserInput → Intent） |
| **关键能力** | 意图分类、槽位提取、歧义检测、置信度评估 |
| **当前实现** | `src/humanoid_fleet/understanding.py` |

### 3.4 task-memory-service（任务记忆服务） ✅ 已实现

| 项 | 内容 |
|----|------|
| **职责** | 模板存储、匹配、版本管理、相似任务召回、经验沉淀 |
| **核心实体** | `TaskTemplate`, `TemplateVersion`, `Experience` |
| **依赖服务** | 无 |
| **存储** | Postgres（模板元数据）+ 向量库（相似召回，待选型） |
| **通信** | gRPC |
| **关键接口** | `find_template(intent)`, `record_experience(...)` |
| **关键能力** | 模板匹配、参数 schema 校验、版本化、人工审核 |
| **当前实现** | `src/humanoid_fleet/memory.py`（内存版） |

### 3.5 orchestrator-service（任务编排服务） ✅ 已实现

| 项 | 内容 |
|----|------|
| **职责** | 任务目标 → 工作流 → 技能链；SOP / Skill / Agent 三路决策 |
| **核心实体** | `WorkflowPlan`, `WorkflowTask`, `ResolvedExecution` |
| **依赖服务** | task-memory, scheduler |
| **存储** | Postgres（工作流实例）+ Redis（运行状态） |
| **通信** | 上游 gRPC；下游 scheduler gRPC；事件发 Kafka |
| **关键接口** | `build_workflow(intent, resolved)`, `plan_task(text)` |
| **当前实现** | `src/humanoid_fleet/orchestrator.py` + `resolution.py` |

### 3.6 scheduler-service（协同调度服务） ✅ 已实现

| 项 | 内容 |
|----|------|
| **职责** | 多机器人能力匹配、资源分配、单/多机决策、任务迁移 |
| **核心实体** | `RobotCapability`, `Assignment` |
| **依赖服务** | resource-service, robot-gateway |
| **存储** | Redis（机器人热状态）+ Postgres（历史分配） |
| **通信** | gRPC + 事件订阅 `RobotStateChanged` |
| **关键接口** | `assign(workflow, robots)`, `reassign(task_id)` |
| **当前实现** | `src/humanoid_fleet/scheduler.py` |

### 3.7 skill-runtime-service（技能运行时服务）

| 项 | 内容 |
|----|------|
| **职责** | 技能注册中心、调用网关、执行状态机、失败恢复 |
| **核心实体** | `SkillSpec`, `SkillExecution`, `ExecutionTrace` |
| **依赖服务** | robot-gateway |
| **存储** | Postgres（执行历史）+ Redis（运行时状态） |
| **通信** | gRPC + 事件发 `SkillStarted`/`SkillCompleted`/`SkillFailed` |
| **关键接口** | `execute(skill_id, params)`, `cancel(task_id)` |
| **当前实现** | `src/humanoid_fleet/runtime.py`（仅规划卡生成） |

### 3.8 resource-service（资源管理服务）

| 项 | 内容 |
|----|------|
| **职责** | 咖啡机、工作台、物料、通道等共享资源协调 |
| **核心实体** | `Resource`, `ResourceOccupancy` |
| **存储** | Postgres + Redis |
| **通信** | gRPC + 事件发 `ResourceAcquired`/`ResourceReleased` |
| **关键接口** | `acquire(resource_id)`, `release(...)`, `query_availability(...)` |

### 3.9 robot-gateway（机器人接入网关）

| 项 | 内容 |
|----|------|
| **职责** | 机器人接入、心跳采集、状态上报、指令下行、协议适配 |
| **核心实体** | `RobotSession`, `RobotHeartbeat` |
| **存储** | Redis（在线状态） |
| **通信** | 机器人侧：DDS / Zenoh（ROS 2）；平台侧：gRPC + 事件 |
| **关键接口** | `register_robot(...)`, `report_state(...)`, `dispatch_skill(...)` |

### 3.10 observability-service（可观测性服务）

| 项 | 内容 |
|----|------|
| **职责** | 日志聚合、链路追踪、指标采集、事件回放 |
| **技术栈** | OpenTelemetry + Prometheus + Loki/Jaeger + MinIO（事件存储） |
| **通信** | 拉模式（Prometheus）+ 推模式（OTel SDK） |
| **关键能力** | 端到端 trace、决策解释、任务回放 |

---

## 4. 服务间通信模式

### 4.1 同步通信：gRPC

**用于**：请求-响应、需要立即返回结果的场景

```
interaction-service ──gRPC──> intent-service (解析意图)
orchestrator-service ──gRPC──> task-memory-service (查模板)
orchestrator-service ──gRPC──> scheduler-service (分配机器人)
skill-runtime-service ──gRPC──> robot-gateway (下发技能)
```

**Proto 文件统一管理**：`/proto/*.proto`，所有服务依赖同一套 IDL。

### 4.2 异步通信：事件总线

**用于**：状态变更、跨服务通知、不需要立即响应的场景

| 事件主题 | 发布方 | 订阅方 |
|----------|--------|--------|
| `IntentRecognized` | intent-service | orchestrator-service |
| `WorkflowPlanned` | orchestrator-service | observability-service |
| `RobotAssigned` | scheduler-service | observability-service |
| `SkillStarted` | skill-runtime-service | observability-service, interaction-service |
| `SkillCompleted` | skill-runtime-service | interaction-service（反馈用户） |
| `SkillFailed` | skill-runtime-service | orchestrator-service（重规划） |
| `RobotStateChanged` | robot-gateway | scheduler-service, observability-service |
| `ResourceAcquired`/`Released` | resource-service | scheduler-service |

**事件总线选型**：Kafka（强一致、回放需求）或 NATS（轻量、低时延）

### 4.3 通信原则

- **能异步就异步**：减少服务耦合，避免级联雪崩
- **同步调用要超时**：gRPC 默认 3s 超时，避免长时间占用
- **事件要有版本号**：避免消费者因事件结构变化崩溃
- **关键事件持久化**：用于回放和审计

---

## 5. 数据架构

### 5.1 数据存储分工

| 存储 | 用途 | 写入方 |
|------|------|--------|
| **PostgreSQL** | 事务数据（模板、工作流实例、技能执行） | task-memory, orchestrator, skill-runtime, resource |
| **Redis** | 热状态（会话、机器人在线、任务运行状态） | interaction, scheduler, robot-gateway |
| **Kafka / NATS** | 事件流 | 全服务 |
| **ClickHouse / TimescaleDB** | 时序指标 | observability |
| **向量库**（Qdrant / pgvector） | 相似任务召回 | task-memory |
| **MinIO / S3** | 日志归档、音频、trace 数据 | observability, speech |

### 5.2 数据自治

- 每个服务 **拥有自己的表**，不直接跨服务查询
- 跨服务数据需求通过 **API 调用** 或 **事件订阅** 解决
- 严禁跨服务 join、严禁共享 schema

### 5.3 关键数据契约

服务间通过 Protobuf / JSON Schema 定义数据契约：

```protobuf
// intent.proto
message Intent {
  string intent_id = 1;
  string session_id = 2;
  string intent_type = 3;  // prepare_and_deliver_drinks | pickup_and_deliver_items
  repeated DrinkOrder drinks = 4;
  repeated ItemOrder items = 5;
  string destination = 6;
  map<string, string> constraints = 7;
  float confidence = 8;
  bool requires_confirmation = 9;
}
```

---

## 6. 部署架构

### 6.1 三层部署

```
┌────────────────────────────────────────────────────┐
│                  中心云控                           │
│  intent-service, task-memory-service,              │
│  resource-service, observability-service, ops-bff  │
│  (Kubernetes, 多副本)                               │
└────────────────────────────────────────────────────┘
                        ↕ HTTPS / VPN
┌────────────────────────────────────────────────────┐
│              门店边缘节点 (每店 1 套)                │
│  interaction-bff, interaction-service,             │
│  orchestrator-service, scheduler-service,          │
│  skill-runtime-service, robot-gateway,             │
│  speech-service                                    │
│  (K3s / Docker Compose, 边缘服务器或工控机)         │
└────────────────────────────────────────────────────┘
                        ↕ DDS / Zenoh / WiFi
┌────────────────────────────────────────────────────┐
│                机器人本体 (每机器人 1 套)            │
│  - ROS 2 节点 (技能代理、本地控制)                  │
│  - 本地安全闭环 (避障、急停)                        │
│  - 状态采集                                         │
└────────────────────────────────────────────────────┘
```

### 6.2 服务部署策略

| 服务 | 部署位置 | 副本数 | 说明 |
|------|----------|--------|------|
| interaction-bff | 边缘 | 2 | 入口层冗余 |
| ops-bff | 中心 | 2 | 运营平台入口 |
| interaction-service | 边缘 | 2 | 会话有状态，用 Redis 共享 |
| speech-service | 边缘 | 1-2 | 重 CPU 任务，模型占用大 |
| intent-service | 中心 | 3 | 调用 LLM，需扩容应对并发 |
| task-memory-service | 中心 | 2 | 模板读多写少 |
| orchestrator-service | 边缘 | 2 | 低时延要求 |
| scheduler-service | 边缘 | 2 | 依赖本地机器人状态 |
| skill-runtime-service | 边缘 | 2 | 调度执行就近 |
| resource-service | 中心 | 2 | 资源状态全局一致 |
| robot-gateway | 边缘 | 2 | 协议适配常驻 |
| observability-service | 中心 | 2 | 聚合 + 索引 |

### 6.3 当前原型部署状态

```
┌─────────────────────────────────────────────┐
│  当前（原型阶段）：单进程本地部署            │
│  - web.py (8000)        WSGI               │
│  - speech_service.py (8010) FastAPI        │
│  - 内部模块: app, understanding,            │
│    memory, orchestrator, scheduler,        │
│    runtime, domain, bootstrap              │
└─────────────────────────────────────────────┘
```

所有业务模块当前合并在 `humanoid_fleet` 包内运行，**未做进程级微服务拆分**，但模块边界已按上述微服务划分保留，为后续拆分做准备。

---

## 7. 关键数据流

### 7.1 用户下单到执行完成

```
[用户] 
  → (1) 语音输入
  → interaction-bff (8000)
  → (2) gRPC: speech-service.transcribe(audio)
  → interaction-service (3) 维护会话上下文
  → (4) gRPC: intent-service.interpret(text)
  → (5) gRPC: task-memory.find_template(intent)
  → (6) 命中 SOP → 进入"等待确认"状态
  → (7) 用户点击确认
  → orchestrator-service (8) build_workflow
  → (9) gRPC: scheduler-service.assign
  → (10) gRPC: skill-runtime-service.execute
  → (11) 通过 robot-gateway 下发到机器人
  → (12) 事件: SkillStarted / SkillCompleted
  → interaction-service (13) 推送用户反馈（TTS）
  → (14) 事件全部入 observability-service
```

### 7.2 异常重规划

```
skill-runtime-service 检测到失败
  → 发布事件 SkillFailed(reason)
  → orchestrator-service 订阅并重规划
  → scheduler-service 重分配
  → 发布 RobotAssigned / WorkflowReplanned
  → interaction-service 推送给用户
  → observability-service 全程记录
```

---

## 8. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 通信协议 | gRPC + 事件总线 | 高性能、强契约、异步解耦 |
| 事件总线 | Kafka（主）/ NATS（边缘） | Kafka 用于回放，NATS 用于低时延 |
| 任务理解 | 模型优先 + 规则兜底 | 模型擅长理解，规则保证确定性 |
| 执行优先级 | SOP > Skill > Agent | 确定性、稳定性、低时延 |
| 部署形态 | 中心 + 边缘 + 本体 | 全局一致性与本地自治兼顾 |
| 状态管理 | 写时通过事件同步，读时直连 | 避免分布式事务 |
| 失败处理 | 重试 → 重分配 → 重规划 → 人工接管 | 多级降级 |
| 监控 | OpenTelemetry + Prometheus | 标准化、可移植 |

---

## 9. 高可用与容错

### 9.1 故障隔离

- **进程级隔离**：每个微服务独立进程，单服务崩溃不影响其他服务
- **资源隔离**：通过 Kubernetes namespace / 资源配额隔离
- **限流熔断**：gRPC 链路使用 sentinel / istio 限流

### 9.2 数据可靠性

- **Postgres**：主从复制 + WAL 归档
- **Kafka**：副本数 ≥ 3，关键事件保留 7 天
- **Redis**：持久化 + 主从，故障自动切换

### 9.3 降级策略

| 故障 | 降级行为 |
|------|----------|
| 中心服务不可用 | 边缘层继续处理已注册模板任务 |
| LLM 不可用 | 切换到规则解释器（`RuleFallbackInterpreter`） |
| 语音服务不可用 | 降级为纯文本输入 |
| 机器人失联 | 超时检测 → 任务迁移到其他机器人 |
| Kafka 不可用 | 关键事件落本地 WAL，恢复后回补 |

### 9.4 一致性策略

- **服务内**：强一致（Postgres 事务）
- **服务间**：最终一致（事件总线 + 幂等消费）
- **状态机**：所有工作流状态机用乐观锁防并发

---

## 10. 安全设计

- **认证**：服务间 mTLS（Istio / Linkerd）
- **授权**：RBAC（控制 / 运营 / 审计三权分立）
- **审计**：所有任务下发、接管、取消操作全量留痕
- **数据分级**：用户数据 / 会话数据 / 日志数据分级存储
- **安全策略**：高风险动作必须经过策略引擎校验
- **LLM 隔离**：LLM 不直接进控制闭环，仅产出 Intent 后由确定性系统执行

---

## 11. 可观测性

| 维度 | 工具 | 关键指标 |
|------|------|----------|
| 指标 | Prometheus | QPS、延迟、错误率、模板命中率 |
| 日志 | Loki | 结构化 JSON，含 trace_id |
| 追踪 | Jaeger / OTel | 端到端 trace，含 LLM 调用、gRPC、事件 |
| 事件 | Kafka + MinIO | 全量事件归档，可回放 |
| 告警 | Alertmanager | 任务失败率、机器人失联、LLM 超时 |

**关键看板**：
- 任务成功率（按模板）
- 平均响应时延（按链路）
- 机器人健康度
- LLM 调用成本
- 模板命中率

---

## 12. 演进路线

### 12.1 阶段一：原型（当前）✅

- ✅ 单进程实现所有核心模块
- ✅ Web UI + 语音服务
- ✅ Mock / Ollama LLM
- ✅ 2 个模板、2 个演示机器人
- ✅ 端到端流程打通

**当前状态**：模块边界已按微服务原则划分，但**未做进程级拆分**。

### 12.2 阶段二：微服务拆分（3 个月）

- 将 `humanoid_fleet` 拆为独立 Python 包 / Go 服务
- 引入 Kafka / NATS
- 引入 gRPC + Protobuf IDL
- 引入 Postgres + Redis
- 引入 K3s 边缘部署
- 接入 ROS 2 机器人

### 12.3 阶段三：平台化（6 个月）

- 多门店联邦
- 模板审批工作流
- 经验沉淀与策略学习
- 数字孪生仿真
- 跨场景复用

---

## 13. 关键架构抓手

1. **微服务 + 事件**：通过微服务拆分领域边界，通过事件总线解耦服务依赖
2. **模型优先 + 规则兜底**：LLM 解决开放理解，规则保证关键路径确定性
3. **SOP > Skill > Agent**：执行优先级固化在 `ExecutionResolver`，保证可控性
4. **边云协同**：边缘承担低时延编排与执行，中心承担全局一致性与 LLM
5. **透明可解释**：所有关键决策有 explanation，关键状态有 trace，关键事件可回放

---

## 14. 附录：当前原型模块对应微服务

| 当前文件 | 归属微服务 | 备注 |
|----------|------------|------|
| `web.py` | interaction-bff | BFF，渲染 UI + 语音中转 |
| `speech_service.py` | speech-service | 已独立部署 ✅ |
| `app.py` (FleetControlApp) | interaction-bff | 串联各模块的 BFF 逻辑 |
| `understanding.py` | intent-service | 模型解析 + 规则兜底 |
| `memory.py` | task-memory-service | 模板匹配 |
| `resolution.py` | orchestrator-service | SOP/Skill/Agent 决策 |
| `orchestrator.py` | orchestrator-service | 工作流构建 |
| `scheduler.py` | scheduler-service | 机器人分配 |
| `runtime.py` | skill-runtime-service | 执行计划生成 |
| `domain.py` | 公共 | 数据类 |
| `bootstrap.py` | scheduler-service | 演示用机器人池 |

---

*本文档反映**目标微服务架构**与**当前原型实现**的对应关系。阶段二将完成真正的进程级微服务拆分。*
