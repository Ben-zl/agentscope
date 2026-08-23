# -*- coding: utf-8 -*-
"""远端 backend 的内置文件工具缓存回归测试。"""

import os
from unittest.async_case import IsolatedAsyncioTestCase

from agentscope.state import AgentState
from agentscope.tool import BackendBase, Edit, ExecResult, Read, Write


class _RemoteMemoryBackend(BackendBase):
    """模拟文件仅存在于远端 workspace 的 backend。"""

    def __init__(self) -> None:
        """初始化远端文件、mtime 与状态查询开关。"""
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, float] = {}
        self.stat_available = True
        self.freeze_mtime = False
        self.change_mtime_on_read = False
        self.fail_next_write = False

    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """为 Write 创建目录返回成功。"""
        del command, cwd, timeout
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    async def read_file(self, path: str) -> bytes:
        """从远端内存文件系统读取文件。"""
        if self.change_mtime_on_read:
            self.mtimes[path] = self.mtimes.get(path, 0.0) + 1.0
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        """写入文件，并在未冻结时推进 mtime。"""
        if self.fail_next_write:
            self.fail_next_write = False
            raise OSError("simulated backend write failure")
        self.files[path] = data
        if not self.freeze_mtime:
            self.mtimes[path] = self.mtimes.get(path, 0.0) + 1.0

    async def file_exists(self, path: str) -> bool:
        """判断远端文件是否存在。"""
        return path in self.files

    async def is_dir(self, path: str) -> bool:
        """测试 backend 只保存普通文件。"""
        del path
        return False

    async def stat_mtime(self, path: str) -> float | None:
        """返回远端 mtime；关闭查询时返回 None。"""
        if not self.stat_available:
            return None
        return self.mtimes.get(path)


