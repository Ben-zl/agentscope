# -*- coding: utf-8 -*-
"""Workspace backed by a remote Windows host accessed via SSH.

Windows-specific lifecycle and transport behaviour lives on this class and
on :class:`WindowsSSHBackend`; small shared extension points are provided by
the base workspace and gateway client.

Architecture (see ``docs/research/windows-workspace-form2-design-v4.md``):

* A **supervisor** service (installed on the Windows host) manages
  per-workspace gateway processes using Windows Job Objects.
* :class:`WindowsWorkspace` connects via SSH, sets up local port
  forwards to the supervisor and gateway (both loopback-only on the
  remote side), and talks to the gateway through a
  :class:`WinGatewayClient` (direct HTTP, no exec-shell shim).

Overrides summary:

* ``_setup_mcp_gateway`` — replaces the POSIX pkill/nohup launch with
  supervisor-managed gateway start.
* ``_ensure_workspace_layout`` — replaces ``mkdir -p`` with PowerShell.
* Shared skill hooks select Windows paths and PowerShell operations.
* MCP declaration and skill partition semantics are inherited unchanged.
* ``list_tools`` — PowerShell shell + Windows-adapted Glob/Grep.
* ``initialize`` — try/except rollback.
"""

from __future__ import annotations

import asyncio
import ntpath
import uuid
from typing import TYPE_CHECKING, Any

from ..._logging import logger
from ...mcp import MCPClient
from .._sandboxed_base import SandboxedWorkspaceBase
from .._utils import (
    DEFAULT_WORKSPACE_INSTRUCTIONS,
    _GATEWAY_BASE_REQUIREMENTS,
    _read_gateway_script_bytes,
    _read_glob_helper_bytes,
)
from ._constants import (
    AS_ROOT,
    RUNNER_PATH,
    SUPERVISOR_PORT,
    UV_BIN,
    ws_gateway_home,
    ws_gateway_log,
    ws_gateway_python,
    ws_gateway_script,
    ws_glob_helper,
    ws_ripgrep,
    ws_workdir,
    validate_workspace_id,
)
from ._win_gateway_client import WinGatewayClient
from ._windows_ssh_backend import WindowsSSHBackend, ps_quote

if TYPE_CHECKING:
    import asyncssh


