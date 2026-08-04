# -*- coding: utf-8 -*-
"""Fixed path constants for the remote Windows workspace.

All paths are derived from :data:`AS_ROOT` so the supervisor (running
on the Windows host) and the :class:`WindowsWorkspace` (running on the
agent host) agree on where every file lives.  These values are not
configurable by the client — they are part of the deployment contract
(see ``deployments/windows/install.ps1``).
"""

import ntpath
import re

#: Root of all AgentScope-managed files on the Windows host.
AS_ROOT = r"C:\ProgramData\AgentScope"

#: Per-workspace directories live here.
WS_ROOT = ntpath.join(AS_ROOT, "ws")

#: Pre-installed ``win_runner.ps1`` (the exec_shell helper).
RUNNER_PATH = ntpath.join(AS_ROOT, "runner", "win_runner.ps1")

#: Pre-installed ``uv`` binary (installed by ``install.ps1``).
UV_BIN = ntpath.join(AS_ROOT, "uv", "uv.exe")

#: Port the supervisor listens on (loopback only).
SUPERVISOR_PORT = 7550

#: Port range the supervisor draws gateway ports from.
GATEWAY_PORT_RANGE = range(5601, 5700)


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_workspace_id(workspace_id: str) -> str:
    """Validate and canonicalize a Windows workspace identifier."""
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
        workspace_id,
    ) or workspace_id.endswith("."):
        raise ValueError(f"Invalid Windows workspace_id: {workspace_id!r}")
    device_name = workspace_id.split(".", 1)[0].upper()
    if device_name in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Invalid Windows workspace_id: {workspace_id!r}")
    return workspace_id.casefold()


def ws_workdir(workspace_id: str) -> str:
    """Return the workdir for *workspace_id*."""
    return ntpath.join(WS_ROOT, workspace_id)


def ws_gateway_home(workspace_id: str) -> str:
    """Return the gateway home dir for *workspace_id*."""
    return ntpath.join(ws_workdir(workspace_id), ".gateway")


def ws_gateway_python(workspace_id: str) -> str:
    """Return the gateway venv python path for *workspace_id*."""
    return ntpath.join(
        ws_gateway_home(workspace_id),
        ".venv",
        "Scripts",
        "python.exe",
    )


def ws_gateway_script(workspace_id: str) -> str:
    """Return the gateway entry-script path for *workspace_id*."""
    return ntpath.join(ws_gateway_home(workspace_id), "_mcp_gateway_app.py")


def ws_mcp_file(workspace_id: str) -> str:
    """Return the ``.mcp`` config path for *workspace_id*."""
    return ntpath.join(ws_workdir(workspace_id), ".mcp")


def ws_gateway_log(workspace_id: str) -> str:
    """Return the gateway log path for *workspace_id*."""
    return ntpath.join(ws_gateway_home(workspace_id), "gateway.log")


def ws_glob_helper(workspace_id: str) -> str:
    """Return the glob-helper script path for *workspace_id*."""
    return ntpath.join(ws_gateway_home(workspace_id), "_glob_helper.py")


def ws_ripgrep(workspace_id: str) -> str:
    """Return the ripgrep binary path (in venv Scripts) for *workspace_id*."""
    return ntpath.join(
        ws_gateway_home(workspace_id),
        ".venv",
        "Scripts",
        "rg.exe",
    )
