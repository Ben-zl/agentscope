# -*- coding: utf-8 -*-
# pylint: disable=protected-access,unused-argument
"""Unit tests for :class:`WindowsSSHBackend` and :class:`WindowsWorkspace`.

These tests mock ``asyncssh`` so they run on any platform (no real
Windows host required).
"""

import base64
import ctypes
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest.async_case import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

# Skip if asyncssh is not installed.
try:
    import asyncssh  # noqa: F401
except ImportError:
    asyncssh = None

_SKIP = "requires 'asyncssh' (pip install agentscope[workspace-ssh])"


@unittest.skipUnless(asyncssh is not None, _SKIP)
class TestWindowsSSHBackend(IsolatedAsyncioTestCase):
    """WindowsSSHBackend primitives (mocked SSH)."""

    def _make_backend(self):
        from agentscope.workspace._windows._windows_ssh_backend import (
            WindowsSSHBackend,
        )

        conn = MagicMock()
        conn.run = AsyncMock()
        return (
            WindowsSSHBackend(
                conn=conn,
                workdir=r"C:\workspace\test",
                runner_path=r"C:\runner\win_runner.ps1",
            ),
            conn,
        )

    @staticmethod
    def _runner_out(exit_code: int, stdout: str, stderr: str = ""):
        r = MagicMock()
        r.exit_status = 0
        env = {
            "exit_code": exit_code,
            "stdout": base64.b64encode(stdout.encode()).decode(),
            "stderr": base64.b64encode(stderr.encode()).decode(),
        }
        r.stdout = json.dumps(env)
        return r

    async def test_exec_shell_parses_output(self):
        backend, conn = self._make_backend()
        conn.run.return_value = self._runner_out(0, "hello\n")
        result = await backend.exec_shell(["python", "-c", "print('hello')"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"hello\n")
        self.assertTrue(
            conn.run.await_args.args[0].startswith("powershell.exe "),
        )

    async def test_exec_shell_nonzero(self):
        backend, conn = self._make_backend()
        conn.run.return_value = self._runner_out(42, "", "err")
        result = await backend.exec_shell(["false"])
        self.assertEqual(result.exit_code, 42)
        self.assertIn(b"err", result.stderr)

    async def test_exec_shell_timeout(self):
        import asyncio

        backend, conn = self._make_backend()
        conn.run.side_effect = asyncio.TimeoutError
        result = await backend.exec_shell(["sleep", "99"], timeout=0.01)
        self.assertEqual(result.exit_code, -1)
        self.assertEqual(result.stderr, b"timed out")

    async def test_list_dir_non_recursive_basenames(self):
        backend, conn = self._make_backend()
        conn.run.return_value = self._runner_out(0, "a.txt\nb.py")
        entries = await backend.list_dir(r"C:\test")
        self.assertEqual(entries, ["a.txt", "b.py"])

    async def test_list_dir_recursive_full_paths(self):
        backend, conn = self._make_backend()
        conn.run.return_value = self._runner_out(
            0,
            r"C:\test\a.txt" + "\n" + r"C:\test\sub\b.py",
        )
        entries = await backend.list_dir(r"C:\test", recursive=True)
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e.startswith("C:\\") for e in entries))

    async def test_stat_mtime_unix_epoch(self):
        backend, conn = self._make_backend()
        conn.run.return_value = self._runner_out(0, "1691075205\n")
        self.assertAlmostEqual(
            await backend.stat_mtime(r"C:\f"),
            1691075205.0,
        )

    async def test_stat_mtime_missing(self):
        backend, conn = self._make_backend()
        conn.run.return_value = self._runner_out(1, "", "not found")
        self.assertIsNone(await backend.stat_mtime(r"C:\missing"))

    async def test_file_exists(self):
        backend, conn = self._make_backend()
        conn.run.return_value = self._runner_out(0, "")
        self.assertTrue(await backend.file_exists(r"C:\ok"))
        conn.run.return_value = self._runner_out(1, "")
        self.assertFalse(await backend.file_exists(r"C:\nope"))

    def test_path_module_ntpath(self):
        import ntpath

        backend, _ = self._make_backend()
        self.assertIs(backend._path_module, ntpath)

    def test_join_path_backslash(self):
        backend, _ = self._make_backend()
        self.assertEqual(
            backend.join_path(r"C:\foo", "bar"),
            r"C:\foo\bar",
        )


