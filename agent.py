"""
项目 1: 手写 ReAct Agent — GitHub 仓库分析
==========================================
从零构建生产级 ReAct Agent，使用 DeepSeek API 驱动，
自主调用 GitHub REST API 完成仓库探索，生成结构化分析报告。

架构栈（自底向上）：
  ① HTTP 层     — httpx + GitHub REST API v3（真实网络调用）
  ② 工具层      — 4 工具：仓库信息 / 目录浏览 / 文件读取 / 提交历史
  ③ 弹性层      — 重试(指数退避+抖动) + 熔断器(三态) + 降级
  ④ Agent 循环  — ReAct (Reasoning + Acting)，LLM 自主决策调用序列
  ⑤ 报告生成     — Agent 综合信息生成结构化分析

与 exercises 练习的核心区别：
  练习用模拟数据 → 本项目用真实 GitHub API
  练习教学导向     → 本项目是可演示的完整简历作品
  练习单工具场景   → 本项目多工具协同完成复杂分析任务

运行方式：
  uv run python react-agent/agent.py

前置条件：
  .env 中需配置 API_KEY（DeepSeek），可选 GITHUB_TOKEN（提升限额至 5000 次/小时）

==================== 参考资料 ====================
  GitHub REST API:       https://docs.github.com/en/rest
  DeepSeek API:          https://api-docs.deepseek.com/
  ReAct 论文:            https://arxiv.org/abs/2210.03629
  Building Effective Agents: https://www.anthropic.com/research/building-effective-agents
  Circuit Breaker 模式:  https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker
==================== 参考资料 ====================
"""

import asyncio
import json
import os
import random
import re
import sys
import time
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ╔══════════════════════════════════════════════════════════════════╗
# ║                    第一部分：配置中心                             ║
# ╚══════════════════════════════════════════════════════════════════╝

GITHUB_API_BASE = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # 可选，未配置则走未认证访问（60次/小时）

RESILIENCE_CONFIG = {
    "max_retries": 3,
    "base_delay": 0.5,
    "max_delay": 8.0,
    "retryable_statuses": (429, 500, 502, 503, 504),
    "circuit_breaker_threshold": 5,
    "circuit_breaker_reset": 30,
    "request_timeout": 15.0,
}

MODEL = "deepseek-v4-flash"

SYSTEM_PROMPT = """你是一个专业的 GitHub 仓库分析师。你拥有以下工具来探索和分析 GitHub 仓库：

1. **get_repo_info** — 获取仓库基本信息（星标、Fork、语言、描述、许可证、创建/更新时间）
2. **list_directory** — 列出仓库目录结构（文件和子目录）
3. **read_file** — 读取仓库中指定文件的内容
4. **get_commits** — 获取最近的提交记录

分析工作流（你自主决定调用顺序，以下为推荐模式）：
  第一步：调用 get_repo_info 了解仓库概况
  第二步：调用 list_directory（根目录）了解项目结构
  第三步：根据目录结构，读取关键文件（README.md、package.json/pyproject.toml/go.mod 等）
  第四步：调用 get_commits 了解近期开发活跃度
  第五步：综合所有信息，输出一份结构化的分析报告

输出要求：
  - 使用 Markdown 格式组织报告
  - 报告应包含：项目概况、技术栈、目录结构解读、开发活跃度、亮点与风险
  - 信息密度高，避免空洞评价
  - 如果某个工具返回了错误或降级数据，如实说明，不要编造"""


# ╔══════════════════════════════════════════════════════════════════╗
# ║              第二部分：弹性层（重试 + 熔断 + 降级）              ║
# ╚══════════════════════════════════════════════════════════════════╝

