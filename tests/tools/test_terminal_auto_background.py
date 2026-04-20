"""Tests for terminal tool unified execution with auto-background.

Commands completing within 5 seconds return results immediately.
Commands exceeding 5 seconds are automatically moved to background execution.

The "spawn-first, release-later" pattern means:
  1. The command is always spawned via process_registry first
  2. A brief poll loop checks if it finished within AUTO_BACKGROUND_TIMEOUT
  3. If completed → return output + exit_code immediately, cleanup session
  4. If still running → return auto_backgrounded status with session_id
"""
import json
import threading
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import pytest


def _make_env_config(**overrides):
    """Return a minimal _get_env_config()-shaped dict with optional overrides."""
    config = {
        "env_type": "local",
        "timeout": 180,
        "cwd": "/tmp",
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
    }
    config.update(overrides)
    return config


def _make_mock_proc_session(
    session_id="proc_test_123",
    pid=12345,
    exited=False,
    exit_code=None,
    output_buffer="",
    command="echo hello",
):
    """Create a mock ProcessSession with controllable state."""
    session = SimpleNamespace(
        id=session_id,
        pid=pid,
        command=command,
        exited=exited,
        exit_code=exit_code,
        output_buffer=output_buffer,
        process=None,
        _reader_thread=None,
        _lock=threading.Lock(),
        _exited_event=threading.Event(),
        notify_on_complete=False,
        watcher_platform="",
        watcher_chat_id="",
        watcher_user_id="",
        watcher_user_name="",
        watcher_thread_id="",
        watcher_interval=0,
        watch_patterns=[],
    )
    if exited:
        session._exited_event.set()
    return session


