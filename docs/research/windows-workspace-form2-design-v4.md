# 远程 Windows Workspace 设计文档 v4

> 基于 v3 第三轮评审反馈重写。评审确认的 10 个问题（2 个 P0 + 6 个 P1 + 2 个 P2）
> 全部成立。本版逐一解决。代码基线：`9d1026fa`。
>
> v1: `docs/research/windows-workspace-form2-design.md`
> v2: `docs/research/windows-workspace-form2-design-v2.md`
> v3: `docs/research/windows-workspace-form2-design-v3.md`

> **上游同步说明（2026-08-12）**：本文记录基于 `9d1026fa` 的原始设计。
> 合入 `8f24009a` 后，MCP 声明改由 workspace 按 `(agent_id, session_id)`
> 持久化，gateway 以空 registry 启动并仅维护会话隔离的运行实例；skill 也改为
> agent 分区。因此下文关于 gateway 写 `.mcp`、`--config`、启动时加载 MCP，
> 以及 Windows `file://` 特例的内容均为历史方案，不代表当前实现。

---

## 0. v3 问题与本版对策

| # | v3 问题 | 严重度 | v4 对策 | 章节 |
|---|---------|--------|---------|------|
| 1 | gateway 启动缺 auth_token/nonce | P0 | `_launch_in_job()` cmdline 正式契约化，传入 token/nonce/日志重定向 | §4.4 |
| 2 | Service 与 SSH 用户提权路径 | P0 | 明确 **trusted single-tenant** 前提；supervisor 以 SSH 用户身份运行 gateway；目录 ACL | §4.6 |
| 3 | read-only lease 无协议无授权 | P1 | 首版**只支持单 owner**，移除 read-only lease 概念（简化） | §4.2 |
| 4 | 重启恢复状态机走不通 | P1 | renew 404 时自动重新 `/start`；renew loop 有重连逻辑 | §5.5 |
| 5 | .mcp 单写者未成立 | P1 | 覆写 `add_mcp`/`remove_mcp`，移除 host 端 `_save_mcp_file` | §5.6 |
| 6 | 初始化未同步 gateway MCP | P1 | `_setup_mcp_gateway` 末尾加 `list_mcps` 同步 | §5.4 |
| 7 | runner/bootstrap 无法运行（4 子问题） | P1 | runner 作为**部署前置**；修正 Process 构造/ToUnixTime/LOCALAPPDATA | §5.2, §5.7 |
| 8 | Job Object 清理不安全（4 子问题） | P1 | 全部 Win32 调用检查返回值；关闭 h_process；reconcile 不盲杀 PID | §4.4 |
| 9 | file URI 修复缺调用点 | P2 | 同时改调用点 `_base.py:608` | §6.1 |
| 10 | sweeper 持锁等 I/O | P2 | 锁内只更新状态收集待停列表，I/O 放锁外 | §4.3 |

---

## 1. 目标与范围

### 1.1 首版目标

Agent 的文件/exec 操作和 MCP 服务运行在远程 Windows 机器上。形态 2（常驻 supervisor +
动态 per-workspace gateway），**单 owner 模型**。

### 1.2 首版约束

| 约束 | 说明 |
|------|------|
| **Trusted single-tenant** | 一台 Windows 机器服务一个可信用户。SSH 用户和 supervisor **同一身份**（见 §4.6）。 |
| **单 owner** | 每个 workspace 同时只有一个活跃 lease。不支持 read-only lease（首版简化）。 |
| **不包含交互式 UI MCP** | Session 0 限制。 |
| **单台 Windows 机器** | supervisor 非集群。 |

---

## 2. 架构总览

```
Agent 主机 (任意 OS)                           远程 Windows 机器 (仅开放 SSH:22)
┌──────────────────────────────┐              ┌──────────────────────────────────────────┐
│ WindowsWorkspace             │   SSH        │  OpenSSH Server                          │
│  (继承 SandboxedWSBase)      │─────────────▶│                                          │
│                              │  exec_shell  │  ws-supervisor.exe (Windows Service)      │
│  WindowsSSHBackend           │  SFTP        │  以 SSH 用户身份运行 gateway              │
│   exec_shell ─► runner.ps1   │  + port fwd  │  loopback:7550                            │
│   read/write ─► SFTP         │              │                                          │
│   write_stream ─► SFTP       │              │  gateway (per-ws, loopback:动态端口)      │
│                              │              │     │ 管理 stdio MCP 子进程                │
│  GatewayTransport (httpx)    │  SSH tunnel  │     ▼                                    │
│  SupervisorClient (httpx)    │◀─────────────│  %PROGRAMDATA%\AgentScope\               │
│                              │              │    ws\{ws_id}\ (.mcp, skills, ...)       │
│                              │              │    runner\win_runner.ps1                 │
│                              │              │    uv\uv.exe (全局预装)                   │
└──────────────────────────────┘              └──────────────────────────────────────────┘
```

---

## 3. 部署模型（修正问题 7：runner 作为前置）

### 3.1 部署阶段（install.ps1 一次性完成）

v3 的致命时序错误：`_ensure_workspace_layout()` 在 bootstrap 之前运行，但它的 `exec_shell`
依赖 runner——而 runner 要到 `_bootstrap_gateway()` 才部署。

**v4 解决方案：runner 和 uv 在安装阶段预装，不在运行时部署。**

```powershell
# install.ps1（远程 Windows 上执行一次，管理员权限）

# 1. 创建目录结构
$ROOT = "$env:PROGRAMDATA\AgentScope"
New-Item -ItemType Directory -Force -Path "$ROOT\ws", "$ROOT\runner", "$ROOT\supervisor"

# 2. 安装 uv 到固定路径（不依赖 PATH）
$UV_DIR = "$ROOT\uv"
Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
# uv 安装脚本默认装到 %USERPROFILE%\.cargo\bin，我们复制到固定路径
Copy-Item "$env:USERPROFILE\.cargo\bin\uv.exe" "$UV_DIR\uv.exe" -Force

# 3. 部署 runner 脚本到固定路径
Copy-Item win_runner.ps1 "$ROOT\runner\win_runner.ps1"

# 4. 部署 supervisor
# supervisor 需要自己的 venv（与 gateway venv 分离）
python -m venv "$ROOT\supervisor\.venv"
& "$ROOT\supervisor\.venv\Scripts\pip" install fastapi uvicorn httpx pydantic

# 5. 注册 Windows Service（nssm）
nssm install AgentScopeSupervisor `
    "$ROOT\supervisor\.venv\Scripts\python.exe" "ws_supervisor.py"
