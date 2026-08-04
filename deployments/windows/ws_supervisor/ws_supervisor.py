# -*- coding: utf-8 -*-
"""Windows workspace supervisor — manages per-workspace gateway processes.

Runs as a Windows Service (registered via ``nssm`` or ``pywin32``).
Exposes a small HTTP API on ``127.0.0.1:7550`` (loopback only).
Agent hosts reach it through an SSH local port forward.

Design (see ``docs/research/windows-workspace-form2-design-v4.md``):

* **Single owner** — one active lease per workspace.
* **TTL + heartbeat** — leases expire automatically; the sweeper
  moves expired workspaces to a grace period before stopping the
  gateway.
* **Job Object** — each gateway process is placed in a Windows Job
  Object with ``KILL_ON_JOB_CLOSE`` so a supervisor crash cleans up
  all child processes.
* **Reconcile** — on restart, state is reset (old gateways were
  already killed by the Job Object); no blind ``taskkill`` by PID.
* **Lock-free I/O in sweeper** — the global lock only collects
  workspaces to stop; the actual ``TerminateJobObject`` happens
  outside the lock.

Win32 Job Object calls use ``ctypes`` directly (no pywin32 dependency
on the hot path).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import msvcrt
import os
import re
import secrets
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import uvicorn
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ── Configuration ────────────────────────────────────────────────────

AS_ROOT = os.environ.get(
    "AS_ROOT",
    r"C:\ProgramData\AgentScope",
)
WS_ROOT = os.path.join(AS_ROOT, "ws")
SUPERVISOR_PORT = int(os.environ.get("SUPERVISOR_PORT", "7550"))
STATE_FILE = os.path.join(AS_ROOT, "supervisor", "state.json")
DEFAULT_TTL = float(os.environ.get("LEASE_TTL", "300"))
GRACE_PERIOD = float(os.environ.get("GRACE_PERIOD", "60"))
SWEEP_INTERVAL = 10.0
MAX_RESTARTS = 3
GATEWAY_PORT_START = 5601
GATEWAY_PORT_END = 5700

# ── Win32 Job Object constants ──────────────────────────────────────

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
STARTF_USESTDHANDLES = 0x00000100

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ── Win32 structure definitions ─────────────────────────────────────


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


# Configure Win32 function prototypes.
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [
    ctypes.c_void_p,
    wintypes.LPCWSTR,
]

kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]

kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [
    wintypes.HANDLE,
    wintypes.HANDLE,
]

kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]

kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [
    wintypes.HANDLE,
    wintypes.UINT,
]

kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


# ── Data model ──────────────────────────────────────────────────────


@dataclass
class WorkspaceEntry:
    """Runtime state of a single workspace's gateway."""

    workspace_id: str
    gateway_port: int = 0
    pid: int = 0
    h_job: int = 0
    h_process: int = 0
    auth_token: str = ""
    instance_nonce: str = ""
    lease_id: str = ""
    expires_at: float = 0.0
    grace_deadline: float = 0.0
    status: str = "stopped"  # stopped | running | stopping | restarting | dead
    restart_count: int = 0
    bootstrapped: bool = False


# ── Path helpers ────────────────────────────────────────────────────


def _ws_workdir(ws_id: str) -> str:
    return os.path.join(WS_ROOT, ws_id)


def _ws_gw_home(ws_id: str) -> str:
    return os.path.join(_ws_workdir(ws_id), ".gateway")


def _ws_gw_python(ws_id: str) -> str:
    return os.path.join(_ws_gw_home(ws_id), ".venv", "Scripts", "python.exe")


def _ws_gw_script(ws_id: str) -> str:
    return os.path.join(_ws_gw_home(ws_id), "_mcp_gateway_app.py")


def _ws_mcp_file(ws_id: str) -> str:
    return os.path.join(_ws_workdir(ws_id), ".mcp")


def _ws_gw_log(ws_id: str) -> str:
    return os.path.join(_ws_gw_home(ws_id), "gateway.log")


def _is_bootstrapped(ws_id: str) -> bool:
    return os.path.isfile(_ws_gw_script(ws_id))


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _validate_workspace_id(ws_id: str) -> str:
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", ws_id)
        or ws_id.endswith(".")
        or ws_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise HTTPException(400, "invalid workspace_id")
    return ws_id.casefold()


