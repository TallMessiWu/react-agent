# ReAct Agent — GitHub 仓库分析

[![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=astral&logoColor=white)](https://docs.astral.sh/uv/)
[![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-1E40AF)](https://api-docs.deepseek.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

> **零框架手写**的生产级 ReAct Agent。DeepSeek 驱动 LLM 推理 + 自主调用 GitHub REST API 完成仓库探索，内置 **重试 / 熔断 / 降级** 三层弹性保护，输出结构化分析报告。

---

## 项目卖点

| 维度 | 实现 | 工程价值 |
|---|---|---|
| **零框架** | 不用 LangChain / AutoGen / CrewAI，纯 `openai` + `httpx` 手写 ReAct 主循环 | 完全掌控控制流，便于面试讲清原理 |
| **真实 API** | GitHub REST API v3，处理 Base64、限流、重定向、404 | 非玩具 demo，能跑真实仓库 |
| **三层弹性** | 指数退避重试 + 三态熔断器 + 工具级降级 | 生产级容错，借鉴 Hystrix / Polly 思想 |
| **异步全栈** | `asyncio` + `httpx.AsyncClient` + `AsyncOpenAI`，连接池复用 | 工具调用并行执行，延迟显著降低 |
| **可观测** | CoT + reasoning_content + 工具入参/出参全打印 | 调试友好，便于排查 Agent 决策链 |

---

## Demo

```bash
$ uv run python agent.py

═════════════════════════════════════════════════════════════════
   🚀 ReAct Agent — GitHub 仓库分析
   工具: 仓库信息 + 目录浏览 + 文件读取 + 提交历史
   特性: 指数退避重试 + Circuit Breaker + 降级容错
═════════════════════════════════════════════════════════════════

─────────────────────────────────────────────────────────────────
  [全面分析] 请全面分析 GitHub 仓库 https://github.com/tiangolo/fastapi ...
─────────────────────────────────────────────────────────────────

  [轮次 1] 💭 我需要先了解仓库整体概况
           🎯 → get_repo_info
           get_repo_info(owner=tiangolo, repo=fastapi)
           ← {"ok": true, "stars": 78532, "language": "Python", ...}

  [轮次 2] 💭 接下来看根目录结构
           🎯 → list_directory
           list_directory(owner=tiangolo, repo=fastapi, path=)
           ← {"ok": true, "items": [{"name": "README.md", ...}, ...]}

  [轮次 3] 🎯 → read_file, get_commits     ← 并行调用 2 个工具
  ...

  [轮次 6] LLM 最终回复 ✓

═════════════════════════════════════════════════════════════════
  📊 分析报告
═════════════════════════════════════════════════════════════════
# FastAPI 仓库分析报告
## 项目概况
FastAPI 是一个现代化的 Python Web 框架...
```

---

## 架构

```
┌───────────────────────────────────────────────────────────────┐
│                    Agent Loop (ReAct)                         │
│   Thought → Action → Observation → Thought → ... → Final      │
│   LLM 自主决策调用顺序，非固定流程                            │
└───────────────────┬───────────────────────────────────────────┘
                    ▼
┌───────────────────────────────────────────────────────────────┐
│                 弹性保护层 (Resilience)                       │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐          │
│  │  Retry      │ → │  Circuit    │ → │  Fallback   │          │
│  │  指数退避    │   │  Breaker    │   │  降级数据    │          │
│  │  + 抖动      │   │  三态状态机  │   │  优雅容错    │          │
│  └─────────────┘   └─────────────┘   └─────────────┘          │
└───────────────────┬───────────────────────────────────────────┘
                    ▼
┌───────────────────────────────────────────────────────────────┐
│                    工具层 (4 Tools)                           │
│  get_repo_info │ list_directory │ read_file │ get_commits     │
└───────────────────┬───────────────────────────────────────────┘
                    ▼
┌───────────────────────────────────────────────────────────────┐
│           HTTP 层 (httpx.AsyncClient)                         │
│           GitHub REST API v3                                  │
└───────────────────────────────────────────────────────────────┘
```

---

## 快速开始

### 前置条件

1. 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)（Astral 出品的 Python 包管理器，比 pip 快 10–100×）
2. 申请 [DeepSeek API Key](https://platform.deepseek.com/)（首次注册赠送额度）
3. 可选：[GitHub Personal Access Token](https://github.com/settings/tokens)（未认证 60 次/小时，已认证 5000 次/小时）

### 运行

```bash
# 1. 克隆
git clone https://github.com/TallMessiWu/react-agent.git
cd react-agent

# 2. 配置环境变量
cp .env.example .env       # 或手动新建
# 编辑 .env：
#   API_KEY=sk-xxx              # DeepSeek API Key（必需）
#   GITHUB_TOKEN=ghp_xxx        # GitHub Token（可选，强烈建议配置）

# 3. 安装依赖 + 一键运行
uv sync
uv run python agent.py
```

### 用作模块

```python
import asyncio
import httpx
from openai import AsyncOpenAI
from agent import run_agent

async def analyze(repo_url: str) -> str:
    llm = AsyncOpenAI(api_key="sk-xxx", base_url="https://api.deepseek.com")
    async with httpx.AsyncClient(follow_redirects=True) as http:
        return await run_agent(llm, http, f"分析 {repo_url}")

report = asyncio.run(analyze("https://github.com/pallets/flask"))
print(report)
```

---

## 工具列表

| 工具 | 输入 | 用途 |
|---|---|---|
| **`get_repo_info`** | `owner`, `repo` | 仓库元信息（星标、Fork、语言、许可证、活跃度） |
| **`list_directory`** | `owner`, `repo`, `path?` | 列出目录条目，自动判定文件/子目录 |
| **`read_file`** | `owner`, `repo`, `path` | 读取文本文件内容，自动 Base64 解码，超 6000 字截断 |
| **`get_commits`** | `owner`, `repo`, `per_page?` | 最近提交记录（SHA + 信息 + 作者 + 日期） |

所有工具用 **OpenAI Function Calling JSON Schema** 注册，遵循"语义化命名 + 详细 description + 精确 required"原则。

---

## 工程深度

### 1. ReAct 主循环

```python
messages = [system_prompt, user_query]
for turn in range(max_turns):
    msg = await llm.chat.completions.create(messages=messages, tools=TOOLS, tool_choice="auto")
    if msg.tool_calls:
        for tc in msg.tool_calls:
            result = await TOOL_EXECUTORS[tc.function.name](**json.loads(tc.function.arguments))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
    else:
        return msg.content     # LLM 决定不再调工具 → 输出最终报告
```

### 2. 指数退避 + 随机抖动

```python
delay = min(base_delay * (2 ** attempt), max_delay)
jitter = delay * 0.3 * random.random()
await asyncio.sleep(delay + jitter)
```

**为什么加抖动？** 避免「雷鸣羊群效应」(thundering herd) — 多个客户端在同一秒齐刷刷重试，会再次打垮刚恢复的服务。

### 3. 三态熔断器

```
                  失败次数达阈值
   CLOSED ─────────────────────────► OPEN
     ▲                                  │
     │  探测成功                         │ reset_timeout 后
     │                                  ▼
   HALF_OPEN ◄─────────────── (允许一次探测)
```

- **CLOSED**：正常放行
- **OPEN**：直接降级，**不发起请求**，保护下游 API 配额
- **HALF_OPEN**：超时后允许 1 次试探，成功则回到 CLOSED，失败则再次 OPEN

### 4. GitHub API 特殊处理

| 情况 | 处理 |
|---|---|
| `301/302` 重定向（仓库已迁移） | 跟随 `Location` 头或响应体 `url` 字段重试 |
| `403` + `X-RateLimit-Remaining: 0` | 读取 `Retry-After` 头，等待后继续（不消耗重试次数） |
| `429` Rate Limit | 同上 |
| `404` | 立即返回，不重试（请求本身有问题） |
| 5xx | 指数退避重试 |
| 文本文件 Base64 | 自动解码为 UTF-8 |

---

## 面试要点

<details>
<summary><b>Q1: 为什么用 ReAct 而不是 Chain？</b></summary>

分析陌生仓库时无法预知需要读几个文件、探索几层目录。固定 Chain 要么读太少（漏关键信息），要么读太多（浪费 token + API 配额）。ReAct 让 LLM 根据每一步的实际返回**动态调整**下一步动作，对探索性任务效果显著优于固定流程。
</details>

<details>
<summary><b>Q2: Circuit Breaker 三态机的工程价值？</b></summary>

源自 Netflix Hystrix 的经典模式。当下游 API 故障时，**继续无脑重试会浪费 API 配额 + 拖累自己**。熔断器在失败次数达阈值后切到 OPEN 状态，**直接降级**，给下游服务恢复时间；超时后切到 HALF_OPEN 试探，成功则回到正常。这是分布式系统教科书级容错模式。
</details>

<details>
<summary><b>Q3: 为什么熔断器要按工具粒度而非全局？</b></summary>

GitHub 不同 endpoint 故障率不同 — `read_file` 可能因某个文件特殊导致连续失败，但 `get_repo_info` 仍然正常。**按工具粒度熔断**避免一个工具的故障误伤其他工具。
</details>

<details>
<summary><b>Q4: Tool Schema 设计有什么讲究？</b></summary>

- **description 极度详细**：LLM 只能通过 description 理解工具用途，含糊就会乱调
- **required 精确**：必填参数要列全，可选参数要标可选
- **命名语义化**：`get_repo_info` > `getInfo`，让 LLM 一眼看懂
- **加"适用场景"**：在 description 末尾点明"什么时候用这个工具"，可显著提升调用准确率
</details>

<details>
<summary><b>Q5: 为什么用 asyncio 而不是 thread pool？</b></summary>

LLM 调用 + HTTP 请求都是 **I/O 密集**，asyncio 单线程事件循环比线程池开销低得多。当 LLM 一次返回多个 `tool_calls` 时，asyncio 可以**并发**执行所有工具调用，延迟取决于最慢的那个，而非所有调用之和。
</details>

---

## 项目结构

```
react-agent/
├── agent.py            # 主程序：Agent 循环 + 工具实现 + 弹性层
├── agent.ipynb         # 交互式 Notebook，分步骤讲解（教学/演示用）
├── pyproject.toml      # uv 项目定义
├── README.md           # 你正在看的文件
└── .env                # API_KEY + GITHUB_TOKEN（不提交）
```

源码仅 **~720 行**，分 6 段：配置中心 → 弹性层 → 工具实现 → Tool Schema → Agent 循环 → 主程序，可以从上到下顺读。

---

## 后续路线

- [ ] **缓存层**：用 SQLite 缓存 GitHub 响应，相同仓库重复分析时省 API 配额
- [ ] **多仓库对比**：一次分析多个仓库并生成对比报告
- [ ] **流式输出**：用 `stream=True` 让分析报告边生成边显示
- [ ] **MCP 适配**：把 4 个工具封装成 MCP Server，被任意 MCP 客户端调用
- [ ] **测试覆盖**：补充 `pytest` + `respx` mock 测试弹性层边界条件

---

## License

[MIT](./LICENSE) © 2026 Junlin Wu