nssm set AgentScopeSupervisor AppDirectory "$ROOT\supervisor"
nssm set AgentScopeSupervisor AppEnvironmentExtra "AS_ROOT=$ROOT"
nssm start AgentScopeSupervisor
```

**关键点**：
- `runner\win_runner.ps1` 和 `uv\uv.exe` 在**安装阶段**就部署到固定路径
- supervisor、runner、uv 的路径全部是**固定常量**，不由客户端控制
- `WindowsSSHBackend` 构造时就知道 runner 的固定路径，`_ensure_workspace_layout` 可以立即使用

### 3.2 路径常量（单一定义）

```python
# src/agentscope/workspace/_windows/_constants.py
import ntpath

AS_ROOT = r"C:\ProgramData\AgentScope"
WS_ROOT       = ntpath.join(AS_ROOT, "ws")
RUNNER_PATH   = ntpath.join(AS_ROOT, "runner", "win_runner.ps1")
UV_BIN        = ntpath.join(AS_ROOT, "uv", "uv.exe")
SUPERVISOR_PORT = 7550

def ws_workdir(ws_id: str) -> str:
    return ntpath.join(WS_ROOT, ws_id)

def ws_gateway_home(ws_id: str) -> str:
    return ntpath.join(ws_workdir(ws_id), ".gateway")

def ws_gateway_python(ws_id: str) -> str:
    return ntpath.join(ws_gateway_home(ws_id), ".venv", "Scripts", "python.exe")

def ws_gateway_script(ws_id: str) -> str:
    return ntpath.join(ws_gateway_home(ws_id), "_mcp_gateway_app.py")

def ws_mcp_file(ws_id: str) -> str:
    return ntpath.join(ws_workdir(ws_id), ".mcp")

def ws_gateway_log(ws_id: str) -> str:
    return ntpath.join(ws_gateway_home(ws_id), "gateway.log")
```

supervisor 和 workspace 共用这些常量，消除 v3 的路径不一致（问题 5d）。

---

## 4. Supervisor 设计

### 4.1 职责

- 拉起/看护/回收 gateway 进程（Job Object）
- 租约管理（TTL + heartbeat）
- **不做文件 I/O**

### 4.2 HTTP API（单 owner 模型，修正问题 3）

监听 `127.0.0.1:7550`。

#### `POST /start` — 申请 owner lease

```json
// Request
{"workspace_id": "ws-abc123", "lease_id": "lease-xyz789"}
// Response 200
{
  "workspace_id": "ws-abc123",
  "gateway_port": 5601,
  "auth_token": "tok_<32hex>",
  "instance_nonce": "n_<32hex>",
  "lease_id": "lease-xyz789",
  "expires_at": "2026-08-03T10:10:00Z",
  "ttl_seconds": 300,
  "status": "running",
  "ready": true
}
// Response 409: workspace 已有其他活跃 lease
// Response 410: gateway 未 bootstrap（gateway_script 不存在）
```

**幂等语义**：同一 `(workspace_id, lease_id)` 重试 → 返回同一结果（不新建 lease）。
若 lease 已过期或已 release → 视为新申请。

#### `POST /renew`

```json
// Request
{"workspace_id": "ws-abc123", "lease_id": "lease-xyz789"}
// Response 200: {"expires_at": "..."}
// Response 404: lease 不存在（已过期/已 release/supervisor 重启）
```

#### `POST /release`

```json
// Request
{"workspace_id": "ws-abc123", "lease_id": "lease-xyz789"}
// Response 200: {"gateway_stopped": false}   ← grace period 后才停
```

#### `GET /status/{workspace_id}` / `GET /healthz`

同 v3。

### 4.3 租约 TTL 状态机 + sweeper（修正问题 1/10）

```
                      /start
                        │
                        ▼
               ┌─────────────┐
       ┌───────│   ACTIVE    │◀──── /renew
       │       │ expires_at  │
       │       └─────────────┘
       │              │ TTL 过期 / /release
       │              ▼
       │       ┌─────────────┐
       │       │   GRACE     │ (60s) 新 /start 可激活
       │       └─────────────┘
       │              │ grace 过期
       │              ▼
       └────── ┌─────────────┐
               │   STOPPED    │ gateway 被杀，venv/.mcp 保留
               └─────────────┘
```

**sweeper（修正问题 10：锁外 I/O）：**

```python
class Supervisor:
    SWEEP_INTERVAL = 10.0

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(self.SWEEP_INTERVAL)
            now = utcnow()
            to_stop: list[str] = []   # ← 收集待停止项

            # ── 锁内：只更新状态 ──
            async with self._lock:
                for ws_id, entry in list(self._workspaces.items()):
                    # owner lease 过期
                    if entry.lease and entry.expires_at < now:
                        entry.lease = None
                        entry.grace_deadline = now + self.GRACE_PERIOD
                    # grace 过期
                    if (entry.lease is None and entry.grace_deadline
                            and entry.grace_deadline < now):
                        entry.status = "stopping"
                        to_stop.append(ws_id)   # ← 只收集，不 I/O

            # ── 锁外：执行进程停止（慢 I/O）──
            for ws_id in to_stop:
                try:
                    await self._stop_gateway(ws_id)
                except Exception:
                    logger.exception("stop_gateway %s failed", ws_id)
                async with self._lock:
                    e = self._workspaces.get(ws_id)
                    if e is not None:
                        e.status = "stopped"
                        e.grace_deadline = None
```

### 4.4 进程管理：Job Object（修正问题 1/8）

#### gateway 启动契约（修正问题 1：补全 auth_token/nonce/日志）

```python
import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CREATE_SUSPENDED         = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9