@unittest.skipUnless(asyncssh is not None, _SKIP)
class TestWindowsWorkspace(IsolatedAsyncioTestCase):
    """Verify WindowsWorkspace lifecycle and platform adaptations."""

    def test_import_and_mro(self):
        from agentscope.workspace import WindowsWorkspace
        from agentscope.workspace._sandboxed_base import SandboxedWorkspaceBase

        self.assertTrue(
            issubclass(WindowsWorkspace, SandboxedWorkspaceBase),
        )

    def test_workdir_and_constants(self):
        from agentscope.workspace import WindowsWorkspace

        ws = WindowsWorkspace(
            workspace_id="t1",
            host="h",
            username="u",
        )
        self.assertEqual(
            ws.workdir,
            r"C:\ProgramData\AgentScope\ws\t1",
        )
        self.assertIn("Windows (remote SSH)", ws.instructions)

    def test_ssh_host_key_verification_is_enabled_by_default(self):
        from agentscope.workspace import WindowsWorkspace

        ws = WindowsWorkspace(
            workspace_id="host-key",
            host="h",
            username="u",
        )
        self.assertNotIn("known_hosts", ws._ssh_cfg)

    def test_lease_id_unique(self):
        from agentscope.workspace import WindowsWorkspace

        a = WindowsWorkspace(workspace_id="a", host="h", username="u")
        b = WindowsWorkspace(workspace_id="b", host="h", username="u")
        self.assertNotEqual(a._lease_id, b._lease_id)
        self.assertTrue(a._lease_id.startswith("lease-"))

    def test_mcp_management_uses_shared_agent_session_contract(self):
        from agentscope.workspace import WindowsWorkspace
        from agentscope.workspace._sandboxed_base import SandboxedWorkspaceBase

        self.assertIs(
            WindowsWorkspace.add_mcp,
            SandboxedWorkspaceBase.add_mcp,
        )
        self.assertIs(
            WindowsWorkspace.remove_mcp,
            SandboxedWorkspaceBase.remove_mcp,
        )
        self.assertIs(
            WindowsWorkspace.reset,
            SandboxedWorkspaceBase.reset,
        )

    async def test_gateway_kill_hook_noop(self):
        """WindowsWorkspace does not use the parent's pkill/nohup path."""
        from agentscope.workspace import WindowsWorkspace

        ws = WindowsWorkspace(
            workspace_id="noop-test",
            host="h",
            username="u",
        )
        # _setup_mcp_gateway is overridden — verify it exists and is
        # not the parent's implementation.
        import inspect

        src = inspect.getsource(type(ws)._setup_mcp_gateway)
        # Must NOT contain pkill or nohup (parent's POSIX commands).
        self.assertNotIn("pkill", src)
        self.assertNotIn("nohup", src)

    def test_initialize_has_rollback(self):
        """initialize must have try/except for failure rollback."""
        import inspect
        from agentscope.workspace import WindowsWorkspace

        src = inspect.getsource(WindowsWorkspace.initialize)
        self.assertIn("try:", src)
        self.assertIn("_teardown_backend", src)

    async def test_initial_layout_uses_existing_root_as_cwd(self):
        from agentscope.workspace import WindowsWorkspace

        ws = WindowsWorkspace(
            workspace_id="new-workspace",
            host="h",
            username="u",
        )
        backend = MagicMock()
        result = MagicMock()
        result.ok.return_value = True
        backend.exec_shell = AsyncMock(return_value=result)
        backend.file_exists = AsyncMock(return_value=False)
        backend.write_file = AsyncMock()
        ws._backend = backend

        await ws._ensure_workspace_layout()

        self.assertEqual(
            backend.exec_shell.await_args.kwargs["cwd"],
            r"C:\ProgramData\AgentScope",
        )

    def test_runner_suppresses_process_start_result(self):
        runner = pathlib.Path(
            "deployments/windows/runner/win_runner.ps1",
        ).read_text(encoding="utf-8")
        self.assertIn("$null = $proc.Start()", runner)

    def test_runner_supports_windows_powershell_51(self):
        runner = pathlib.Path(
            "deployments/windows/runner/win_runner.ps1",
        ).read_text(encoding="utf-8")
        self.assertIn("ConvertTo-WindowsCommandLineArg", runner)
        self.assertIn("$psi.Arguments = $encodedArgs -join ' '", runner)
        self.assertNotIn("$psi.ArgumentList", runner)
        self.assertIn("taskkill.exe", runner)
        self.assertNotIn("$proc.Kill($true)", runner)

    def test_rejects_unsafe_workspace_ids(self):
        from agentscope.workspace import WindowsWorkspace

        for workspace_id in (
            "../escape",
            r"a\\b",
            " ",
            ".hidden",
            "trailing.",
            "CON",
            "com1.txt",
        ):
            with self.subTest(workspace_id=workspace_id):
                with self.assertRaises(ValueError):
                    WindowsWorkspace(
                        workspace_id=workspace_id,
                        host="h",
                        username="u",
                    )

    def test_workspace_id_is_canonicalized_for_windows(self):
        from agentscope.workspace import WindowsWorkspace

        workspace = WindowsWorkspace(
            workspace_id="Team-A",
            host="h",
            username="u",
        )

        self.assertEqual(workspace.workspace_id, "team-a")
        self.assertTrue(workspace.workdir.endswith(r"\team-a"))

    async def test_reconnect_rebuilds_tunnel_when_remote_port_changes(self):
        from agentscope.workspace import WindowsWorkspace

        ws = WindowsWorkspace(
            workspace_id="reconnect",
            host="h",
            username="u",
        )
        ws.gateway_port = 5601
        ws._supervisor_info = {
            "gateway_port": 5601,
            "auth_token": "old",
            "instance_nonce": "old-nonce",
        }

        async def _start_with_new_port() -> None:
            ws._supervisor_info = {
                "gateway_port": 5602,
                "auth_token": "new",
                "instance_nonce": "new-nonce",
            }
            ws.gateway_port = 5602

        ws._supervisor_start = AsyncMock(side_effect=_start_with_new_port)
        old_listener = MagicMock()
        old_listener.wait_closed = AsyncMock()
        new_listener = MagicMock()
        new_listener.get_port.return_value = 16002
        conn = MagicMock()
        conn.forward_local_port = AsyncMock(return_value=new_listener)
        gateway = MagicMock()
        gateway.reconnect = AsyncMock()
        gateway.list_mcps = AsyncMock(return_value=[])
        ws._gw_listener = old_listener
        ws._conn = conn
        ws._gateway = gateway

        await ws._reconnect()

        conn.forward_local_port.assert_awaited_once()
        gateway.reconnect.assert_awaited_once_with(
            "http://127.0.0.1:16002",
            "new",
            "new-nonce",
        )

    async def test_reset_clears_shared_partitioned_state(self):
        from agentscope.workspace import WindowsWorkspace

        ws = WindowsWorkspace(
            workspace_id="reset-failure",
            host="h",
            username="u",
        )
        ws._mcp_specs[("agent", "session")] = []
        ws._equipped_partitions.add("agent")
        ws._close_all_mcp_instances = AsyncMock()
        backend = MagicMock()
        backend.delete_path = AsyncMock()
        ws._backend = backend

        await ws.reset()

        ws._close_all_mcp_instances.assert_awaited_once()
        self.assertEqual(ws._mcp_specs, {})
        self.assertEqual(ws._equipped_partitions, set())
        self.assertEqual(backend.delete_path.await_count, 4)