# ── Supervisor ──────────────────────────────────────────────────────


class Supervisor:
    """Manages workspace gateway processes."""

    def __init__(self) -> None:
        self._workspaces: dict[str, WorkspaceEntry] = {}
        self._lock = asyncio.Lock()
        self._port_pool: set[int] = set()  # currently allocated ports

    # ── state persistence ──

    def _load_state(self) -> None:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for ws_id in data.get("workspaces", {}):
            try:
                ws_id = _validate_workspace_id(ws_id)
            except HTTPException:
                continue
            e = WorkspaceEntry(workspace_id=ws_id)
            e.bootstrapped = _is_bootstrapped(ws_id)
            self._workspaces[ws_id] = e

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        data = {"workspaces": {}}
        for ws_id, e in self._workspaces.items():
            data["workspaces"][ws_id] = {
                "bootstrapped": e.bootstrapped,
                "status": e.status,
            }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)

    async def reconcile(self) -> None:
        """On startup, reset state (Job Objects already cleaned up)."""
        self._load_state()
        # All workspaces start as 'stopped'; leases are invalidated.
        # No taskkill by PID (KILL_ON_JOB_CLOSE already cleaned up).

    # ── port allocation ──

    def _alloc_port(self) -> int:
        for p in range(GATEWAY_PORT_START, GATEWAY_PORT_END):
            if p not in self._port_pool:
                self._port_pool.add(p)
                return p
        raise RuntimeError("no free gateway ports")

    def _free_port(self, port: int) -> None:
        self._port_pool.discard(port)

    # ── lease management ──

    def _now(self) -> float:
        return time.time()

    async def start_or_attach(
        self,
        ws_id: str,
        lease_id: str,
    ) -> WorkspaceEntry:
        ws_id = _validate_workspace_id(ws_id)
        async with self._lock:
            entry = self._workspaces.get(ws_id)
            if entry is None:
                entry = WorkspaceEntry(workspace_id=ws_id)
                entry.bootstrapped = _is_bootstrapped(ws_id)
                self._workspaces[ws_id] = entry

            # Check for conflicting owner.
            if (
                entry.lease_id
                and entry.lease_id != lease_id
                and entry.expires_at > self._now()
            ):
                raise HTTPException(409, "workspace has active owner")

            if not entry.bootstrapped:
                raise HTTPException(
                    410,
                    "gateway not bootstrapped; run bootstrap first",
                )

            # Idempotent: same lease_id reusing existing entry.
            if entry.status == "running" and entry.lease_id == lease_id:
                entry.expires_at = self._now() + DEFAULT_TTL
                return entry

            # Need to launch gateway.
            if entry.status == "restarting":
                raise HTTPException(503, "gateway is restarting")
            if entry.status == "stopping" and entry.h_job:
                # The sweeper marked this entry for stopping but has not
                # detached its Job Object yet. A new lease cancels that stop.
                entry.status = "running"
            elif entry.status != "running":
                await self._launch_gateway(entry)
                entry.status = "running"
                entry.restart_count = 0

            entry.lease_id = lease_id
            entry.expires_at = self._now() + DEFAULT_TTL
            entry.grace_deadline = 0.0
            self._save_state()
            return entry

    async def renew(self, ws_id: str, lease_id: str) -> WorkspaceEntry:
        ws_id = _validate_workspace_id(ws_id)
        async with self._lock:
            entry = self._workspaces.get(ws_id)
            if (
                entry is None
                or entry.lease_id != lease_id
                or entry.expires_at <= self._now()
            ):
                raise HTTPException(404, "lease not found")
            entry.expires_at = self._now() + DEFAULT_TTL
            return entry

    async def release(self, ws_id: str, lease_id: str) -> dict[str, Any]:
        ws_id = _validate_workspace_id(ws_id)
        async with self._lock:
            entry = self._workspaces.get(ws_id)
            if entry is None or entry.lease_id != lease_id:
                return {"gateway_stopped": False}
            entry.lease_id = ""
            entry.expires_at = 0.0
            entry.grace_deadline = self._now() + GRACE_PERIOD
            self._save_state()
            return {"gateway_stopped": False}

    def status(self, ws_id: str) -> dict[str, Any]:
        ws_id = _validate_workspace_id(ws_id)
        entry = self._workspaces.get(ws_id)
        if entry is None:
            raise HTTPException(404, "workspace not found")
        return {
            "workspace_id": ws_id,
            "status": entry.status,
            "gateway_port": entry.gateway_port,
            "lease_id": entry.lease_id,
            "restart_count": entry.restart_count,
        }

    # ── process management ──

    async def _launch_gateway(
        self,
        entry: WorkspaceEntry,
        *,
        preserve_identity: bool = False,
    ) -> None:
        """Launch gateway in a Job Object (CREATE_SUSPENDED → assign → resume)."""
        ws_id = entry.workspace_id
        allocated_port = not preserve_identity
        if preserve_identity:
            port = entry.gateway_port
            if not port or not entry.auth_token or not entry.instance_nonce:
                raise RuntimeError(
                    "cannot preserve incomplete gateway identity"
                )
            auth_token = entry.auth_token
            instance_nonce = entry.instance_nonce
        else:
            port = self._alloc_port()
            auth_token = "tok_" + secrets.token_hex(16)
            instance_nonce = "n_" + secrets.token_hex(16)

        python = _ws_gw_python(ws_id)
        script = _ws_gw_script(ws_id)
        config = _ws_mcp_file(ws_id)
        workdir = _ws_workdir(ws_id)

        cmdline = (
            f'"{python}" -u "{script}"'
            f' --config "{config}"'
            f" --port {port}"
            f" --auth-token {auth_token}"
            f" --instance-nonce {instance_nonce}"
        )

        # 1. Create Job Object.
        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            if allocated_port:
                self._free_port(port)
            raise RuntimeError(
                f"CreateJobObjectW failed: {ctypes.get_last_error()}",
            )

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            err = ctypes.get_last_error()
            kernel32.CloseHandle(h_job)
            if allocated_port:
                self._free_port(port)
            raise RuntimeError(f"SetInformationJobObject failed: {err}")

        # 2. Create suspended process.
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        pi = PROCESS_INFORMATION()
        log_file = None
        stdin_file = None
        log_handle = None
        stdin_handle = None
        try:
            os.makedirs(_ws_gw_home(ws_id), exist_ok=True)
            log_file = open(_ws_gw_log(ws_id), "ab", buffering=0)
            stdin_file = open(os.devnull, "rb", buffering=0)
            log_handle = msvcrt.get_osfhandle(log_file.fileno())
            stdin_handle = msvcrt.get_osfhandle(stdin_file.fileno())
            os.set_handle_inheritable(log_handle, True)
            os.set_handle_inheritable(stdin_handle, True)
            si.dwFlags |= STARTF_USESTDHANDLES
            si.hStdInput = stdin_handle
            si.hStdOutput = log_handle
            si.hStdError = log_handle

            ok = kernel32.CreateProcessW(
                None,
                ctypes.create_unicode_buffer(cmdline),
                None,
                None,
                True,
                CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP,
                None,
                workdir,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
        except Exception:
            kernel32.CloseHandle(h_job)
            if allocated_port:
                self._free_port(port)
            raise
        finally:
            if log_handle is not None:
                try:
                    os.set_handle_inheritable(log_handle, False)
                except OSError:
                    pass
            if stdin_handle is not None:
                try:
                    os.set_handle_inheritable(stdin_handle, False)
                except OSError:
                    pass
            if log_file is not None:
                log_file.close()
            if stdin_file is not None:
                stdin_file.close()
        if not ok:
            kernel32.CloseHandle(h_job)
            if allocated_port:
                self._free_port(port)
            err = ctypes.get_last_error()
            raise RuntimeError(
                f"CreateProcessW failed: {err}",
            )

        # 3. Assign to Job Object (while suspended).
        if not kernel32.AssignProcessToJobObject(h_job, pi.hProcess):
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(h_job)
            if allocated_port:
                self._free_port(port)
            raise RuntimeError(
                f"AssignProcessToJobObject failed: "
                f"{ctypes.get_last_error()}",
            )

        # 4. Resume.
        if kernel32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(h_job)
            if allocated_port:
                self._free_port(port)
            raise RuntimeError("ResumeThread failed")

        kernel32.CloseHandle(pi.hThread)

        entry.gateway_port = port
        entry.auth_token = auth_token
        entry.instance_nonce = instance_nonce
        entry.h_job = h_job
        entry.h_process = pi.hProcess
        entry.pid = pi.dwProcessId

        # Start a watcher task for crash recovery.
        asyncio.create_task(self._watch_process(entry, pi.hProcess))

    async def _watch_process(
        self,
        entry: WorkspaceEntry,
        h_process: int,
    ) -> None:
        """Watch the gateway process; restart on unexpected exit."""
        # Wait in a thread (WaitForSingleObject is blocking).
        loop = asyncio.get_event_loop()
        wait_status = await loop.run_in_executor(
            None,
            _wait_for_process,
            h_process,
        )
        if wait_status != WAIT_OBJECT_0:
            print(
                f"gateway wait failed for {entry.workspace_id!r}: "
                f"status={wait_status}, winerror={ctypes.get_last_error()}",
                flush=True,
            )
            return
        kernel32.CloseHandle(h_process)
        async with self._lock:
            if entry.h_process != h_process:
                return
            entry.h_process = 0
            if entry.status != "running":
                return  # Expected exit (we're stopping it).
            entry.status = "restarting"
        await self._on_gateway_exit(entry)

    async def _on_gateway_exit(self, entry: WorkspaceEntry) -> None:
        """Handle unexpected gateway exit — restart or mark dead."""
        async with self._lock:
            if entry.status != "restarting":
                return
            # Close the old job. The watcher already closed h_process.
            if entry.h_job:
                kernel32.TerminateJobObject(entry.h_job, 1)
                kernel32.CloseHandle(entry.h_job)
                entry.h_job = 0
            entry.restart_count += 1
            if entry.restart_count > MAX_RESTARTS:
                entry.status = "dead"
                self._free_port(entry.gateway_port)
                entry.gateway_port = 0
                self._save_state()
                return
            if not entry.lease_id:
                entry.status = "stopped"
                self._free_port(entry.gateway_port)
                entry.gateway_port = 0
                self._save_state()
                return

            backoff = min(2**entry.restart_count, 30)

        await asyncio.sleep(backoff)

        async with self._lock:
            if entry.status != "restarting":
                return
            if not entry.lease_id or entry.expires_at <= self._now():
                entry.lease_id = ""
                entry.status = "stopped"
                self._free_port(entry.gateway_port)
                entry.gateway_port = 0
                self._save_state()
                return
            try:
                await self._launch_gateway(entry, preserve_identity=True)
            except Exception:
                entry.status = "dead"
                self._free_port(entry.gateway_port)
                entry.gateway_port = 0
                self._save_state()
                return
            launched_pid = entry.pid

        try:
            ready = await self._wait_gateway_ready(entry)
        except Exception as error:
            print(
                f"gateway readiness check failed for "
                f"{entry.workspace_id!r}: {error}",
                flush=True,
            )
            ready = False

        async with self._lock:
            if entry.status != "restarting" or entry.pid != launched_pid:
                return
            if ready and entry.lease_id and entry.expires_at > self._now():
                entry.status = "running"
                self._save_state()
                return
            if entry.h_job:
                kernel32.TerminateJobObject(entry.h_job, 1)
                kernel32.CloseHandle(entry.h_job)
                entry.h_job = 0
            entry.status = "dead" if entry.lease_id else "stopped"
            self._free_port(entry.gateway_port)
            entry.gateway_port = 0
            self._save_state()

    async def _wait_gateway_ready(
        self,
        entry: WorkspaceEntry,
        timeout: float = 30.0,
    ) -> bool:
        """Wait for a relaunched gateway to report the expected nonce."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        url = f"http://127.0.0.1:{entry.gateway_port}/health"
        async with httpx.AsyncClient(timeout=2.0) as client:
            while loop.time() < deadline:
                try:
                    response = await client.get(url)
                    payload = response.json()
                    if (
                        response.status_code == 200
                        and isinstance(payload, dict)
                        and payload.get("instance_nonce")
                        == entry.instance_nonce
                    ):
                        return True
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.25)
        return False

    async def stop_gateway(self, ws_id: str) -> None:
        """Detach gateway state under lock, then stop it outside the lock."""
        async with self._lock:
            entry = self._workspaces.get(ws_id)
            if entry is None or entry.status != "stopping":
                return
            h_job = entry.h_job
            entry.h_job = 0
            # The watcher owns and closes the process handle after the job is
            # terminated. Detaching it prevents a later launch from confusing
            # that watcher with the new process.
            entry.h_process = 0
            entry.status = "stopped"
            self._free_port(entry.gateway_port)
            entry.gateway_port = 0
            self._save_state()
        if h_job:
            kernel32.TerminateJobObject(h_job, 1)
            kernel32.CloseHandle(h_job)

    # ── sweeper ──

    async def sweep_loop(self) -> None:
        """Periodically expire leases and stop grace-expired gateways."""
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            now = self._now()
            to_stop: list[str] = []

            async with self._lock:
                for ws_id, entry in list(self._workspaces.items()):
                    # Expire owner lease.
                    if entry.lease_id and entry.expires_at < now:
                        entry.lease_id = ""
                        entry.grace_deadline = now + GRACE_PERIOD
                    # Grace period expired.
                    if (
                        not entry.lease_id
                        and entry.grace_deadline
                        and entry.grace_deadline < now
                        and entry.status == "running"
                    ):
                        entry.status = "stopping"
                        to_stop.append(ws_id)

            # Stop outside the lock.
            for ws_id in to_stop:
                try:
                    await self.stop_gateway(ws_id)
                except Exception as error:
                    print(
                        f"failed to stop gateway {ws_id!r}: {error}",
                        flush=True,
                    )


def _wait_for_process(h_process: int) -> int:
    """Block until the process exits and return the wait status."""
    INFINITE = 0xFFFFFFFF
    return kernel32.WaitForSingleObject(h_process, INFINITE)


WAIT_OBJECT_0 = 0
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
]


# ── HTTP API ────────────────────────────────────────────────────────


supervisor = Supervisor()
app = FastAPI(title="agentscope-windows-supervisor")


class StartRequest(BaseModel):
    workspace_id: str
    lease_id: str


class ReleaseRequest(BaseModel):
    workspace_id: str
    lease_id: str


class RenewRequest(BaseModel):
    workspace_id: str
    lease_id: str


@app.on_event("startup")
async def _startup() -> None:
    await supervisor.reconcile()
    asyncio.create_task(supervisor.sweep_loop())


@app.get("/healthz")
async def healthz() -> str:
    return "ok"


@app.post("/start")
async def start_endpoint(req: StartRequest) -> dict[str, Any]:
    entry = await supervisor.start_or_attach(
        req.workspace_id,
        req.lease_id,
    )
    return {
        "workspace_id": entry.workspace_id,
        "gateway_port": entry.gateway_port,
        "auth_token": entry.auth_token,
        "instance_nonce": entry.instance_nonce,
        "lease_id": entry.lease_id,
        "expires_at": datetime.fromtimestamp(
            entry.expires_at,
            tz=timezone.utc,
        ).isoformat(),
        "ttl_seconds": DEFAULT_TTL,
        "status": entry.status,
        "ready": entry.status == "running",
    }


@app.post("/renew")
async def renew_endpoint(req: RenewRequest) -> dict[str, Any]:
    entry = await supervisor.renew(req.workspace_id, req.lease_id)
    return {
        "lease_id": entry.lease_id,
        "expires_at": datetime.fromtimestamp(
            entry.expires_at,
            tz=timezone.utc,
        ).isoformat(),
    }


@app.post("/release")
async def release_endpoint(req: ReleaseRequest) -> dict[str, Any]:
    return await supervisor.release(req.workspace_id, req.lease_id)


@app.get("/status/{workspace_id}")
async def status_endpoint(workspace_id: str) -> dict[str, Any]:
    return supervisor.status(workspace_id)


def main() -> None:
    """Run the supervisor (called by the Windows Service wrapper)."""
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=SUPERVISOR_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