async def _launch_in_job(self, ws_id: str) -> dict:
    """创建 job → 挂起进程 → 加入 job → 恢复。

    所有 Win32 调用检查返回值（修正问题 8a）。
    """
    workdir = ws_workdir(ws_id)
    python = ws_gateway_python(ws_id)
    script = ws_gateway_script(ws_id)
    config = ws_mcp_file(ws_id)
    log    = ws_gateway_log(ws_id)

    auth_token = secrets.token_hex(16)       # /start 时生成
    instance_nonce = "n_" + secrets.token_hex(16)

    # ── gateway 启动命令行（正式契约，修正问题 1）──
    # 必须传 --auth-token 和 --instance-nonce，否则客户端 nonce 校验失败
    cmdline = (
        f'"{python}" -u "{script}"'
        f' --config "{config}"'
        f' --port {port}'
        f' --auth-token {auth_token}'         # ← v3 漏了
        f' --instance-nonce {instance_nonce}'  # ← v3 漏了
    )

    # 日志重定向：用 STARTUPINFO 的 hStdOutput
    # （或让 supervisor 的 CreateProcessW 用独立 stderr 重定向到 log 文件）

    # 1. 创建 Job Object
    h_job = kernel32.CreateJobObjectW(None, None)
    if not h_job:
        raise ctypes.WinError(ctypes.get_last_error())

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        h_job, JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())

    # 2. 创建挂起进程
    startup_info = STARTUPINFOW()
    startup_info.cb = ctypes.sizeof(startup_info)
    proc_info = PROCESS_INFORMATION()

    ok = kernel32.CreateProcessW(
        None, cmdline, None, None, False,
        CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP,
        None, workdir,
        ctypes.byref(startup_info),
        ctypes.byref(proc_info))
    if not ok:                                          # ← 检查返回值（问题 8a）
        kernel32.CloseHandle(h_job)
        raise ctypes.WinError(ctypes.get_last_error())

    # 3. 加入 Job Object（进程仍挂起，不会 spawn 子进程）
    if not kernel32.AssignProcessToJobObject(
        h_job, proc_info.hProcess):                     # ← 检查返回值
        kernel32.TerminateProcess(proc_info.hProcess, 1)
        kernel32.CloseHandle(proc_info.hProcess)
        kernel32.CloseHandle(proc_info.hThread)
        kernel32.CloseHandle(h_job)
        raise ctypes.WinError(ctypes.get_last_error())

    # 4. 恢复执行
    if kernel32.ResumeThread(proc_info.hThread) == -1:   # ← 检查返回值
        kernel32.TerminateProcess(proc_info.hProcess, 1)
        kernel32.CloseHandle(proc_info.hProcess)
        kernel32.CloseHandle(proc_info.hThread)
        kernel32.CloseHandle(h_job)
        raise ctypes.WinError(ctypes.get_last_error())

    kernel32.CloseHandle(proc_info.hThread)   # hThread 不再需要

    return {
        "h_job": h_job,
        "h_process": proc_info.hProcess,       # ← 保存（修正问题 8b：重启时关 h_process）
        "pid": proc_info.dwProcessId,
        "auth_token": auth_token,
        "instance_nonce": instance_nonce,
    }
```

#### 崩溃重启（修正问题 8b/8c）

```python
async def _on_gateway_exit(self, ws_id: str):
    entry = self._workspaces.get(ws_id)
    if entry is None or entry.status != "running":
        return

    # 1. 先终止并关闭旧 Job Object（杀残留子进程）
    if entry.h_job:
        kernel32.TerminateJobObject(entry.h_job, 1)
        kernel32.CloseHandle(entry.h_job)
        entry.h_job = None
    if entry.h_process:                    # ← 关闭 h_process（问题 8b）
        kernel32.CloseHandle(entry.h_process)
        entry.h_process = None

    # 2. 检查是否还应运行（问题 8c：退避期间检查状态）
    async with self._lock:
        if entry.lease is None:            # 已被 release/TTL 回收
            entry.status = "stopped"
            return

    # 3. 退避 + 重启上限
    entry.restart_count += 1
    if entry.restart_count > entry.max_restarts:
        entry.status = "dead"
        logger.error("gateway %s exceeded max_restarts", ws_id)
        return

    backoff = min(2 ** entry.restart_count, 30)
    await asyncio.sleep(backoff)

    # 再次检查（退避期间可能已被 release）
    async with self._lock:
        if entry.lease is None:
            entry.status = "stopped"
            return

    # 4. 重新拉起（新 job + 新进程）
    info = await self._launch_in_job(ws_id)
    entry.h_job = info["h_job"]
    entry.h_process = info["h_process"]
    entry.pid = info["pid"]
    entry.auth_token = info["auth_token"]
    entry.instance_nonce = info["instance_nonce"]

    await self._wait_ready(ws_id)
```

#### reconcile（修正问题 8d：不盲杀 PID）

```python
async def reconcile(self):
    """supervisor 启动时清理旧状态。

    KILL_ON_JOB_CLOSE 保证 supervisor 退出时旧 gateway 已被清理。
    不盲杀 PID（防 PID 复用误杀）。
    """
    state = self._load_state()

    for ws_id, rec in state.items():
        # 不用 taskkill 盲杀 PID（PID 可能已被复用）
        # 只重置状态，等下次 /start 重新拉起
        rec["lease"] = None
        rec["status"] = "stopped"
        rec["h_job"] = None
        rec["h_process"] = None
        rec["pid"] = None
        rec["grace_deadline"] = None
        # 验证 gateway venv 是否还在（决定下次 /start 走 bootstrap 还是快路径）
        script_exists = os.path.exists(ws_gateway_script(ws_id))
        rec["bootstrapped"] = script_exists

    self._save_state(state)
```

### 4.5 Config 版本模型（修正问题 5）

#### 单写者：移除 host 端 _save_mcp_file

v3 声称"workspace 层基本不用改"，但 `add_mcp`（`_sandboxed_base.py:288`）和
`remove_mcp`（`:312`）在 gateway HTTP 请求后仍调 `self._save_mcp_file()`——**第二写者**。

**v4 方案：`WindowsWorkspace` 覆写 `add_mcp`/`remove_mcp`，移除 host 端持久化。**

gateway 的 `POST /mcps`（`_mcp_gateway_app.py:94`）和 `DELETE /mcps/{name}`（`:112`）
已经在 gateway 内存中更新 MCP 列表。gateway 只需在自身写 `.mcp` 时用原子 replace。

```python
# WindowsWorkspace 覆写
async def add_mcp(self, mcp_client: MCPClient) -> None:
    """注册 MCP 到 gateway（gateway 负责持久化）。

    覆写父类：移除 _save_mcp_file（gateway 是唯一 .mcp 写者）。
    """
    if self._gateway is None:
        raise RuntimeError("Workspace has no MCP gateway attached.")
    async with self._mcp_lock:
        if any(m.name == mcp_client.name for m in self._mcps):
            raise ValueError(
                f"MCP {mcp_client.name!r} already exists in workspace.")
        spec = mcp_client.model_dump(mode="json")
        gw_client = self._gateway.make_client(spec)
        await gw_client.connect()       # POST /mcps（gateway 内存 + 原子写 .mcp）
        self._mcps.append(gw_client)
        # 不调 _save_mcp_file —— gateway 已持久化

async def remove_mcp(self, name: str) -> None:
    """从 gateway 注销 MCP（gateway 负责持久化）。"""
    if self._gateway is None:
        raise RuntimeError("Workspace has no MCP gateway attached.")
    async with self._mcp_lock:
        for i, mcp in enumerate(self._mcps):
            if mcp.name == name:
                await mcp.close()       # DELETE /mcps/{name}
                self._mcps.pop(i)
                # 不调 _save_mcp_file
                return
        logger.warning("MCP %r not found in workspace", name)