@unittest.skipUnless(asyncssh is not None, _SKIP)
class TestWinGatewayClient(IsolatedAsyncioTestCase):
    """WinGatewayClient overrides exec_request for direct HTTP."""

    def test_inherits_gateway_client(self):
        from agentscope.workspace._gateway_client import GatewayClient
        from agentscope.workspace._windows._win_gateway_client import (
            WinGatewayClient,
        )

        self.assertTrue(issubclass(WinGatewayClient, GatewayClient))

    async def test_exec_request_uses_httpx(self):
        """exec_request must dispatch via httpx, not backend.exec_shell."""
        from agentscope.workspace._windows._win_gateway_client import (
            WinGatewayClient,
        )

        client = WinGatewayClient(
            backend=MagicMock(),
            base_url="http://127.0.0.1:19999",
            auth_token="tok_test",
        )
        # Mock the internal httpx client to avoid real network.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"status":"ok"}'
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=mock_resp)
        client._http_client = mock_http

        status, body = await client.exec_request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"status":"ok"}')
        # Verify httpx was called, not backend.exec_shell.
        mock_http.request.assert_called_once()

    async def test_exec_request_encodes_agent_and_session_params(self):
        from agentscope.workspace._windows._win_gateway_client import (
            WinGatewayClient,
        )

        client = WinGatewayClient(
            backend=MagicMock(),
            base_url="http://127.0.0.1:19999",
        )
        response = MagicMock(status_code=200, content=b"[]")
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=response)
        client._http_client = mock_http

        await client.exec_request(
            "GET",
            "/mcps",
            params={"agent_id": "agent/a", "session_id": "session b"},
        )

        self.assertEqual(
            mock_http.request.await_args.args[:2],
            ("GET", "/mcps?agent_id=agent%2Fa&session_id=session+b"),
        )

    async def test_aclose_closes_http(self):
        from agentscope.workspace._windows._win_gateway_client import (
            WinGatewayClient,
        )

        backend = MagicMock()
        client = WinGatewayClient(
            backend=backend,
            base_url="http://127.0.0.1:1",
        )
        self.assertIs(client.backend, backend)
        mock_http = AsyncMock()
        mock_http.aclose = AsyncMock()
        client._http_client = mock_http
        await client.aclose()
        mock_http.aclose.assert_called_once()

    async def test_transport_error_recovers_and_retries_once(self):
        import httpx
        from agentscope.workspace._windows._win_gateway_client import (
            WinGatewayClient,
        )

        client = WinGatewayClient(
            backend=MagicMock(),
            base_url="http://127.0.0.1:1",
        )
        first_http = AsyncMock()
        first_http.request = AsyncMock(
            side_effect=httpx.ConnectError("tunnel closed"),
        )
        response = MagicMock(status_code=200, content=b"ok")
        second_http = AsyncMock()
        second_http.request = AsyncMock(return_value=response)

        async def recover() -> None:
            client._http_client = second_http

        client._http_client = first_http
        client.set_recovery_callback(recover)

        status, body = await client.exec_request("GET", "/mcps")

        self.assertEqual((status, body), (200, b"ok"))
        first_http.request.assert_awaited_once()
        second_http.request.assert_awaited_once()

    async def test_transport_error_does_not_replay_mutation(self):
        import httpx
        from agentscope.workspace._windows._win_gateway_client import (
            WinGatewayClient,
        )

        client = WinGatewayClient(
            backend=MagicMock(),
            base_url="http://127.0.0.1:1",
        )
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(
            side_effect=httpx.ConnectError("response lost"),
        )
        recover = AsyncMock()
        client._http_client = mock_http
        client.set_recovery_callback(recover)

        with self.assertRaises(httpx.ConnectError):
            await client.exec_request("POST", "/mcps", body={"name": "x"})

        recover.assert_awaited_once()
        self.assertEqual(
            [call.args[:2] for call in mock_http.request.await_args_list],
            [("POST", "/mcps"), ("GET", "/health")],
        )

    async def test_final_transport_failure_uses_remote_log_diagnostic(self):
        import httpx
        from agentscope.workspace._windows._win_gateway_client import (
            WinGatewayClient,
        )

        backend = MagicMock()
        backend.read_file = AsyncMock(return_value=b"gateway crashed")
        client = WinGatewayClient(
            backend=backend,
            base_url="http://127.0.0.1:1",
            gateway_log_path=r"C:\workspace\.gateway\gateway.log",
        )
        mock_http = AsyncMock()
        error = httpx.ConnectError("tunnel closed")
        mock_http.request = AsyncMock(side_effect=error)
        client._http_client = mock_http

        with self.assertRaises(httpx.ConnectError) as raised:
            await client.exec_request("GET", "/mcps")

        self.assertIs(raised.exception, error)
        backend.read_file.assert_awaited_once_with(
            r"C:\workspace\.gateway\gateway.log",
        )