class TestAutoBackground:
    """Unified execution: quick completion vs auto-background."""

    def test_quick_command_returns_immediately(self):
        """Commands completing quickly return results immediately."""
        from tools.terminal_tool import terminal_tool

        mock_session = _make_mock_proc_session(
            session_id="proc_quick",
            pid=111,
            exited=True,
            exit_code=0,
            output_buffer="hello\n",
        )

        mock_registry = MagicMock()
        mock_registry.spawn_local.return_value = mock_session
        mock_registry._completion_consumed = set()
        mock_registry._lock = threading.Lock()
        mock_registry._running = {}
        mock_registry._finished = {}

        mock_env = MagicMock()
        mock_env.env = {}

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""), \
             patch("tools.terminal_tool.is_interrupted", return_value=False), \
             patch("tools.interrupt.is_interrupted", return_value=False):
            result = json.loads(terminal_tool(command="echo hello"))

        assert result["exit_code"] == 0
        assert "hello" in result["output"]
        assert "session_id" not in result
        assert result["error"] is None

    def test_slow_command_auto_backgrounds(self):
        """Commands not completing within threshold auto-background with session_id."""
        from tools.terminal_tool import terminal_tool

        mock_session = _make_mock_proc_session(
            session_id="proc_slow_123",
            pid=12345,
            exited=False,
            exit_code=None,
            output_buffer="",
            command="sleep 10",
        )

        mock_registry = MagicMock()
        mock_registry.spawn_local.return_value = mock_session
        mock_registry._completion_consumed = set()
        mock_registry.pending_watchers = []

        mock_env = MagicMock()
        mock_env.env = {}

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""), \
             patch("tools.terminal_tool.is_interrupted", return_value=False), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("tools.terminal_tool.AUTO_BACKGROUND_TIMEOUT", 0), \
             patch("time.sleep"):
            result = json.loads(terminal_tool(command="sleep 10"))

        assert result["status"] == "auto_backgrounded"
        assert result["session_id"] == "proc_slow_123"
        assert result["pid"] == 12345
        assert result["notify_on_complete"] is True
        assert "background" in result["output"].lower()
        assert "process(action='poll'" in result["output"] or "process" in result["output"]

    def test_auto_background_enables_notify_on_complete(self):
        """Auto-backgrounded commands always have notify_on_complete enabled."""
        from tools.terminal_tool import terminal_tool

        mock_session = _make_mock_proc_session(
            session_id="proc_abc",
            pid=999,
            exited=False,
            exit_code=None,
            output_buffer="",
        )
        mock_session.notify_on_complete = False

        mock_registry = MagicMock()
        mock_registry.spawn_local.return_value = mock_session
        mock_registry._completion_consumed = set()
        mock_registry.pending_watchers = []

        mock_env = MagicMock()
        mock_env.env = {}

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""), \
             patch("tools.terminal_tool.is_interrupted", return_value=False), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("tools.terminal_tool.AUTO_BACKGROUND_TIMEOUT", 0), \
             patch("time.sleep"):
            result = json.loads(terminal_tool(command="long_running_task"))

        assert mock_session.notify_on_complete is True

    def test_quick_command_with_error_returncode(self):
        """Quick commands with non-zero exit codes still return immediately."""
        from tools.terminal_tool import terminal_tool

        mock_session = _make_mock_proc_session(
            session_id="proc_err",
            pid=222,
            exited=True,
            exit_code=1,
            output_buffer="error\n",
        )

        mock_registry = MagicMock()
        mock_registry.spawn_local.return_value = mock_session
        mock_registry._completion_consumed = set()
        mock_registry._lock = threading.Lock()
        mock_registry._running = {}
        mock_registry._finished = {}

        mock_env = MagicMock()
        mock_env.env = {}

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""), \
             patch("tools.terminal_tool.is_interrupted", return_value=False), \
             patch("tools.interrupt.is_interrupted", return_value=False):
            result = json.loads(terminal_tool(command="false"))

        assert result["exit_code"] == 1
        assert "error" in result["output"]
        assert "session_id" not in result

    def test_workdir_passed_to_spawn(self):
        """Workdir parameter is passed to spawn_local as cwd."""
        from tools.terminal_tool import terminal_tool

        mock_session = _make_mock_proc_session(
            exited=True, exit_code=0, output_buffer="/home\n",
        )

        mock_registry = MagicMock()
        mock_registry.spawn_local.return_value = mock_session
        mock_registry._completion_consumed = set()
        mock_registry._lock = threading.Lock()
        mock_registry._running = {}
        mock_registry._finished = {}

        mock_env = MagicMock()
        mock_env.env = {}

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""), \
             patch("tools.terminal_tool.is_interrupted", return_value=False), \
             patch("tools.interrupt.is_interrupted", return_value=False):
            result = json.loads(terminal_tool(command="pwd", workdir="/home"))

        call_kwargs = mock_registry.spawn_local.call_args[1]
        assert call_kwargs["cwd"] == "/home"

    def test_pty_disabled_for_pipe_stdin_commands(self):
        """PTY is auto-disabled for commands requiring piped stdin."""
        from tools.terminal_tool import terminal_tool

        mock_session = _make_mock_proc_session(
            session_id="proc_pty_test",
            pid=111,
            exited=False,
            exit_code=None,
            output_buffer="",
        )

        mock_registry = MagicMock()
        mock_registry.spawn_local.return_value = mock_session
        mock_registry._completion_consumed = set()
        mock_registry.pending_watchers = []

        mock_env = MagicMock()
        mock_env.env = {}

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""), \
             patch("tools.terminal_tool.is_interrupted", return_value=False), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("tools.terminal_tool.AUTO_BACKGROUND_TIMEOUT", 0), \
             patch("time.sleep"):
            result = json.loads(terminal_tool(
                command="gh auth login --with-token",
                pty=True
            ))

        assert result["status"] == "auto_backgrounded"
        assert "pty_note" in result
        call_kwargs = mock_registry.spawn_local.call_args[1]
        assert call_kwargs["use_pty"] is False

    def test_interrupted_command_returns_interrupted_status(self):
        """Interrupted commands return interrupted status."""
        from tools.terminal_tool import terminal_tool

        mock_session = _make_mock_proc_session(
            session_id="proc_interrupt",
            pid=333,
            exited=False,
            exit_code=None,
        )

        mock_registry = MagicMock()
        mock_registry.spawn_local.return_value = mock_session
        mock_registry.kill_process.return_value = {"status": "killed"}

        mock_env = MagicMock()
        mock_env.env = {}

        interrupt_count = [0]
        def mock_interrupted():
            interrupt_count[0] += 1
            return interrupt_count[0] > 1

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""), \
             patch("tools.terminal_tool.is_interrupted", side_effect=mock_interrupted), \
             patch("tools.interrupt.is_interrupted", side_effect=mock_interrupted), \
             patch("time.sleep"):
            result = json.loads(terminal_tool(command="long_task"))

        assert result["status"] == "interrupted"
        assert result["exit_code"] == -1

    def test_remote_env_uses_spawn_via_env(self):
        """Non-local environments use spawn_via_env instead of spawn_local."""
        from tools.terminal_tool import terminal_tool

        mock_session = _make_mock_proc_session(
            session_id="proc_remote",
            pid=444,
            exited=False,
            exit_code=None,
        )

        mock_registry = MagicMock()
        mock_registry.spawn_via_env.return_value = mock_session
        mock_registry._completion_consumed = set()
        mock_registry.pending_watchers = []

        mock_env = MagicMock()
        mock_env.env = {}

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config(env_type="docker")), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""), \
             patch("tools.terminal_tool.is_interrupted", return_value=False), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("tools.terminal_tool.AUTO_BACKGROUND_TIMEOUT", 0), \
             patch("time.sleep"):
            result = json.loads(terminal_tool(command="docker_command"))

        mock_registry.spawn_via_env.assert_called_once()
        mock_registry.spawn_local.assert_not_called()
        assert result["status"] == "auto_backgrounded"

    def test_spawn_failure_returns_error(self):
        """If spawn fails, return error immediately."""
        from tools.terminal_tool import terminal_tool

        mock_registry = MagicMock()
        mock_registry.spawn_local.side_effect = OSError("Cannot spawn")

        mock_env = MagicMock()
        mock_env.env = {}

        with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
             patch("tools.terminal_tool._last_activity", {"default": 0}), \
             patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
             patch("tools.process_registry.process_registry", mock_registry), \
             patch("tools.approval.get_current_session_key", return_value=""), \
             patch("gateway.session_context.get_session_env", return_value=""):
            result = json.loads(terminal_tool(command="broken_command"))

        assert result["exit_code"] == -1
        assert "Cannot spawn" in result["error"]


class TestSchema:
    """Verify the tool schema."""

    def test_background_parameter_removed(self):
        """background parameter should not exist in schema."""
        from tools.terminal_tool import TERMINAL_SCHEMA
        properties = TERMINAL_SCHEMA["parameters"]["properties"]
        assert "background" not in properties

    def test_notify_on_complete_parameter_removed(self):
        """notify_on_complete parameter should not exist in schema."""
        from tools.terminal_tool import TERMINAL_SCHEMA
        properties = TERMINAL_SCHEMA["parameters"]["properties"]
        assert "notify_on_complete" not in properties

    def test_remaining_parameters(self):
        """Only expected parameters should remain."""
        from tools.terminal_tool import TERMINAL_SCHEMA
        properties = TERMINAL_SCHEMA["parameters"]["properties"]
        expected = {"command", "timeout", "workdir", "pty", "watch_patterns"}
        assert set(properties.keys()) == expected