```

#### gateway 端原子写入

```python
# _mcp_gateway_app.py 改造
def _save_mcp_atomic(state, config_path):
    """gateway 唯一的 .mcp 持久化入口（原子 temp + os.replace）。"""
    import os, json, tempfile
    data = [c.model_dump(mode="json") for c in state.clients.values()]
    dir_ = os.path.dirname(config_path)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, config_path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise
```

`POST /mcps` 和 `DELETE /mcps/{name}` 在更新内存后调 `_save_mcp_atomic`。

### 4.6 安全模型（修正问题 2）

#### Trusted Single-Tenant 前提

v4 明确：**一台 Windows 机器服务一个可信用户。SSH 用户 = supervisor 运行身份。**

#### 执行身份对齐

```powershell
# install.ps1 中，supervisor 以 SSH 用户身份运行（不是 LocalSystem）
nssm install AgentScopeSupervisor `
    "$ROOT\supervisor\.venv\Scripts\python.exe" "ws_supervisor.py"
# 关键：不设 ObjectName → supervisor 以默认 Service 账号运行
# 但我们要求 SSH 用户和 Service 账号一致：

# 方法 A（推荐）：让 supervisor 不注册为 Service，而是以用户身份常驻
#   用 Task Scheduler "Run only when user is logged on" + 启动时运行
#   或直接在用户 session 里 nohup-equivalent 运行

# 方法 B：nssm 设为特定用户
nssm set AgentScopeSupervisor ObjectName ".\sshuser" "password"
```

#### 目录 ACL

```powershell
# workdir 只允许 SSH 用户和 supervisor（同一身份）读写
icacls "$ROOT\ws" /grant "sshuser:(OI)(CI)F" /inheritance:r
# runner/uv/ 只读
icacls "$ROOT\runner" /grant "sshuser:(OI)(CI)RX" /inheritance:r
icacls "$ROOT\uv" /grant "sshuser:(OI)(CI)RX" /inheritance:r
```

#### 提权路径关闭

因为 supervisor 以 SSH 用户身份运行 gateway，gateway 执行 `.mcp` 中的 stdio 命令时
**不会获得比 SSH 用户更高的权限**——提权路径关闭。

> **首版限制**：不支持多用户共享一台 Windows。如果需要多租户隔离，需要 Windows
> Container（形态 1）或每用户独立的 Windows 账号 + supervisor 实例。

---

## 5. Agent 主机端组件

### 5.1 WindowsSSHBackend（修正全部接口契约）

#### exec_shell — runner 模式

```python
class WindowsSSHBackend(BackendBase):
    _path_module = ntpath

    def __init__(self, conn, workdir: str, runner_path: str = RUNNER_PATH):
        self._conn = conn
        self._workdir = workdir
        self._runner_path = runner_path   # 固定路径（部署前置）

    async def exec_shell(self, command, *, cwd=None, timeout=None):
        import base64, json as _json

        payload = _json.dumps({
            "cwd": cwd or self._workdir,
            "argv": list(command),
            "timeout": timeout,
        })
        encoded = base64.b64encode(payload.encode("utf-16-le")).decode("ascii")

        # 通过 SSH 执行 runner（runner 已预装在固定路径）
        ssh_cmd = (
            f'powershell.exe -NoLogo -NoProfile -NonInteractive '
            f'-File "{self._runner_path}" -Payload {encoded}'
        )
        try:
            result = await asyncio.wait_for(
                self._conn.run(ssh_cmd, check=False),
                timeout=(timeout + 15) if timeout else None,
            )
        except asyncio.TimeoutError:
            return ExecResult(exit_code=-1, stdout=b"", stderr=b"timed out")

        return self._parse_runner_output(result)
```

#### 文件 I/O

```python
    async def read_file(self, path):
        async with self._conn.start_sftp_client() as sftp:
            async with sftp.open(path, "rb") as f:
                return await f.read()

    async def write_file(self, path, data):
        async with self._conn.start_sftp_client() as sftp:
            parent = ntpath.dirname(path)
            if parent:
                await sftp.makedirs(parent, exist_ok=True)
            async with sftp.open(path, "wb") as f:
                await f.write(data)

    async def write_stream(self, path, stream):
        async with self._conn.start_sftp_client() as sftp:
            parent = ntpath.dirname(path)
            if parent:
                await sftp.makedirs(parent, exist_ok=True)
            async with sftp.open(path, "wb") as f:
                async for chunk in stream:
                    await f.write(chunk)
```

#### 派生方法（修正 list_dir basename + stat_mtime epoch）

```python
    async def list_dir(self, path, *, recursive=False):
        """非递归返回 basename，递归返回完整路径。

        契约（_backend.py:436-448）：
        - recursive=False → base names（like os.listdir）
        - recursive=True  → 完整路径（like find path -type f）

        _find_skill_root（_base.py:845）依赖非递归返回 basename。
        """
        if recursive:
            r = await self.exec_shell(
                ["powershell", "-NoProfile", "-Command",
                 f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 f"Get-ChildItem -LiteralPath {ps_quote(path)} -Recurse -File | "
                 f"ForEach-Object {{ $_.FullName }}"])
        else:
            # -Name 返回 basename（非 FullName）
            r = await self.exec_shell(
                ["powershell", "-NoProfile", "-Command",
                 f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 f"Get-ChildItem -LiteralPath {ps_quote(path)} -Name"])
        if not r.ok():
            return []
        return [line for line in r.stdout.decode(errors="replace").splitlines() if line]

    async def stat_mtime(self, path):
        """返回 Unix epoch 秒。

        修正 v3 的 DateTime.ToUnixTimeSeconds()（不存在）。
        用 DateTimeOffset（.NET 4.6+ 有 ToUnixTimeSeconds）。
        """
        r = await self.exec_shell(
            ["powershell", "-NoProfile", "-Command",
             f"[long][DateTimeOffset]::new("
             f"(Get-Item -LiteralPath {ps_quote(path)}).LastWriteTime"
             f").ToUnixTimeSeconds()"])
        if not r.ok():
            return None
        try:
            return float(r.stdout.strip())
        except ValueError:
            return None

    async def file_exists(self, path):
        r = await self.exec_shell(
            ["powershell", "-NoProfile", "-Command",
             f"exit ![bool](Test-Path -LiteralPath {ps_quote(path)})"])
        return r.exit_code == 0

    async def is_dir(self, path):
        r = await self.exec_shell(
            ["powershell", "-NoProfile", "-Command",
             f"exit !((Get-Item -LiteralPath {ps_quote(path)}).PSIsContainer)"])
        return r.exit_code == 0

    async def delete_path(self, path):
        await self.exec_shell(
            ["powershell", "-NoProfile", "-Command",
             f"Remove-Item -LiteralPath {ps_quote(path)} -Recurse -Force "
             f"-ErrorAction SilentlyContinue"])

    async def getcwd(self):
        return self._workdir

    async def expanduser(self, path):
        if not path.startswith("~"):
            return path
        r = await self.exec_shell(
            ["powershell", "-NoProfile", "-Command", "$env:USERPROFILE"])
        home = r.stdout.decode(errors="replace").strip()
        if not home:
            return path
        return ntpath.join(home, path.lstrip("~/").replace("/", "\\"))
```