class BackendAwareFileCacheTest(IsolatedAsyncioTestCase):
    """通过公开文件工具验证 host 不可见路径的缓存行为。"""

    async def asyncSetUp(self) -> None:
        """创建隔离 backend、工具和 AgentState。"""
        self.backend = _RemoteMemoryBackend()
        self.file_path = "/workspace/agent-cache-test.txt"
        await self.backend.write_file(self.file_path, b"alpha\n")
        self.assertFalse(os.path.exists(self.file_path))

        self.state = AgentState()
        self.read_tool = Read(backend=self.backend)
        self.edit_tool = Edit(backend=self.backend)
        self.write_tool = Write(backend=self.backend)

    async def test_edit_after_read_succeeds_for_backend_only_file(
        self,
    ) -> None:
        """Read 后应能编辑只存在于远端 backend 的文件。"""
        read = await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )
        edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="beta",
            _agent_state=self.state,
        )

        self.assertEqual(read.state.value, "running")
        self.assertEqual(edit.state.value, "running")
        self.assertEqual(
            await self.backend.read_file(self.file_path),
            b"beta\n",
        )
        self.assertEqual(self.state.tool_context.read_file_cache, [])

    async def test_write_after_read_succeeds_for_backend_only_file(
        self,
    ) -> None:
        """Read 后应能覆盖只存在于远端 backend 的文件。"""
        await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )
        write = await self.write_tool(
            file_path=self.file_path,
            content="gamma\n",
            _agent_state=self.state,
        )

        self.assertEqual(write.state.value, "running")
        self.assertEqual(
            await self.backend.read_file(self.file_path),
            b"gamma\n",
        )
        self.assertEqual(self.state.tool_context.read_file_cache, [])

    async def test_existing_backend_file_must_be_read_before_modification(
        self,
    ) -> None:
        """未 Read 的远端既有文件应拒绝 Edit 和 Write。"""
        edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="beta",
            _agent_state=self.state,
        )
        write = await self.write_tool(
            file_path=self.file_path,
            content="gamma\n",
            _agent_state=self.state,
        )

        self.assertEqual(edit.state.value, "error")
        self.assertIn("must first read", edit.content[0].text)
        self.assertEqual(write.state.value, "error")
        self.assertIn("has not been read yet", write.content[0].text)
        self.assertEqual(
            await self.backend.read_file(self.file_path),
            b"alpha\n",
        )

    async def test_changed_backend_mtime_rejects_stale_edit(self) -> None:
        """Read 后 mtime 变化时不得使用陈旧内容执行 Edit。"""
        await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )
        await self.backend.write_file(self.file_path, b"external\n")

        edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="beta",
            _agent_state=self.state,
        )

        self.assertEqual(edit.state.value, "error")
        self.assertIn("must first read", edit.content[0].text)
        self.assertEqual(
            await self.backend.read_file(self.file_path),
            b"external\n",
        )
        self.assertEqual(self.state.tool_context.read_file_cache, [])

    async def test_unavailable_backend_mtime_fails_closed(self) -> None:
        """无法取得 backend mtime 时不得授权修改既有文件。"""
        self.backend.stat_available = False

        read = await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )
        edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="beta",
            _agent_state=self.state,
        )
        write = await self.write_tool(
            file_path=self.file_path,
            content="gamma\n",
            _agent_state=self.state,
        )

        self.assertEqual(read.state.value, "running")
        self.assertEqual(self.state.tool_context.read_file_cache, [])
        self.assertEqual(edit.state.value, "error")
        self.assertIn("Cannot verify the file state", edit.content[0].text)
        self.assertEqual(write.state.value, "error")
        self.assertIn("Cannot verify the file state", write.content[0].text)

    async def test_frozen_mtime_requires_reread_after_each_edit(self) -> None:
        """固定 mtime 下每次 Edit 后仍应重新建立已读证明。"""
        self.backend.freeze_mtime = True
        await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )

        first_edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="beta",
            _agent_state=self.state,
        )
        second_edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="gamma",
            _agent_state=self.state,
        )

        self.assertEqual(first_edit.state.value, "running")
        self.assertEqual(second_edit.state.value, "error")
        self.assertIn("must first read", second_edit.content[0].text)
        self.assertEqual(
            await self.backend.read_file(self.file_path),
            b"beta\n",
        )

        await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )
        third_edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="beta",
            new_string="gamma",
            _agent_state=self.state,
        )

        self.assertEqual(third_edit.state.value, "running")
        self.assertEqual(
            await self.backend.read_file(self.file_path),
            b"gamma\n",
        )

    async def test_explicit_none_mtime_removes_existing_cache(self) -> None:
        """显式 None mtime 应失败关闭并移除已有缓存。"""
        await self.state.tool_context.cache_file(
            file_path=self.file_path,
            lines=["alpha\n"],
            mtime=1.0,
        )

        await self.state.tool_context.cache_file(
            file_path=self.file_path,
            lines=["stale\n"],
            mtime=None,
        )

        self.assertEqual(self.state.tool_context.read_file_cache, [])

    async def test_agent_state_serialization_preserves_backend_cache(
        self,
    ) -> None:
        """AgentState JSON 往返应保留既有缓存数据结构。"""
        await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )

        restored = AgentState.model_validate_json(self.state.model_dump_json())

        self.assertEqual(len(restored.tool_context.read_file_cache), 1)
        entry = restored.tool_context.read_file_cache[0]
        self.assertEqual(entry.file_path, self.file_path)
        self.assertEqual(entry.lines, ["alpha\n"])
        self.assertEqual(entry.updated_at, 1.0)

    async def test_mtime_change_during_read_does_not_create_cache(
        self,
    ) -> None:
        """读取期间文件变化时不得为该内容创建已读证明。"""
        self.backend.change_mtime_on_read = True

        read = await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )
        edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="beta",
            _agent_state=self.state,
        )

        self.assertEqual(read.state.value, "running")
        self.assertEqual(self.state.tool_context.read_file_cache, [])
        self.assertEqual(edit.state.value, "error")
        self.assertIn("must first read", edit.content[0].text)

    async def test_failed_edit_attempt_invalidates_cache(self) -> None:
        """backend 写入失败后再次 Edit 应要求重新 Read。"""
        await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )
        self.backend.fail_next_write = True

        failed_edit = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="beta",
            _agent_state=self.state,
        )
        retry = await self.edit_tool(
            file_path=self.file_path,
            old_string="alpha",
            new_string="beta",
            _agent_state=self.state,
        )

        self.assertEqual(failed_edit.state.value, "error")
        self.assertIn(
            "simulated backend write failure",
            failed_edit.content[0].text,
        )
        self.assertEqual(retry.state.value, "error")
        self.assertIn("must first read", retry.content[0].text)

    async def test_failed_write_attempt_invalidates_cache(self) -> None:
        """backend 写入失败后再次 Write 应要求重新 Read。"""
        await self.read_tool(
            file_path=self.file_path,
            _agent_state=self.state,
        )
        self.backend.fail_next_write = True

        with self.assertRaisesRegex(
            OSError, "simulated backend write failure"
        ):
            await self.write_tool(
                file_path=self.file_path,
                content="beta\n",
                _agent_state=self.state,
            )
        retry = await self.write_tool(
            file_path=self.file_path,
            content="beta\n",
            _agent_state=self.state,
        )

        self.assertEqual(retry.state.value, "error")
        self.assertIn("has not been read yet", retry.content[0].text)
