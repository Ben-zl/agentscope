# -*- coding: utf-8 -*-
"""SSH/SFTP backend for a remote Windows host.

Implements :class:`BackendBase` by delegating to ``asyncssh`` for
command execution and SFTP for file I/O.  The remote environment is
Windows, so:

* :attr:`_path_module` is :mod:`ntpath` (drive letters, backslashes).
* ``exec_shell`` serialises the argv + cwd as a JSON payload,
  Base64-encodes it (UTF-16-LE), and invokes a pre-installed
  ``win_runner.ps1`` on the remote host. The runner applies Windows
  command-line escaping without invoking a child shell, preserving the
  argv contract of :meth:`BackendBase.exec_shell`.
* Derived filesystem helpers (``file_exists``, ``is_dir``,
  ``list_dir``, ``stat_mtime``, ``delete_path``) are overridden with
  PowerShell equivalents because the base-class defaults use POSIX
  commands (``test``, ``find``, ``stat``, ``rm``) that do not exist
  on Windows.
"""

from __future__ import annotations

import asyncio
import base64
import json as _json
import ntpath
from typing import TYPE_CHECKING, Any, AsyncIterator

from ...tool import BackendBase, ExecResult

if TYPE_CHECKING:
    import asyncssh


#: PowerShell single-quote helper.
def ps_quote(s: str) -> str:
    """Return *s* as a PowerShell single-quoted literal."""
    return "'" + s.replace("'", "''") + "'"