def _load_supervisor_module():
    """Load the Windows-only supervisor with mocked platform bindings."""
    module_name = "_agentscope_windows_supervisor_test"
    path = pathlib.Path(
        "deployments/windows/ws_supervisor/ws_supervisor.py",
    ).resolve()
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    kernel32 = MagicMock()
    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.get_osfhandle = MagicMock(return_value=1)
    sys.modules[module_name] = module
    try:
        with (
            patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
            patch.object(
                ctypes,
                "WinDLL",
                return_value=kernel32,
                create=True,
            ),
        ):
            spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


class TestWindowsSupervisor(IsolatedAsyncioTestCase):
    """Exercise the supervisor state machine without a Windows host."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_supervisor_module()

    def _make_supervisor(self):
        supervisor = self.module.Supervisor()
        supervisor._save_state = MagicMock()
        return supervisor

    async def test_expired_lease_cannot_be_renewed(self):
        supervisor = self._make_supervisor()
        entry = self.module.WorkspaceEntry(
            workspace_id="demo",
            lease_id="lease-1",
            expires_at=supervisor._now() - 1,
            status="running",
        )
        supervisor._workspaces["demo"] = entry

        with self.assertRaises(self.module.HTTPException) as error:
            await supervisor.renew("DEMO", "lease-1")

        self.assertEqual(error.exception.status_code, 404)

    async def test_crash_restart_backs_off_and_waits_until_ready(self):
        supervisor = self._make_supervisor()
        entry = self.module.WorkspaceEntry(
            workspace_id="demo",
            gateway_port=5601,
            h_job=10,
            auth_token="token",
            instance_nonce="nonce",
            lease_id="lease-1",
            expires_at=supervisor._now() + 60,
            status="restarting",
        )
        supervisor._port_pool.add(5601)

        async def launch(current, *, preserve_identity=False):
            self.assertTrue(preserve_identity)
            current.h_job = 11
            current.h_process = 12
            current.pid = 13

        supervisor._launch_gateway = AsyncMock(side_effect=launch)
        supervisor._wait_gateway_ready = AsyncMock(return_value=True)

        with patch.object(
            self.module.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            await supervisor._on_gateway_exit(entry)

        sleep.assert_awaited_once_with(2)
        supervisor._wait_gateway_ready.assert_awaited_once_with(entry)
        self.assertEqual(entry.status, "running")

    async def test_release_during_restart_backoff_prevents_relaunch(self):
        supervisor = self._make_supervisor()
        entry = self.module.WorkspaceEntry(
            workspace_id="demo",
            gateway_port=5601,
            h_job=10,
            auth_token="token",
            instance_nonce="nonce",
            lease_id="lease-1",
            expires_at=supervisor._now() + 60,
            status="restarting",
        )
        supervisor._port_pool.add(5601)
        supervisor._launch_gateway = AsyncMock()

        async def release_during_sleep(_delay):
            entry.lease_id = ""

        with patch.object(
            self.module.asyncio,
            "sleep",
            new=AsyncMock(side_effect=release_during_sleep),
        ):
            await supervisor._on_gateway_exit(entry)

        supervisor._launch_gateway.assert_not_awaited()
        self.assertEqual(entry.status, "stopped")
        self.assertNotIn(5601, supervisor._port_pool)

    async def test_readiness_exception_marks_restarted_gateway_dead(self):
        supervisor = self._make_supervisor()
        entry = self.module.WorkspaceEntry(
            workspace_id="demo",
            gateway_port=5601,
            h_job=10,
            auth_token="token",
            instance_nonce="nonce",
            lease_id="lease-1",
            expires_at=supervisor._now() + 60,
            status="restarting",
        )
        supervisor._port_pool.add(5601)

        async def launch(current, *, preserve_identity=False):
            current.h_job = 11
            current.h_process = 12
            current.pid = 13

        supervisor._launch_gateway = AsyncMock(side_effect=launch)
        supervisor._wait_gateway_ready = AsyncMock(
            side_effect=ValueError("unexpected health payload"),
        )

        with patch.object(
            self.module.asyncio,
            "sleep",
            new=AsyncMock(),
        ):
            await supervisor._on_gateway_exit(entry)

        self.assertEqual(entry.status, "dead")
        self.assertNotIn(5601, supervisor._port_pool)


class TestWindowsToolAdaptations(IsolatedAsyncioTestCase):
    """Windows-specific executable-path adaptations."""

    def test_glob_and_grep_accept_explicit_executables(self):
        import inspect
        from agentscope.tool import Glob, Grep

        self.assertIn(
            "python_bin",
            inspect.signature(Glob.__init__).parameters,
        )
        self.assertIn(
            "rg_path",
            inspect.signature(Grep.__init__).parameters,
        )

    async def test_glob_uses_explicit_windows_python(self):
        from agentscope.tool import Glob

        backend = MagicMock()
        backend.isabs.return_value = True
        backend.is_dir = AsyncMock(return_value=True)
        result = MagicMock(exit_code=0, stdout=b"[]", stderr=b"")
        backend.exec_shell = AsyncMock(return_value=result)
        tool = Glob(
            backend=backend,
            glob_helper_path=r"C:\helper.py",
            python_bin=r"C:\python.exe",
        )

        await tool(pattern="*.py", path=r"C:\workspace")

        command = backend.exec_shell.await_args.args[0]
        self.assertEqual(command[0], r"C:\python.exe")

    async def test_grep_uses_explicit_windows_ripgrep(self):
        from agentscope.tool import Grep

        backend = MagicMock()
        backend.isabs.return_value = True
        result = MagicMock(exit_code=1, stdout=b"", stderr=b"")
        backend.exec_shell = AsyncMock(return_value=result)
        tool = Grep(backend=backend, rg_path=r"C:\rg.exe")

        await tool(pattern="needle", path=r"C:\workspace")

        command = backend.exec_shell.await_args.args[0]
        self.assertEqual(command[0], r"C:\rg.exe")


class TestGatewayIsolation(unittest.TestCase):
    """Gateway keeps runtime MCPs isolated by agent and session."""

    def test_add_list_and_remove_are_partitioned(self):
        from fastapi.testclient import TestClient
        from agentscope.workspace._mcp_gateway import _mcp_gateway_app

        fake_client = MagicMock()
        fake_client.name = "demo"
        fake_client.is_stateful = False
        fake_client.is_connected = False
        fake_client.model_dump.return_value = {
            "name": "demo",
            "transport": "stdio",
            "command": "demo.exe",
        }

        state = _mcp_gateway_app._State()
        app = _mcp_gateway_app._build_app(state)
        client = TestClient(app)

        with patch.object(
            _mcp_gateway_app,
            "_build_client",
            new=AsyncMock(return_value=fake_client),
        ):
            response = client.post(
                "/mcps?agent_id=agent-a&session_id=session-1",
                json=fake_client.model_dump.return_value,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            client.get(
                "/mcps?agent_id=agent-a&session_id=session-1",
            ).json(),
            [fake_client.model_dump.return_value],
        )
        self.assertEqual(
            client.get(
                "/mcps?agent_id=agent-a&session_id=session-2",
            ).json(),
            [],
        )

        response = client.delete(
            "/mcps/demo?agent_id=agent-a&session_id=session-1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(("agent-a", "session-1"), state.clients)


if __name__ == "__main__":
    unittest.main()