class CircuitBreaker:
    """熔断器 — 三态状态机（CLOSED → OPEN → HALF_OPEN → CLOSED）。

    防止对已故障的 GitHub API 持续发起无效请求，避免浪费 API 配额。
    """

    def __init__(self, name: str, threshold: int, reset_timeout: float):
        self.name = name
        self.threshold = threshold
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "CLOSED"

    def allow_request(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time >= self.reset_timeout:
                self.state = "HALF_OPEN"
                return True
            return False
        return True  # HALF_OPEN: 允许一次探测

    def record_success(self):
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.threshold:
            self.state = "OPEN"

    @property
    def status(self) -> str:
        return f"[{self.state} | failures={self.failure_count}/{self.threshold}]"


# 全局熔断器实例（每个工具一个）
_CIRCUIT_BREAKERS: dict[str, CircuitBreaker] = {}


def _get_cb(name: str) -> CircuitBreaker:
    if name not in _CIRCUIT_BREAKERS:
        _CIRCUIT_BREAKERS[name] = CircuitBreaker(
            name,
            threshold=RESILIENCE_CONFIG["circuit_breaker_threshold"],
            reset_timeout=RESILIENCE_CONFIG["circuit_breaker_reset"],
        )
    return _CIRCUIT_BREAKERS[name]


def _is_retryable(status_code: int) -> bool:
    """判断 HTTP 状态码是否属于可重试错误。

    429 (Rate Limit) → 可重试（等待后恢复）
    5xx (Server Error) → 可重试（服务端瞬态故障）
    4xx (except 429) → 不可重试（请求本身有问题）
    """
    return status_code in RESILIENCE_CONFIG["retryable_statuses"]


async def resilient_github_request(
    tool_name: str,
    url: str,
    client: httpx.AsyncClient,
    verbose: bool = True,
) -> dict:
    """带完整弹性保护的 GitHub API 请求。

    执行链路：
      ① 熔断器检查 → 已熔断则直接降级
      ② HTTP 请求（带超时）
      ③ 状态码检查 → 可重试则指数退避重试
      ④ 全部失败 → 返回降级数据

    GitHub API 特定处理：
      - 403 + X-RateLimit-Remaining=0 → 读取 Retry-After 头等待
      - 404 → 不可重试（资源不存在）
    """
    cb = _get_cb(tool_name)
    cfg = RESILIENCE_CONFIG

    if not cb.allow_request():
        if verbose:
            print(f"  ⚡ [{tool_name}] 熔断器开路 {cb.status}，直接降级")
        return _fallback_for(tool_name)

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "react-agent/1.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    last_status = None

    for attempt in range(cfg["max_retries"] + 1):
        try:
            response = await client.get(
                url,
                headers=headers,
                timeout=cfg["request_timeout"],
            )

            if response.status_code == 200:
                cb.record_success()
                return {"ok": True, "data": response.json()}

            if response.status_code == 404:
                cb.record_success()
                return {"ok": False, "error": f"资源不存在 (404): {url}"}

            if response.status_code in (301, 302):
                # GitHub repo renamed/transferred → 提取新 URL 并跟随
                redirect_url = None
                try:
                    body = response.json()
                    redirect_url = body.get("url")
                except Exception:
                    pass
                if not redirect_url:
                    redirect_url = response.headers.get("Location")
                if redirect_url:
                    if verbose:
                        print(f"  ↪ [{tool_name}] 跟随重定向 → {redirect_url}")
                    url = redirect_url
                    continue  # 用新 URL 重试

            if response.status_code in (403, 429):
                # GitHub 速率限制 → 检查是否可等待
                remaining = response.headers.get("X-RateLimit-Remaining", "?")
                if response.status_code == 429 or remaining == "0":
                    retry_after = response.headers.get("Retry-After", "60")
                    wait = min(float(retry_after), cfg["max_delay"])
                    if verbose:
                        print(f"  ⏳ [{tool_name}] API 限流，等待 {wait:.0f}s...")
                    await asyncio.sleep(wait)
                    continue  # 不消耗重试次数——速率限制恢复后可继续

            # 其他非 200 状态码
            last_status = response.status_code
            if not _is_retryable(response.status_code):
                cb.record_failure()
                return {"ok": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_status = type(e).__name__
        except Exception as e:
            last_status = f"{type(e).__name__}: {e}"
            cb.record_failure()
            if attempt == cfg["max_retries"]:
                return {"ok": False, "error": str(e)}
            break

        cb.record_failure()

        if attempt < cfg["max_retries"]:
            delay = min(cfg["base_delay"] * (2 ** attempt), cfg["max_delay"])
            jitter = delay * 0.3 * random.random()
            wait = delay + jitter
            if verbose:
                print(f"  ↻ [{tool_name}] 第 {attempt+1}/{cfg['max_retries']} 次重试 "
                      f"({wait:.1f}s, status={last_status})")
            await asyncio.sleep(wait)

    if verbose:
        print(f"  ▼ [{tool_name}] 全部重试失败 {cb.status}，降级返回")
    return _fallback_for(tool_name)


def _fallback_for(tool_name: str) -> dict:
    """降级数据——让 Agent 能优雅告知用户，而非直接崩溃。"""
    fallbacks = {
        "get_repo_info": {
            "ok": False,
            "error": "仓库信息暂时不可用（API 限流或网络故障）",
            "_fallback": True,
        },
        "list_directory": {
            "ok": False,
            "error": "目录列表暂时不可用",
            "_fallback": True,
        },
        "read_file": {
            "ok": False,
            "error": "文件内容暂时不可用",
            "_fallback": True,
        },
        "get_commits": {
            "ok": False,
            "error": "提交历史暂时不可用",
            "_fallback": True,
        },
    }
    return fallbacks.get(tool_name, {"ok": False, "error": "服务不可用", "_fallback": True})


# ╔══════════════════════════════════════════════════════════════════╗
# ║              第三部分：工具实现（GitHub API 调用）               ║
# ╚══════════════════════════════════════════════════════════════════╝

def parse_repo_url(user_input: str) -> tuple[str, str] | None:
    """从用户输入中提取 GitHub owner/repo。

    支持格式：
      https://github.com/owner/repo
      https://github.com/owner/repo.git
      https://github.com/owner/repo/tree/branch/path
      owner/repo
    """
    # 尝试匹配完整 URL
    url_match = re.search(r'github\.com[:/]([^/]+)/([^/\s#.]+)', user_input)
    if url_match:
        return url_match.group(1), url_match.group(2).removesuffix(".git")

    # 尝试匹配 owner/repo 简写
    short_match = re.match(r'^([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)$', user_input.strip())
    if short_match:
        return short_match.group(1), short_match.group(2)

    return None


async def get_repo_info(owner: str, repo: str, client: httpx.AsyncClient,
                        verbose: bool = True) -> dict:
    """获取 GitHub 仓库基本信息。"""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
    result = await resilient_github_request("get_repo_info", url, client, verbose)

    if not result.get("ok"):
        return result

    data = result["data"]
    return {
        "ok": True,
        "name": data.get("full_name"),
        "description": data.get("description"),
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
        "language": data.get("language"),
        "topics": data.get("topics", []),
        "license": data.get("license", {}).get("spdx_id") if data.get("license") else None,
        "default_branch": data.get("default_branch"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "size_kb": data.get("size"),
        "archived": data.get("archived", False),
    }


async def list_directory(owner: str, repo: str, path: str = "",
                         client: httpx.AsyncClient = None, verbose: bool = True) -> dict:
    """列出仓库目录内容。"""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    if not path:
        url = url.rstrip("/")
    result = await resilient_github_request("list_directory", url, client, verbose)

    if not result.get("ok"):
        return result

    data = result["data"]

    # 如果是单个文件（非目录），返回文件信息
    if isinstance(data, dict) and data.get("type") == "file":
        return {
            "ok": True,
            "path": path,
            "is_directory": False,
            "items": [{"name": data["name"], "type": "file", "size": data.get("size", 0)}],
        }

    # 目录 → 返回条目摘要列表
    if isinstance(data, list):
        items = []
        for item in data:
            items.append({
                "name": item["name"],
                "type": item["type"],  # "file" or "dir"
                "size": item.get("size", 0) if item["type"] == "file" else None,
            })
        return {"ok": True, "path": path or "/", "is_directory": True, "items": items}

    return {"ok": False, "error": f"意外的响应格式: {type(data)}"}


async def read_file(owner: str, repo: str, path: str,
                    client: httpx.AsyncClient, verbose: bool = True) -> dict:
    """读取仓库中的文件内容（文本文件，最长 6000 字符）。"""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    result = await resilient_github_request("read_file", url, client, verbose)

    if not result.get("ok"):
        return result

    data = result["data"]

    if isinstance(data, list):
        return {"ok": False, "error": f"'{path}' 是目录而非文件，请使用 list_directory"}
    if not isinstance(data, dict):
        return {"ok": False, "error": f"意外的响应格式"}

    if data.get("encoding") == "base64":
        import base64
        try:
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            content = "[二进制文件，无法显示文本内容]"
    else:
        content = data.get("content", "[无内容]")

    max_chars = 6000
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + f"\n\n... [截断: 文件共 {len(content)} 字符]"

    return {
        "ok": True,
        "path": path,
        "size": data.get("size", 0),
        "content": content,
        "truncated": truncated,
    }


async def get_commits(owner: str, repo: str, per_page: int = 10,
                      client: httpx.AsyncClient = None, verbose: bool = True) -> dict:
    """获取仓库最近提交记录。"""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits?per_page={min(per_page, 30)}"
    result = await resilient_github_request("get_commits", url, client, verbose)

    if not result.get("ok"):
        return result

    commits = []
    for c in result["data"]:
        commits.append({
            "sha": c.get("sha", "")[:8],
            "message": c.get("commit", {}).get("message", "").split("\n")[0],
            "author": c.get("commit", {}).get("author", {}).get("name", "unknown"),
            "date": c.get("commit", {}).get("author", {}).get("date", ""),
        })
    return {"ok": True, "commits": commits, "count": len(commits)}


TOOL_EXECUTORS = {
    "get_repo_info": get_repo_info,
    "list_directory": list_directory,
    "read_file": read_file,
    "get_commits": get_commits,
}


# ╔══════════════════════════════════════════════════════════════════╗
# ║          第四部分：Tool Schema 定义（OpenAI 格式）               ║
# ╚══════════════════════════════════════════════════════════════════╝

GET_REPO_INFO_TOOL = {
    "type": "function",
    "function": {
        "name": "get_repo_info",
        "description": (
            "获取 GitHub 仓库的基本信息。"
            "返回：仓库全名、描述、星标数、Fork 数、主要语言、"
            "主题标签、开源许可证、默认分支、创建/更新时间。"
            "适用场景：了解仓库整体概况，作为分析的第一步。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "仓库拥有者（用户名或组织名），如 'tiangolo'、'langchain-ai'",
                },
                "repo": {
                    "type": "string",
                    "description": "仓库名称，如 'fastapi'、'langchain'",
                },
            },
            "required": ["owner", "repo"],
        },
    },
}

LIST_DIRECTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": (
            "列出仓库中指定路径下的文件和子目录。"
            "如果路径指向目录，返回其中所有条目（名称、类型、大小）；"
            "如果路径指向文件，返回该文件的元信息。"
            "适用场景：了解项目目录结构，发现关键文件（README、配置文件、源码目录）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "仓库拥有者",
                },
                "repo": {
                    "type": "string",
                    "description": "仓库名称",
                },
                "path": {
                    "type": "string",
                    "description": "目录路径。空字符串或 '/' 表示根目录。例如 'src'、'tests'、'docs'",
                },
            },
            "required": ["owner", "repo"],
        },
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读取仓库中指定文件的内容（仅文本文件）。"
            "自动处理 GitHub 的 Base64 编码。内容最长 6000 字符，超出部分会截断并标注。"
            "适用场景：阅读 README、配置文件(pyproject.toml/package.json/go.mod)、"
            "核心源码文件等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "仓库拥有者",
                },
                "repo": {
                    "type": "string",
                    "description": "仓库名称",
                },
                "path": {
                    "type": "string",
                    "description": "文件路径，如 'README.md'、'pyproject.toml'、'src/main.py'",
                },
            },
            "required": ["owner", "repo", "path"],
        },
    },
}