class WindowsSSHBackend(BackendBase):
    """Backend that delegates to a remote Windows host via SSH/SFTP."""

    _path_module = ntpath

    def __init__(
        self,
        conn: "asyncssh.SSHClientConnection",
        workdir: str,
        runner_path: str,
    ) -> None:
        """Initialise the backend.

        Args:
            conn: An active ``asyncssh.SSHClientConnection``.
            workdir (`str`): Default working directory inside the
                remote Windows host.
            runner_path (`str`): Path to the pre-installed
                ``win_runner.ps1`` on the remote host.
        """
        self._conn = conn
        self._workdir = workdir
        self._runner_path = runner_path

    # ── exec ────────────────────────────────────────────────────────

    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Run *command* on the remote host via the runner script.

        The runner receives argv + cwd + timeout as a Base64-encoded
        JSON payload and starts the process without a child shell. On
        timeout the runner terminates the process tree.
        """
        payload = _json.dumps(
            {
                "cwd": cwd or self._workdir,
                "argv": list(command),
                "timeout": timeout,
            }
        )
        encoded = base64.b64encode(
            payload.encode("utf-16-le"),
        ).decode("ascii")

        # The runner path is fixed by the deployment contract. Base64 contains
        # no cmd.exe metacharacters, so only the path needs Windows quoting.
        ssh_cmd = (
            "powershell.exe -NoLogo -NoProfile -NonInteractive "
            f'-File "{self._runner_path}" -Payload {encoded}'
        )

        # Give the runner a grace period beyond the inner timeout so
        # it has time to kill the process tree and report back.
        outer_timeout = (timeout + 15) if timeout else None

        try:
            result = await asyncio.wait_for(
                self._conn.run(ssh_cmd, check=False),
                timeout=outer_timeout,
            )
        except asyncio.TimeoutError:
            return ExecResult(
                exit_code=-1,
                stdout=b"",
                stderr=b"timed out",
            )

        return self._parse_runner_output(result)

    @staticmethod
    def _parse_runner_output(result: Any) -> ExecResult:
        """Parse the JSON envelope emitted by ``win_runner.ps1``."""
        if result.exit_status != 0:
            # Runner itself failed — surface stderr.
            stderr = (
                result.stderr
                if isinstance(result.stderr, bytes)
                else str(result.stderr).encode("utf-8", "replace")
            )
            return ExecResult(
                exit_code=result.exit_status,
                stdout=b"",
                stderr=stderr,
            )

        stdout_bytes = (
            result.stdout
            if isinstance(result.stdout, bytes)
            else str(result.stdout).encode("utf-8", "replace")
        )
        try:
            env = _json.loads(stdout_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return ExecResult(
                exit_code=-1,
                stdout=stdout_bytes,
                stderr=b"runner produced non-JSON output",
            )

        stdout = base64.b64decode(env.get("stdout", ""))
        stderr = base64.b64decode(env.get("stderr", ""))
        return ExecResult(
            exit_code=int(env.get("exit_code", -1)),
            stdout=stdout,
            stderr=stderr,
        )

    # ── file I/O (SFTP) ─────────────────────────────────────────────

    async def read_file(self, path: str) -> bytes:
        async with self._conn.start_sftp_client() as sftp:
            async with sftp.open(path, "rb") as f:
                return await f.read()

    async def write_file(self, path: str, data: bytes) -> None:
        async with self._conn.start_sftp_client() as sftp:
            parent = ntpath.dirname(path)
            if parent:
                await sftp.makedirs(parent, exist_ok=True)
            async with sftp.open(path, "wb") as f:
                await f.write(data)

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
    ) -> None:
        async with self._conn.start_sftp_client() as sftp:
            parent = ntpath.dirname(path)
            if parent:
                await sftp.makedirs(parent, exist_ok=True)
            async with sftp.open(path, "wb") as f:
                async for chunk in stream:
                    await f.write(chunk)

    # ── derived filesystem ops (PowerShell overrides) ───────────────

    async def file_exists(self, path: str) -> bool:
        r = await self.exec_shell(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"if (Test-Path -LiteralPath {ps_quote(path)}) "
                "{ exit 0 } else { exit 1 }",
            ],
        )
        return r.exit_code == 0

    async def is_dir(self, path: str) -> bool:
        r = await self.exec_shell(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"if (Test-Path -LiteralPath {ps_quote(path)} "
                "-PathType Container) { exit 0 } else { exit 1 }",
            ],
        )
        return r.exit_code == 0

    async def list_dir(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> list[str]:
        """List entries under *path*.

        Non-recursive returns **base names** (like ``os.listdir``);
        recursive returns **full paths** (like ``find path -type f``).
        This matches the :meth:`BackendBase.list_dir` contract that
        ``_find_skill_root`` depends on.
        """
        prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        if recursive:
            cmd = (
                f"{prefix}Get-ChildItem -LiteralPath {ps_quote(path)} "
                "-Recurse -File | ForEach-Object { $_.FullName }"
            )
        else:
            cmd = f"{prefix}Get-ChildItem -LiteralPath {ps_quote(path)} -Name"
        r = await self.exec_shell(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
        )
        if not r.ok():
            return []
        return [
            line
            for line in r.stdout.decode("utf-8", "replace").splitlines()
            if line
        ]

    async def stat_mtime(self, path: str) -> float | None:
        """Return the mtime of *path* as a Unix timestamp (seconds)."""
        # DateTimeOffset.ToUnixTimeSeconds() (available .NET 4.6+).
        r = await self.exec_shell(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"[long][DateTimeOffset]::new("
                f"(Get-Item -LiteralPath {ps_quote(path)}).LastWriteTime"
                f").ToUnixTimeSeconds()",
            ],
        )
        if not r.ok():
            return None
        try:
            return float(r.stdout.decode("utf-8", "replace").strip())
        except ValueError:
            return None

    async def delete_path(self, path: str) -> None:
        await self.exec_shell(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Remove-Item -LiteralPath {ps_quote(path)} -Recurse -Force "
                f"-ErrorAction SilentlyContinue",
            ],
        )

    async def getcwd(self) -> str:
        return self._workdir

    async def expanduser(self, path: str) -> str:
        if not path or path[0] != "~":
            return path
        if len(path) > 1 and path[1] not in ("/", ntpath.sep):
            return path  # ~user form unsupported
        r = await self.exec_shell(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$env:USERPROFILE",
            ],
        )
        home = r.stdout.decode("utf-8", "replace").strip()
        if not home:
            return path
        return ntpath.join(home, path.lstrip("~/").replace("/", "\\"))