### 5.2 Runner 脚本（修正 .NET API）

```powershell
# win_runner.ps1 — 修正 Process 构造函数（问题 7b）
param([string]$Payload)
$ErrorActionPreference = "Stop"
$config = [System.Text.Encoding]::Unicode.GetString(
    [System.Convert]::FromBase64String($Payload)) | ConvertFrom-Json

# ConvertTo-WindowsCommandLineArg 按 CommandLineToArgvW 规则处理空参数、
# 空格、引号和反斜杠；完整实现见 deployments/windows/runner/win_runner.ps1。
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $config.argv[0]
$encodedArgs = @()
for ($i = 1; $i -lt $config.argv.Count; $i++) {
    # Windows PowerShell 5.1 没有 ProcessStartInfo.ArgumentList；按
    # CommandLineToArgvW 规则逐项编码后写入 $psi.Arguments。
    $encodedArgs += ConvertTo-WindowsCommandLineArg $config.argv[$i]
}
$psi.Arguments = $encodedArgs -join ' '
$psi.WorkingDirectory = $config.cwd
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true

# 修正：Process 没有接受 ProcessStartInfo 的公开构造函数
# 正确写法：创建实例后赋值 StartInfo
$proc = [System.Diagnostics.Process]::new()
$proc.StartInfo = $psi

$null = $proc.Start()  # stdout 只能包含最终 JSON envelope
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()

# timeout 处理（修正问题 6c：整进程树 kill）
$timedOut = $false
if ($config.timeout) {
    if (-not $proc.WaitForExit([int]([double]$config.timeout * 1000))) {
        # Windows PowerShell 5.1 / .NET Framework 没有 Kill($true)。
        & "$env:SystemRoot\System32\taskkill.exe" /PID $proc.Id /T /F |
            Out-Null
        $timedOut = $true
        $proc.WaitForExit()
    }
} else {
    $proc.WaitForExit()
}

$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderr = $stderrTask.GetAwaiter().GetResult()

$env = @{
    exit_code = if ($timedOut) { -1 } else { $proc.ExitCode }
    stdout = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($stdout))
    stderr = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes(
            $stderr + $(if ($timedOut) { "`n[timed out]" } else { "" })))
}
$env | ConvertTo-Json -Compress
```

### 5.3 GatewayTransport（修正问题 7/9）

```python
# src/agentscope/workspace/_gateway_transport.py
from typing import Any, Protocol


class GatewayTransport(Protocol):
    async def request(self, method: str, path: str, *,
                      body: Any = None, include_auth: bool = True
                      ) -> tuple[int, bytes]: ...
    async def aclose(self) -> None: ...
```

```python
# src/agentscope/workspace/_windows/_httpx_transport.py
import httpx

class HttpxTransport:
    """持久 httpx 连接池。复用核心依赖 httpx（pyproject.toml:30）。"""

    def __init__(self, base_url: str, *, auth_token: str | None = None,
                 timeout: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {auth_token}"} if auth_token else {},
        )
        self._auth_token = auth_token

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    async def request(self, method, path, *, body=None, include_auth=True):
        headers = {} if include_auth else {"Authorization": ""}
        resp = await self._client.request(method, path, json=body, headers=headers)
        return resp.status_code, resp.content

    async def aclose(self):
        await self._client.aclose()

    async def update_base_url(self, new_base_url: str, new_auth_token: str | None = None):
        """重连时更新 base_url 和 token（修正问题 4）。"""
        await self._client.aclose()
        self._client = httpx.AsyncClient(
            base_url=new_base_url,
            timeout=self._client.timeout,
            headers={"Authorization": f"Bearer {new_auth_token}"} if new_auth_token else {},
        )
        self._auth_token = new_auth_token