GET_COMMITS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_commits",
        "description": (
            "获取仓库最近的提交记录。"
            "每条包含：SHA 短码、提交信息、作者、日期。"
            "适用场景：评估项目近期开发活跃度、了解代码变更趋势。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "仓库拥有者",
                },
                "repo": {
                    "type": "string",
                    "description": "仓库名称",
                },
                "per_page": {
                    "type": "integer",
                    "description": "返回的提交数量，默认 10，最大 30",
                },
            },
            "required": ["owner", "repo"],
        },
    },
}

TOOLS = [GET_REPO_INFO_TOOL, LIST_DIRECTORY_TOOL, READ_FILE_TOOL, GET_COMMITS_TOOL]


# ╔══════════════════════════════════════════════════════════════════╗
# ║              第五部分：Agent 循环（ReAct 核心）                  ║
# ╚══════════════════════════════════════════════════════════════════╝

async def run_agent(
    llm_client: AsyncOpenAI,
    http_client: httpx.AsyncClient,
    user_query: str,
    model: str = MODEL,
    max_turns: int = 15,
    verbose: bool = True,
) -> str:
    """Agent 主循环 — ReAct (Reasoning + Acting) 模式。

    Args:
        llm_client: DeepSeek 异步客户端
        http_client: httpx 异步 HTTP 客户端（连接池复用）
        user_query: 用户输入（可包含 GitHub URL）
        model: 模型 ID
        max_turns: 最大 LLM 交互轮次
        verbose: 是否打印调试日志

    Returns:
        Agent 的最终分析报告（Markdown 格式）
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_query},
    ]

    for turn in range(1, max_turns + 1):
        response = await llm_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.0,
        )

        msg = response.choices[0].message

        if msg.tool_calls:
            names = [tc.function.name for tc in msg.tool_calls]
            if verbose:
                print(f"\n  [轮次 {turn}] LLM → {', '.join(names)}")

            messages.append(msg.model_dump())

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                tool_args = json.loads(tc.function.arguments)

                if verbose:
                    args_preview = ", ".join(f"{k}={v}" for k, v in tool_args.items())
                    print(f"           {tool_name}({args_preview})")

                func = TOOL_EXECUTORS.get(tool_name)
                if func is None:
                    result_json = json.dumps(
                        {"ok": False, "error": f"未知工具 '{tool_name}'"},
                        ensure_ascii=False,
                    )
                else:
                    try:
                        result = await func(**tool_args, client=http_client, verbose=verbose)
                        result_json = json.dumps(result, ensure_ascii=False)
                    except Exception as e:
                        result_json = json.dumps(
                            {"ok": False, "error": f"{type(e).__name__}: {e}"},
                            ensure_ascii=False,
                        )

                if verbose:
                    preview = result_json[:150] + "..." if len(result_json) > 150 else result_json
                    print(f"           ← {preview}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_json,
                })

        else:
            if verbose:
                print(f"\n  [轮次 {turn}] LLM 最终回复 ✓")
            return msg.content

    return "分析超时——仓库规模较大，建议缩小分析范围后重试。"


# ╔══════════════════════════════════════════════════════════════════╗
# ║              第六部分：主程序 & 测试用例                         ║
# ╚══════════════════════════════════════════════════════════════════╝

async def main():
    print("=" * 65)
    print("   🚀 项目 1: ReAct Agent — GitHub 仓库分析")
    print("   工具: 仓库信息 + 目录浏览 + 文件读取 + 提交历史")
    print("   特性: 指数退避重试 + Circuit Breaker + 降级容错")
    print("=" * 65)

    llm_client = AsyncOpenAI(
        api_key=os.getenv("API_KEY"),
        base_url="https://api.deepseek.com",
    )

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        test_cases = [
            (
                "全面分析",
                "请全面分析 GitHub 仓库 https://github.com/tiangolo/fastapi ，"
                "包括项目概况、技术栈、目录结构、开发活跃度，并给出亮点与改进建议。",
            ),
            (
                "快速概览",
                "用 5 句话总结 https://github.com/nicedayfor/deepseek-r1-1776 这个仓库。",
            ),
            (
                "架构分析",
                "分析 https://github.com/pallets/flask 的项目架构和目录结构，"
                "重点关注它是如何组织源代码和测试的。",
            ),
        ]

        for title, query in test_cases:
            print(f"\n{'─' * 65}")
            print(f"  [{title}] {query[:80]}...")
            print(f"{'─' * 65}")

            answer = await run_agent(llm_client, http_client, query, verbose=True)
            print(f"\n{'=' * 65}")
            print(f"  📊 分析报告")
            print(f"{'=' * 65}")
            print(answer)
            print()


if __name__ == "__main__":
    asyncio.run(main())
