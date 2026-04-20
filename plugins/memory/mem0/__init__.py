"""Mem0 本地自建插件 — MemoryProvider interface.

基于 Mem0 v2 开源 SDK + Qdrant 服务端向量库 + DashScope (qwen-max) 的本地记忆系统。
替代原版云端 API，全链路国内 API，提取语言为中文。

配置通过 ~/.hermes/mem0/config.py 管理（LLM、Embedding、向量库等）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 本地 Mem0 实例初始化
# ---------------------------------------------------------------------------

_mem0_instance = None
_mem0_lock = threading.Lock()
_mem0_initialized = False


def _get_local_mem0():
    """获取本地 Mem0 Memory 实例（线程安全、懒加载）。"""
    global _mem0_instance, _mem0_initialized
    with _mem0_lock:
        if _mem0_instance is not None:
            return _mem0_instance
        try:
            # 加载自定义配置
            config_path = os.path.expanduser("~/.hermes/mem0")
            if config_path not in sys.path:
                sys.path.insert(0, config_path)
            from config import MEM0_CONFIG

            from mem0 import Memory
            _mem0_instance = Memory.from_config(MEM0_CONFIG)
            _mem0_initialized = True
            logger.info("Mem0 local instance initialized successfully")
            return _mem0_instance
        except Exception as e:
            logger.error("Failed to initialize Mem0 local instance: %s", e)
            raise RuntimeError(f"Mem0 本地初始化失败: {e}")


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "name": "mem0_profile",
    "description": (
        "Retrieve all stored memories about the user — preferences, facts, "
        "project context. Fast, no reranking. Use at conversation start."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": (
        "Search memories by meaning. Returns relevant facts ranked by similarity. "
        "Use for recalling specific details about the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}

CONCLUDE_SCHEMA = {
    "name": "mem0_conclude",
    "description": (
        "Store a durable fact about the user. Stored verbatim (no LLM extraction). "
        "Use for explicit preferences, corrections, or decisions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "description": "The fact to store."},
        },
        "required": ["conclusion"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class Mem0LocalMemoryProvider(MemoryProvider):
    """Mem0 本地自建记忆系统（开源 SDK + Qdrant 服务端 + DashScope qwen-max）。"""

    def __init__(self):
        self._user_id = "hermes-user"
        self._agent_id = "hermes"
        self._client = None
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread = None
        self._sync_thread = None
        # Circuit breaker
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        _BREAKER_THRESHOLD = 5
        _BREAKER_COOLDOWN = 120

    @property
    def name(self) -> str:
        return "mem0-local"

    def is_available(self) -> bool:
        """检查本地配置是否就绪。"""
        config_path = os.path.expanduser("~/.hermes/mem0/config.py")
        if not os.path.exists(config_path):
            return False
        # 检查 DashScope API Key
        try:
            config_dir = os.path.expanduser("~/.hermes/mem0")
            if config_dir not in sys.path:
                sys.path.insert(0, config_dir)
            from config import DASHSCOPE_API_KEY
            return bool(DASHSCOPE_API_KEY)
        except Exception:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        self._user_id = kwargs.get("user_id") or "hermes-user"
        self._agent_id = kwargs.get("agent_id") or "hermes"
        try:
            self._client = _get_local_mem0()
            logger.info("Mem0 local provider initialized for user=%s", self._user_id)
        except Exception as e:
            logger.warning("Mem0 local init failed, will retry on first use: %s", e)

    def _get_client_safe(self):
        """安全获取客户端，失败时重新初始化。"""
        if self._client is not None:
            return self._client
        self._client = _get_local_mem0()
        return self._client

    # -- Circuit breaker -----------------------------------------------------

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < 5:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._breaker_open_until = time.monotonic() + 120
            logger.warning("Mem0 local circuit breaker tripped after %d failures", self._consecutive_failures)

    # -- Filters -------------------------------------------------------------

    def _read_filters(self) -> Dict[str, Any]:
        return {"user_id": self._user_id}

    def _write_user_id(self) -> str:
        return self._user_id

    # -- System prompt -------------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "# Mem0 Memory (本地自建)\n"
            f"Active. User: {self._user_id}. 使用 DashScope qwen-max + Qdrant 服务端向量库.\n"
            "Use mem0_search to find memories, mem0_conclude to store facts, "
            "mem0_profile for a full overview.\n"
            "所有记忆均以中文存储和提取。"
        )

    # -- Prefetch ------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## Mem0 Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_breaker_open():
            return

        def _run():
            try:
                client = self._get_client_safe()
                raw = client.search(query=query, filters=self._read_filters(), top_k=5)
                results = self._unwrap_results(raw)
                if results:
                    lines = [r.get("memory", "") for r in results if r.get("memory") and r.get("score", 0) > 0.3]
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(f"- {l}" for l in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Mem0 local prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="mem0-local-prefetch")
        self._prefetch_thread.start()

    # -- Sync turn -----------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """发送对话轮次到本地 Mem0 进行记忆提取（非阻塞）。"""
        if self._is_breaker_open():
            return

        def _sync():
            try:
                client = self._get_client_safe()
                messages = [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": assistant_content},
                ]
                client.add(messages, user_id=self._write_user_id())
                self._record_success()
                logger.debug("Mem0 local sync success for user=%s", self._user_id)
            except Exception as e:
                self._record_failure()
                logger.warning("Mem0 local sync failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="mem0-local-sync")
        self._sync_thread.start()

    # -- Tool schemas & dispatch ---------------------------------------------

    @staticmethod
    def _unwrap_results(response: Any) -> list:
        """Normalize Mem0 v2 API response — wraps results in {"results": [...]}."""
        if isinstance(response, dict):
            return response.get("results", [])
        if isinstance(response, list):
            return response
        return []

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [PROFILE_SCHEMA, SEARCH_SCHEMA, CONCLUDE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._is_breaker_open():
            return json.dumps({
                "error": "Mem0 本地服务暂时不可用（连续多次失败），将自动恢复。"
            })

        try:
            client = self._get_client_safe()
        except Exception as e:
            return tool_error(str(e))

        if tool_name == "mem0_profile":
            try:
                raw = client.get_all(filters=self._read_filters())
                memories = self._unwrap_results(raw)
                self._record_success()
                if not memories:
                    return json.dumps({"result": "暂无存储的记忆。"})
                lines = [m.get("memory", "") for m in memories if m.get("memory")]
                return json.dumps({"result": "\n".join(lines), "count": len(lines)})
            except Exception as e:
                self._record_failure()
                return tool_error(f"获取记忆失败: {e}")

        elif tool_name == "mem0_search":
            query = args.get("query", "")
            if not query:
                return tool_error("缺少必要参数: query")
            top_k = min(int(args.get("top_k", 10)), 50)
            try:
                raw = client.search(query=query, filters=self._read_filters(), top_k=top_k)
                results = self._unwrap_results(raw)
                self._record_success()
                if not results:
                    return json.dumps({"result": "未找到相关记忆。"})
                items = [{"memory": r.get("memory", ""), "score": r.get("score", 0)} for r in results if r.get("score", 0) > 0.3]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                self._record_failure()
                return tool_error(f"搜索失败: {e}")

        elif tool_name == "mem0_conclude":
            conclusion = args.get("conclusion", "")
            if not conclusion:
                return tool_error("缺少必要参数: conclusion")
            try:
                # 直接存储，不经过 LLM 提取
                client.add(conclusion, user_id=self._write_user_id(), infer=False)
                self._record_success()
                return json.dumps({"result": "记忆已存储。"})
            except Exception as e:
                self._record_failure()
                return tool_error(f"存储失败: {e}")

        return tool_error(f"Unknown tool: {tool_name}")

    # -- Shutdown ------------------------------------------------------------

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        # 本地 Mem0 实例不需要显式关闭（Qdrant 服务端独立运行，不需要进程内持久化）

    # -- Config schema (for setup wizard) ------------------------------------

    def get_config_schema(self):
        return []  # 本地版不需要额外配置，全在 config.py 里

    def save_config(self, values, hermes_home):
        pass  # 本地版不需要


def register(ctx) -> None:
    """Register Mem0 local as a memory provider plugin."""
    ctx.register_memory_provider(Mem0LocalMemoryProvider())