```

### 5.4 WindowsWorkspace（修正生命周期 + 同步 MCP + 失败回滚）

```python
class WindowsWorkspace(SandboxedWorkspaceBase):

    def __init__(self, *, workspace_id=None,
                 host, port=22, username,
                 password=None, client_keys=None,
                 lease_ttl=300,
                 default_mcps=None, skill_paths=None,
                 extra_pip=None,
                 instructions=DEFAULT_WORKSPACE_INSTRUCTIONS):
        super().__init__(workspace_id=workspace_id,
                         default_mcps=default_mcps,
                         skill_paths=skill_paths)

        self.workdir = ws_workdir(self.workspace_id)
        self._gateway_home = ws_gateway_home(self.workspace_id)
        self.gateway_port = 0   # supervisor 返回后填入
        self.extra_pip = list(extra_pip or [])
        self.instructions = instructions.format(
            backend="Windows (remote SSH)", workdir=self.workdir)

        self._ssh_cfg = dict(host=host, port=port, username=username,
                             password=password, client_keys=client_keys)
        self._lease_ttl = lease_ttl
        self._lease_id = f"lease-{uuid.uuid4().hex[:12]}"
        self._renew_task: asyncio.Task | None = None

        self._conn = None
        self._sup_listener = None
        self._gw_listener = None
        self._supervisor_info = None
        self._transport: HttpxTransport | None = None

    # ── OS 适配 hook（名与 §6.1 完全一致）──────────────────
    @property
    def _python_interpreter(self) -> str:
        return ws_gateway_python(self.workspace_id)

    @property
    def _tmp_dir(self) -> str:
        return ntpath.join(AS_ROOT, "tmp")

    async def _shell_makedirs(self, *dirs):
        await self._backend.exec_shell(
            ["powershell", "-NoProfile", "-Command",
             "$dirs = @(" + ",".join(ps_quote(d) for d in dirs) + "); "
             "foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path $d }"])

    async def _shell_move(self, src, dst):
        await self._backend.exec_shell(
            ["powershell", "-NoProfile", "-Command",
             f"Move-Item -LiteralPath {ps_quote(src)} "
             f"-Destination {ps_quote(dst)} -Force"])

    async def _shell_kill_stale_gateway(self):
        pass   # supervisor 管

    async def _shell_launch_gateway(self, python, script, config, port, log):
        pass   # supervisor 管

    # ── 钩子 A：SSH + backend + supervisor tunnel ───────────
    async def _provision_backend(self):
        import asyncssh
        self._conn = await asyncssh.connect(**self._ssh_cfg)
        self._backend = WindowsSSHBackend(self._conn, self.workdir)
        self._sup_listener = await self._conn.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1", SUPERVISOR_PORT,
        )

    # ── 钩子 B：释放租约 + 关 tunnel + 断 SSH ────────────────
    async def _teardown_backend(self):
        if self._renew_task is not None:
            self._renew_task.cancel()
            try: await self._renew_task
            except (asyncio.CancelledError, Exception): pass
            self._renew_task = None

        if self._transport is not None:
            try: await self._transport.aclose()
            except Exception: pass
            self._transport = None

        if self._supervisor_info is not None:
            try: await self._supervisor_release()
            except Exception: pass

        for listener in (self._gw_listener, self._sup_listener):
            if listener is not None:
                try:
                    listener.close()
                    await listener.wait_closed()
                except Exception: pass
        self._gw_listener = None
        self._sup_listener = None

        if self._conn is not None:
            self._conn.close()
            try: await self._conn.wait_closed()
            except Exception: pass
            self._conn = None

    # ── 覆写 _setup_mcp_gateway ─────────────────────────────
    async def _setup_mcp_gateway(self):
        backend = self.get_backend()

        # 1. 首次 bootstrap（建 venv + 装脚本）
        if not await backend.file_exists(ws_gateway_script(self.workspace_id)):
            await self._bootstrap_gateway(backend)

        # 2. 调 supervisor /start
        await self._supervisor_start()

        # 3. 建 gateway tunnel
        self._gw_listener = await self._conn.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1",
            self._supervisor_info["gateway_port"],
        )
        gw_port = self._gw_listener.get_port()

        # 4. 构造 transport
        self._transport = HttpxTransport(
            base_url=f"http://127.0.0.1:{gw_port}",
            auth_token=self._supervisor_info["auth_token"],
        )

        # 5. 构造 GatewayClient
        from .._gateway_client import GatewayClient
        self._gateway = GatewayClient(
            transport=self._transport,
            gateway_port=gw_port,
            instance_nonce=self._supervisor_info["instance_nonce"],
            gateway_log_path=ws_gateway_log(self.workspace_id),
        )

        # 6. 轮询 /health
        deadline = asyncio.get_event_loop().time() + 30.0
        delay = 0.5
        while asyncio.get_event_loop().time() < deadline:
            if await self._gateway.health():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        else:
            try:
                log = await backend.read_file(ws_gateway_log(self.workspace_id))
                tail = log[-2000:].decode(errors="replace")
            except Exception:
                tail = "<no log>"
            raise RuntimeError(f"gateway unhealthy.\nLog tail:\n{tail}")

        # 7. 同步 gateway 已有 MCP（修正问题 6）
        self._mcps = list(await self._gateway.list_mcps())

        # 8. 启动 renew loop
        self._renew_task = asyncio.create_task(self._renew_loop())

    # ── initialize 失败回滚（修正问题 1c）───────────────────
    async def initialize(self):
        if self.is_alive:
            return
        try:
            await self._provision_backend()
            assert self._backend is not None
            await self._ensure_workspace_layout()
            await self._setup_mcp_gateway()
            await self._setup_skills()
        except Exception:
            try:
                if self._gateway is not None:
                    await self._gateway.aclose()
                    self._gateway = None
                await self._teardown_backend()
            except Exception:
                pass
            raise
        self.is_alive = True

    # ── renew loop（修正问题 4：404 时重新 /start）──────────
    async def _renew_loop(self):
        renew_interval = self._lease_ttl * 0.5
        while True:
            try:
                await asyncio.sleep(renew_interval)
                await self._supervisor_renew()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("lease renew failed: %s", e)
                # 尝试重连（修正问题 4）
                try:
                    await self._reconnect()
                except Exception as re_err:
                    logger.error("reconnect failed: %s", re_err)
                    # 下次循环继续重试

    async def _reconnect(self):
        """lease 失效后重新 /start 并重建 tunnel/transport。"""
        # 1. 重新 /start（幂等）
        await self._supervisor_start()

        # 2. 如果端口变了，重建 tunnel
        new_port = self._supervisor_info["gateway_port"]
        old_port = self._gw_listener.get_port() if self._gw_listener else None

        if new_port != old_port:
            if self._gw_listener is not None:
                try:
                    self._gw_listener.close()
                    await self._gw_listener.wait_closed()
                except Exception: pass
            self._gw_listener = await self._conn.forward_local_port(
                "127.0.0.1", 0, "127.0.0.1", new_port,
            )
            new_local = self._gw_listener.get_port()
            await self._transport.update_base_url(
                f"http://127.0.0.1:{new_local}",
                self._supervisor_info["auth_token"],
            )
        else:
            # 端口没变，只更新 token
            await self._transport.update_base_url(
                self._transport.base_url,
                self._supervisor_info["auth_token"],
            )

        # 3. 更新 GatewayClient 的 nonce
        self._gateway.instance_nonce = self._supervisor_info["instance_nonce"]

        # 4. 重新同步 MCP wrappers
        self._mcps = list(await self._gateway.list_mcps())

    # ── bootstrap（修正问题 7：uv 绝对路径 + argv runner）──
    async def _bootstrap_gateway(self, backend):
        from .._utils import (
            _GATEWAY_BASE_REQUIREMENTS,
            _read_gateway_script_bytes,
            _read_glob_helper_bytes,
        )

        # 1. 建 gateway venv + 装依赖（复用 _GATEWAY_BASE_REQUIREMENTS）
        deps = list(_GATEWAY_BASE_REQUIREMENTS) + list(self.extra_pip)
        venv = ntpath.join(self._gateway_home, ".venv")

        # uv 在固定路径（UV_BIN），不依赖 PATH/LOCALAPPDATA（修正问题 7d）
        commands = [
            [UV_BIN, "venv", venv],
            [UV_BIN, "pip", "install",
             "--python", self._python_interpreter] + deps,
            [UV_BIN, "pip", "install",
             "--python", self._python_interpreter,
             "--no-deps", "agentscope"],
            # ripgrep 装到 venv Scripts
            [UV_BIN, "pip", "install",
             "--python", self._python_interpreter, "ripgrep"],
        ]
        for cmd_argv in commands:
            r = await backend.exec_shell(
                cmd_argv,   # argv 数组，由 runner 按 Windows 参数规则编码
                timeout=self._bootstrap_cmd_timeout,
            )
            if not r.ok():
                raise RuntimeError(
                    f"bootstrap failed: {cmd_argv}: "
                    f"{r.stderr.decode(errors='replace')}")

        # 2. 写 glob helper 脚本
        await backend.write_file(
            ntpath.join(self._gateway_home, "_glob_helper.py"),
            _read_glob_helper_bytes(),
        )

        # 3. 写 gateway 脚本
        await backend.write_file(
            ws_gateway_script(self.workspace_id),
            _read_gateway_script_bytes(),
        )

    # ── supervisor 交互 ─────────────────────────────────────
    async def _supervisor_start(self):
        import httpx
        url = f"http://127.0.0.1:{self._sup_listener.get_port()}/start"
        async with httpx.AsyncClient() as http:
            resp = await http.post(url, json={
                "workspace_id": self.workspace_id,
                "lease_id": self._lease_id,
            }, timeout=60)
            if resp.status_code == 409:
                raise RuntimeError(f"workspace {self.workspace_id} has active lease")
            if resp.status_code == 410:
                raise RuntimeError(f"gateway not bootstrapped")
            resp.raise_for_status()
            self._supervisor_info = resp.json()

    async def _supervisor_renew(self):
        import httpx
        url = f"http://127.0.0.1:{self._sup_listener.get_port()}/renew"
        async with httpx.AsyncClient() as http:
            resp = await http.post(url, json={
                "workspace_id": self.workspace_id,
                "lease_id": self._lease_id,
            }, timeout=10)
            resp.raise_for_status()

    async def _supervisor_release(self):
        import httpx
        url = f"http://127.0.0.1:{self._sup_listener.get_port()}/release"
        async with httpx.AsyncClient() as http:
            await http.post(url, json={
                "workspace_id": self.workspace_id,
                "lease_id": self._lease_id,
            }, timeout=10)

    async def get_instructions(self):
        return self.instructions

    # ── add_mcp / remove_mcp 覆写（修正问题 5）──────────────
    async def add_mcp(self, mcp_client):
        """gateway 是唯一 .mcp 写者，不调 host _save_mcp_file。"""
        if self._gateway is None:
            raise RuntimeError("Workspace has no MCP gateway attached.")
        async with self._mcp_lock:
            if any(m.name == mcp_client.name for m in self._mcps):
                raise ValueError(f"MCP {mcp_client.name!r} already exists.")
            spec = mcp_client.model_dump(mode="json")
            gw_client = self._gateway.make_client(spec)
            await gw_client.connect()
            self._mcps.append(gw_client)
            # 不调 _save_mcp_file

    async def remove_mcp(self, name):
        """gateway 负责持久化。"""
        if self._gateway is None:
            raise RuntimeError("Workspace has no MCP gateway attached.")
        async with self._mcp_lock:
            for i, mcp in enumerate(self._mcps):
                if mcp.name == name:
                    await mcp.close()
                    self._mcps.pop(i)
                    # 不调 _save_mcp_file
                    return
            logger.warning("MCP %r not found", name)

    # ── list_tools（工具链闭环）─────────────────────────────
    async def list_tools(self):
        from ...tool import Edit, Glob, Grep, PowerShell, Read, Write
        backend = self.get_backend()
        glob_helper = ntpath.join(self._gateway_home, "_glob_helper.py")
        rg_path = ntpath.join(
            self._gateway_home, ".venv", "Scripts", "rg.exe")

        return [
            PowerShell(cwd=self.workdir, backend=backend),
            Edit(backend=backend),
            Glob(backend=backend,
                 glob_helper_path=glob_helper,
                 python_bin=self._python_interpreter),
            Grep(backend=backend, rg_path=rg_path),
            Read(backend=backend),
            Write(backend=backend),
        ]
