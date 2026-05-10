# 🤖 ReAct Agent — GitHub 仓库分析

> 从零构建的生产级 ReAct Agent，DeepSeek API 驱动，自主调用 GitHub REST API 完成仓库探索，生成结构化分析报告。

## 架构

```
┌─────────────────────────────────────────────────────┐
│                  Agent Loop (ReAct)                 │
│  感知 → 决策 → 执行 → 反馈 → 再决策...               │
├─────────────────────────────────────────────────────┤
│  弹性保护层                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐         │
│  │  重试层   │→ │ 熔断层   │→ │ 降级层   │          │
│  │ 指数退避  │  │ 三态机   │  │ 优雅容错  │          │
│  └──────────┘  └──────────┘  └──────────┘         │
├─────────────────────────────────────────────────────┤
│  工具层                                             │
│  get_repo_info │ list_directory │ read_file │ commits │
├─────────────────────────────────────────────────────┤
│  HTTP 层 (httpx + GitHub REST API v3)               │
└─────────────────────────────────────────────────────┘
```

## 快速开始

```bash
# 安装依赖
uv sync

# 运行
uv run python agent.py
```

**前置条件**：`.env` 中配置 `API_KEY`（DeepSeek），可选 `GITHUB_TOKEN`（提升 API 限额至 5000 次/小时）。

## 技术栈

`Python 3.14` · `DeepSeek API (OpenAI 兼容)` · `httpx` · `asyncio` · `Circuit Breaker`

## 核心特性

| 层次 | 技术点 | 工程价值 |
|------|--------|---------|
| **Agent 循环** | ReAct 模式 (Thought → Action → Observation) | LLM 自主决策工具调用序列，非固定流程 |
| **重试机制** | 指数退避 + 随机抖动 | 避免雷鸣羊群效应，给服务恢复时间 |
| **熔断器** | CLOSED → OPEN → HALF_OPEN 三态状态机 | 防止对故障 API 持续无效请求，保护配额 |
| **降级容错** | 工具级 fallback 数据 | Agent 优雅告知用户，而非直接崩溃 |
| **GitHub API** | REST API v3 + Base64 解码 + 限流处理 | 真实网络调用，非模拟数据 |
| **Tool Schema** | OpenAI Function Calling 格式，4 工具 | 语义化命名 + 详细 description + 精确 required |

## 工具列表

- **get_repo_info** — 仓库基本信息（星标、Fork、语言、许可证）
- **list_directory** — 目录结构浏览
- **read_file** — 文件内容读取（最长 6000 字符）
- **get_commits** — 最近提交记录

## 面试要点

1. **为什么 ReAct 而非 Chain？** — 分析陌生仓库时无法预知需读几个文件、探索几层目录，ReAct 让 LLM 根据实际返回动态调整
2. **Circuit Breaker 三态的工程价值** — 统计分析框架 Hystrix 的经典模式，防止故障雪崩
3. **指数退避 + 随机抖动** — 避免雷鸣羊群效应（thundering herd）
4. **工具 Schema 设计原则** — description 极度详细、required 精确、命名语义化

## License

MIT