class WindowsWorkspace(SandboxedWorkspaceBase):
    """Workspace on a remote Windows host via SSH + supervisor.

    Single-owner model: each ``workspace_id`` has at most one active
    lease at a time.
    """

    # ── Bootstrap timeout (K8s default is 1800s; Windows apt-less
    # install via uv is faster so we use 600s).
    _bootstrap_cmd_timeout: float = 600.0

    def __init__(
        self,
        *,
        workspace_id: str | None = None,
        host: str,
        port: int = 22,
        username: str = "",
        password: str | None = None,
        client_keys: list[str] | None = None,
        known_hosts: Any = (),
        supervisor_port: int = SUPERVISOR_PORT,
        lease_ttl: float = 300.0,
        default_mcps: list[MCPClient] | None = None,
        skill_paths: list[str] | None = None,
        extra_pip: list[str] | None = None,
        instructions: str = DEFAULT_WORKSPACE_INSTRUCTIONS,
    ) -> None:
        super().__init__(
            workspace_id=workspace_id,
            default_mcps=default_mcps,
            skill_paths=skill_paths,
        )
        self.workspace_id = validate_workspace_id(self.workspace_id)

        self.workdir = ws_workdir(self.workspace_id)
        self._gateway_home = ws_gateway_home(self.workspace_id)
        self.gateway_port = 0
        self.extra_pip = list(extra_pip or [])
        self.instructions = instructions.format(
            backend="Windows (remote SSH)",
            workdir=self.workdir,
        )

        self._ssh_cfg: dict[str, Any] = dict(
            host=host,
            port=port,
            username=username,
        )
        if password is not None:
            self._ssh_cfg["password"] = password
        if client_keys:
            self._ssh_cfg["client_keys"] = client_keys
        if known_hosts != ():
            self._ssh_cfg["known_hosts"] = known_hosts

        self._supervisor_port = supervisor_port
        self._lease_ttl = lease_ttl
        self._lease_id = f"lease-{uuid.uuid4().hex[:12]}"
        self._renew_task: asyncio.Task[None] | None = None
        self._reconnect_lock = asyncio.Lock()

        self._conn: "asyncssh.SSHClientConnection | None" = None
        self._sup_listener = None
        self._gw_listener = None
        self._supervisor_info: dict[str, Any] | None = None

    # ── lifecycle hooks ─────────────────────────────────────────────

    @property
    def _python_command(self) -> str:
        return ws_gateway_python(self.workspace_id)

    @property
    def _tmp_dir(self) -> str:
        return ntpath.join(AS_ROOT, "tmp")

    async def _shell_makedirs(self, *dirs: str) -> None:
        if not dirs:
            return
        dir_list = ", ".join(ps_quote(path) for path in dirs)
        result = await self.get_backend().exec_shell(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"@( {dir_list} ) | ForEach-Object "
                "{ New-Item -ItemType Directory -Force -Path $_ }",
            ],
            cwd=AS_ROOT,
        )
        if not result.ok():
            raise RuntimeError(
                "Failed to create Windows workspace directories: "
                f"{result.stderr.decode('utf-8', 'replace')}",
            )

    async def _shell_move(self, src: str, dst: str) -> None:
        result = await self.get_backend().exec_shell(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Move-Item -LiteralPath {ps_quote(src)} "
                f"-Destination {ps_quote(dst)} -Force",
            ],
            cwd=AS_ROOT,
        )
        if not result.ok():
            raise RuntimeError(
                f"Failed to move {src!r} to {dst!r}: "
                f"{result.stderr.decode('utf-8', 'replace')}",
            )

    async def _provision_backend(self) -> None:
        import asyncssh

        self._conn = await asyncssh.connect(**self._ssh_cfg)
        self._backend = WindowsSSHBackend(
            self._conn,
            self.workdir,
            RUNNER_PATH,
        )
        self._sup_listener = await self._conn.forward_local_port(
            "127.0.0.1",
            0,
            "127.0.0.1",
            self._supervisor_port,
        )

    async def _teardown_backend(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except (asyncio.CancelledError, Exception):
                pass
            self._renew_task = None

        if self._gateway is not None:
            try:
                await self._gateway.aclose()
            except Exception:
                pass

        if self._supervisor_info is not None:
            try:
                await self._supervisor_release()
            except Exception:
                pass

        for listener in (self._gw_listener, self._sup_listener):
            if listener is not None:
                try:
                    listener.close()
                    await listener.wait_closed()
                except Exception:
                    pass
        self._gw_listener = None
        self._sup_listener = None

        if self._conn is not None:
            self._conn.close()
            try:
                await self._conn.wait_closed()
            except Exception:
                pass
            self._conn = None

    # ── override: workspace layout (PowerShell mkdir) ───────────────

    async def _ensure_workspace_layout(self) -> None:
        """Create workspace dirs using PowerShell (parent uses mkdir -p)."""
        dirs = [
            self.workdir,
            ntpath.join(self.workdir, "data"),
            ntpath.join(self.workdir, "skills"),
            ntpath.join(self.workdir, "sessions"),
            self._gateway_home,
        ]
        await self._shell_makedirs(*dirs)

    # ── override: gateway setup (supervisor-managed) ────────────────

    async def _setup_mcp_gateway(self) -> None:
        backend = self.get_backend()

        # 1. Bootstrap if needed.
        if not await backend.file_exists(
            ws_gateway_script(self.workspace_id),
        ):
            await self._bootstrap_gateway(backend)

        # 2. Start gateway via supervisor.
        await self._supervisor_start()

        # 3. Forward gateway port.
        self._gw_listener = await self._conn.forward_local_port(
            "127.0.0.1",
            0,
            "127.0.0.1",
            self._supervisor_info["gateway_port"],
        )
        gw_port = self._gw_listener.get_port()

        # 4. Build the gateway client (direct HTTP).
        self._gateway = WinGatewayClient(
            backend=backend,
            base_url=f"http://127.0.0.1:{gw_port}",
            auth_token=self._supervisor_info["auth_token"],
            instance_nonce=self._supervisor_info["instance_nonce"],
            gateway_log_path=ws_gateway_log(self.workspace_id),
        )

        # 5. Poll /health.
        deadline = asyncio.get_event_loop().time() + 30.0
        delay = 0.5
        while asyncio.get_event_loop().time() < deadline:
            if await self._gateway.health():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        else:
            try:
                log_bytes = await backend.read_file(
                    ws_gateway_log(self.workspace_id),
                )
                tail = log_bytes[-2000:].decode("utf-8", "replace")
            except Exception:
                tail = "<no gateway log available>"
            raise RuntimeError(
                f"gateway did not become healthy within 30 s.\n"
                f"Tail of gateway log:\n{tail}",
            )

        # 6. Install recovery callback. The gateway starts empty; MCPs are
        # registered lazily per agent/session by the base implementation.
        self._gateway.set_recovery_callback(self._reconnect)

        # 7. Start lease renew.
        self._renew_task = asyncio.create_task(self._renew_loop())

    # ── override: initialize with rollback ──────────────────────────

    async def initialize(self) -> None:
        if self.is_alive:
            return
        try:
            await self._provision_backend()
            assert self._backend is not None
            self._mcp_specs = await self._restore_mcp_specs()
            await self._ensure_workspace_layout()
            await self._setup_mcp_gateway()
            await self._migrate_skill_layout()
            await self._setup_skills()
        except Exception:
            if self._gateway is not None:
                try:
                    await self._gateway.aclose()
                except Exception:
                    pass
                self._gateway = None
            try:
                await self._teardown_backend()
            except Exception:
                pass
            self._backend = None
            raise
        self.is_alive = True

    # ── override: skills (Windows-compatible tar extraction) ─────────

    async def _setup_skills(self) -> None:
        """Seed skills using Windows-compatible extraction."""
        if not self.skill_paths:
            return
        backend = self._backend
        if backend is None:
            return
        skills_dir = ntpath.join(self.workdir, "skills")
        entries = await backend.list_dir(skills_dir)
        if entries:
            return
        for path in self.skill_paths:
            try:
                await self.add_skill(path)
            except Exception as e:
                logger.warning("Skip skill %r: %s", path, e)

    # ── bootstrap ───────────────────────────────────────────────────

    async def _bootstrap_gateway(self, backend: Any) -> None:
        await backend.write_file(
            ws_glob_helper(self.workspace_id),
            _read_glob_helper_bytes(),
        )

        deps = list(_GATEWAY_BASE_REQUIREMENTS) + list(self.extra_pip)
        venv = ntpath.join(self._gateway_home, ".venv")
        py = ws_gateway_python(self.workspace_id)

        commands: list[list[str]] = [
            [UV_BIN, "venv", venv],
            [UV_BIN, "pip", "install", "--python", py, *deps],
            [
                UV_BIN,
                "pip",
                "install",
                "--python",
                py,
                "--no-deps",
                "agentscope",
            ],
            [UV_BIN, "pip", "install", "--python", py, "ripgrep"],
        ]
        for cmd_argv in commands:
            r = await backend.exec_shell(
                cmd_argv,
                timeout=self._bootstrap_cmd_timeout,
            )
            if not r.ok():
                raise RuntimeError(
                    f"bootstrap failed: {cmd_argv[0]} …\n"
                    f"stderr: {r.stderr.decode('utf-8', 'replace')}",
                )

        await backend.write_file(
            ws_gateway_script(self.workspace_id),
            _read_gateway_script_bytes(),
        )

    # ── lease renew + reconnect ─────────────────────────────────────

    async def _renew_loop(self) -> None:
        while True:
            try:
                ttl = (
                    self._supervisor_info.get("ttl_seconds", self._lease_ttl)
                    if self._supervisor_info
                    else self._lease_ttl
                )
                interval = max(float(ttl) * 0.5, 1.0)
                await asyncio.sleep(interval)
                await self._supervisor_renew()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("lease renew failed: %s; reconnecting", e)
                try:
                    await self._reconnect()
                except Exception as re_err:
                    logger.error("reconnect failed: %s", re_err)

    async def _reconnect(self) -> None:
        async with self._reconnect_lock:
            await self._reconnect_locked()

    async def _reconnect_locked(self) -> None:
        old_remote = self.gateway_port
        await self._supervisor_start()
        new_port = self._supervisor_info["gateway_port"]

        if new_port != old_remote:
            if self._gw_listener is not None:
                try:
                    self._gw_listener.close()
                    await self._gw_listener.wait_closed()
                except Exception:
                    pass
            self._gw_listener = await self._conn.forward_local_port(
                "127.0.0.1",
                0,
                "127.0.0.1",
                new_port,
            )
            local = self._gw_listener.get_port()
            await self._gateway.reconnect(
                f"http://127.0.0.1:{local}",
                self._supervisor_info["auth_token"],
                self._supervisor_info["instance_nonce"],
            )
        else:
            await self._gateway.reconnect(
                self._gateway.base_url,
                self._supervisor_info["auth_token"],
                self._supervisor_info["instance_nonce"],
            )

        self.gateway_port = new_port

    # ── supervisor HTTP ─────────────────────────────────────────────

    async def _supervisor_start(self) -> None:
        import httpx

        url = f"http://127.0.0.1:{self._sup_listener.get_port()}/start"
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                url,
                json={
                    "workspace_id": self.workspace_id,
                    "lease_id": self._lease_id,
                },
                timeout=60.0,
            )
            if resp.status_code == 409:
                raise RuntimeError(
                    f"workspace {self.workspace_id!r} has active owner",
                )
            if resp.status_code == 410:
                raise RuntimeError(
                    f"gateway not bootstrapped for {self.workspace_id!r}",
                )
            resp.raise_for_status()
            self._supervisor_info = resp.json()
            self.gateway_port = self._supervisor_info["gateway_port"]

    async def _supervisor_renew(self) -> None:
        import httpx

        url = f"http://127.0.0.1:{self._sup_listener.get_port()}/renew"
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                url,
                json={
                    "workspace_id": self.workspace_id,
                    "lease_id": self._lease_id,
                },
                timeout=10.0,
            )
            resp.raise_for_status()

    async def _supervisor_release(self) -> None:
        import httpx

        url = f"http://127.0.0.1:{self._sup_listener.get_port()}/release"
        async with httpx.AsyncClient() as http:
            await http.post(
                url,
                json={
                    "workspace_id": self.workspace_id,
                    "lease_id": self._lease_id,
                },
                timeout=10.0,
            )

    # ── tools ───────────────────────────────────────────────────────

    async def list_tools(self) -> list:
        backend = self.get_backend()
        from ...tool import Edit, Glob, Grep, PowerShell, Read, Write

        glob = Glob(
            backend=backend,
            glob_helper_path=ws_glob_helper(self.workspace_id),
            python_bin=ws_gateway_python(self.workspace_id),
        )

        return [
            PowerShell(cwd=self.workdir, backend=backend),
            Edit(backend=backend),
            glob,
            Grep(backend=backend, rg_path=ws_ripgrep(self.workspace_id)),
            Read(backend=backend),
            Write(backend=backend),
        ]

    async def get_instructions(self) -> str:
        return self.instructions