```

### 5.5 辅助函数

```python
def ps_quote(s: str) -> str:
    """PowerShell 单引号字符串字面量。"""
    return "'" + s.replace("'", "''") + "'"
```

---

## 6. 对现有代码的改造

### 6.1 父类改造

**`WorkspaceBase`（`_base.py`）：**

```python
class WorkspaceBase:
    @property
    def _python_interpreter(self) -> str:
        return "python3"

    @property
    def _tmp_dir(self) -> str:
        return "/tmp"

    async def _shell_makedirs(self, *dirs):
        backend = self.get_backend()
        await backend.exec_shell(["mkdir", "-p", *dirs], cwd="/")

    async def _shell_move(self, src, dst):
        backend = self.get_backend()
        await backend.exec_shell(["mv", src, dst])

    @staticmethod
    def _path_to_file_uri(path: str, backend: "BackendBase | None" = None) -> str:
        if path.startswith("/"):
            return f"file://{path}"
        if backend is not None and backend.isabs(path):
            return "file:///" + path.replace("\\", "/")
        return Path(path).as_uri()
```

**同时改调用点（修正问题 9）：**
```python
# _base.py:608 — 传 backend
url=AnyUrl(self._path_to_file_uri(path, backend=backend)),
```

**`SandboxedWorkspaceBase`（`_sandboxed_base.py`）：**

新增 `_shell_kill_stale_gateway` / `_shell_launch_gateway` hook（默认 POSIX 行为）。
`_setup_mcp_gateway` 内部改为调这些 hook。
`initialize()` 加 try/finally 回滚。

**`GatewayClient`（`_gateway_client.py`）：**

```python
class GatewayClient:
    def __init__(self, backend=None, gateway_port=0, *,
                 transport=None,    # ← 新增
                 ...):
        self.backend = backend
        self._transport = transport
        ...

    async def exec_request(self, method, path, *, body=None, include_auth=True):
        if self._transport is not None:
            return await self._transport.request(method, path, body=body,
                                                  include_auth=include_auth)
        assert self.backend is not None
        # 旧 shim 路径不变
```

### 6.2 gateway 改造

`_mcp_gateway_app.py`：`POST /mcps` 和 `DELETE /mcps/{name}` 更新内存后调
`_save_mcp_atomic`（temp + `os.replace`）。

### 6.3 工具改动

| 文件 | 改动 |
|------|------|
| `_glob.py` | 新增 `python_bin` 参数 |
| `_grep.py` | 新增 `rg_path` 参数 |
| `_base.py:608` | `_path_to_file_uri` 传 `backend` |

### 6.4 改动清单

| 文件 | 改动 | 行数（估） |
|------|------|-----------|
| `_base.py` | 4 hook + _path_to_file_uri + 调用点 | ~85 |
| `_sandboxed_base.py` | 2 hook + initialize try/finally | ~50 |
| `_gateway_client.py` | transport 参数 + 分支 | ~30 |
| `_glob.py` | python_bin | ~10 |
| `_grep.py` | rg_path | ~10 |
| `_mcp_gateway_app.py` | _save_mcp_atomic | ~20 |
| `_windows_ssh_backend.py` | **新增** | ~280 |
| `_windows_workspace.py` | **新增** | ~350 |
| `_httpx_transport.py` | **新增** | ~60 |
| `_gateway_transport.py` | **新增** | ~20 |
| `_constants.py` | **新增** | ~25 |
| `_windows/__init__.py` | **新增** | ~5 |
| `_windows_workspace_manager.py` | **新增**（可选） | ~130 |
| `ws_supervisor.py` | **新增** | ~350 |
| `win_runner.ps1` | **新增** | ~55 |
| `install.ps1` | **新增** | ~50 |
| `pyproject.toml` | `workspace-ssh` 组 | ~3 |
| **总计** | | **~1530** |

---

## 7. 数据流

### 7.1 初始化

```
WindowsWorkspace.initialize()  ← 含 try/except 回滚
├─ _provision_backend()
│  ├─ asyncssh.connect()
│  ├─ WindowsSSHBackend(conn, workdir)  ← runner 已预装
│  └─ forward_local_port("127.0.0.1", 0, ...) → listener.get_port()
├─ _ensure_workspace_layout()   ← exec_shell 用预装的 runner
├─ _setup_mcp_gateway()
│  ├─ if 首次: _bootstrap_gateway()  ← uv 绝对路径 + argv
│  ├─ _supervisor_start()             ← POST /start
│  ├─ forward_local_port → get_port()
│  ├─ HttpxTransport(127.0.0.1:{port})
│  ├─ GatewayClient(transport=...)
│  ├─ health() 轮询
│  ├─ self._mcps = list(await gateway.list_mcps())  ← 同步（修正问题 6）
│  └─ renew_task = create_task(_renew_loop())
└─ _setup_skills()
```

### 7.2 renew + 重连（修正问题 4）

```
_renew_loop()
├─ sleep(TTL/2)
├─ _supervisor_renew()
│  ├─ 200 → 续期成功
│  ├─ 404 → lease 过期/supervisor 重启
│  │  └─ _reconnect()
│  │     ├─ _supervisor_start()        ← 重新获取 port/token/nonce
│  │     ├─ if 端口变了: 重建 tunnel + update_base_url
│  │     ├─ 更新 GatewayClient nonce
│  │     └─ self._mcps = list_mcps()   ← 重新同步 MCP wrappers
│  └─ 网络错误 → _reconnect()
└─ 重复
```

---

## 8. Session 0

首版排除交互式 UI MCP（同 v3）。trusted single-tenant 下 supervisor 可选以用户 session
运行（非 Service），但首版以 Service + single-tenant 为基准。

---

## 9. POSIX 硬编码清单（v4 最终）

共 19 处（同 v3），处理方式不变。

---

## 10. 落地步骤

| 阶段 | 内容 | 验证标准 |
|------|------|----------|
| **P0a** | PoC: runner.ps1 正确执行 argv（含 Process 构造修正） | `exec_shell` 返回正确 exit/stdout/stderr |
| **P0b** | PoC: SSH local forward + `.get_port()` | tunnel 可用 |
| **P0c** | PoC: Job Object CREATE_SUSPENDED → Assign → Resume（全部检查返回值） | 进程在 Job 内；杀 Job 清理子进程 |
| **P0d** | PoC: gateway 带 `--auth-token`/`--instance-nonce` 启动，nonce 校验通过 | `/health` 返回匹配 nonce |
| **P1** | `install.ps1`（预装 runner/uv/supervisor） | 一键安装 |
| **P2** | `ws_supervisor.py`（TTL sweeper + Job Object + reconcile） | /start → /renew → TTL → grace → stop |
| **P3** | `WindowsSSHBackend`（list_dir basename、stat_mtime DateTimeOffset） | 全部派生方法正确 |
| **P4** | 父类 OS hook + initialize try/finally + _path_to_file_uri 调用点 | 现有后端不回归 |
| **P5** | GatewayTransport + GatewayClient 改造 | 现有 shim 不回归 |
| **P6** | `WindowsWorkspace` 完整（含 add_mcp/remove_mcp 覆写 + list_mcps 同步 + reconnect） | 全链路 |
| **P7** | gateway 原子 .mcp + 工具链修复 | 并发安全 |
| **P8** | Manager + 集成测试 | 全场景 |

---

## 11. 风险矩阵（v4）

| 风险 | 严重度 | v4 缓解 | 残余 |
|------|--------|---------|------|
| gateway 缺 nonce | 高 | cmdline 契约化（§4.4） | 低 |
| 提权路径 | 高 | trusted single-tenant + 同身份运行 + ACL | 低 |
| runner 时序 | 高 | **部署前置**（§3.1） | 低 |
| Process 构造 | 高 | `Process::new()` + `.StartInfo=`（§5.2） | 低 |
| ToUnixTime | 高 | `DateTimeOffset`（§5.1） | 低 |
| LOCALAPPDATA | 高 | uv 固定路径 `UV_BIN`（§5.4） | 低 |
| list_dir basename | 高 | `-Name` 返回 basename | 低 |
| .mcp 双写者 | 高 | 覆写 add/remove_mcp | 低 |
| MCP 未同步 | 高 | `_setup_mcp_gateway` 末尾 list_mcps | 低 |
| 重连状态机 | 中 | renew 404 → /start + 重建 tunnel | 低 |
| Job Object 安全 | 中 | 全部检查返回值 + 关 h_process + 不盲杀 PID | 低 |
| sweeper 阻塞 | 中 | 锁外 I/O | 低 |
| file URI | 低 | 调用点传 backend | 低 |

---

## 附录：v3 → v4 差异

| 维度 | v3 | v4 |
|------|----|----|
| gateway 启动参数 | 漏 token/nonce | cmdline 契约化 |
| 安全模型 | 未定义 | trusted single-tenant + 同身份 + ACL |
| read-only lease | 概念无实现 | 移除（首版单 owner） |
| runner 部署 | 运行时部署（时序矛盾） | **部署前置** |
| Process 构造 | `::new($psi)`（不存在） | `::new()` + `.StartInfo=` |
| stat_mtime | `DateTime.ToUnixTimeSeconds()`（不存在） | `DateTimeOffset` |
| uv 路径 | 主机 LOCALAPPDATA | 固定 `UV_BIN` |
| .mcp 写者 | 双写者 | 覆写 add/remove_mcp |
| MCP 同步 | 遗漏 | `_setup_mcp_gateway` 末尾 list_mcps |
| 重连 | /renew 404 后继续用旧 info | 404 → /start + 重建 tunnel + 同步 MCP |
| Job Object | 无返回值检查 + 盲杀 PID | 全部检查 + 不盲杀 |
| sweeper | 锁内 I/O | 锁外 I/O |
| _path_to_file_uri | 只改签名不改调用 | 同时改 `_base.py:608` |
| 总行数 | ~1490 | ~1530 |
