# -*- coding: utf-8 -*-
"""Direct-HTTP gateway client for Windows workspaces.

Subclasses :class:`GatewayClient` and overrides :meth:`exec_request`
to talk to the gateway over a persistent ``httpx`` connection (tunneled
via SSH local port forwarding) instead of the default exec-shell shim.

The inherited facade retains the real remote backend for diagnostics. This
subclass inherits ``health``, ``list_mcps``, and ``make_client`` and only
replaces request dispatch and connection cleanup.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

from .._gateway_client import GatewayClient

if TYPE_CHECKING:
    from ...tool import BackendBase


class WinGatewayClient(GatewayClient):
    """GatewayClient that talks HTTP directly (no exec-shell shim).

    Every request goes through :meth:`exec_request`, which dispatches via
    :attr:`_http_client` rather than an exec-shell shim.
    """

    def __init__(
        self,
        *,
        backend: "BackendBase",
        base_url: str,
        auth_token: str | None = None,
        instance_nonce: str | None = None,
        gateway_log_path: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Build a direct-HTTP gateway client.

        Args:
            backend: Remote Windows backend, retained for inherited
                diagnostics and facade state.
            base_url: Gateway base URL, e.g.
                ``http://127.0.0.1:12345`` (a locally-forwarded port).
            auth_token: Bearer token for gateway auth.
            instance_nonce: Nonce expected from ``/health``.
            gateway_log_path: Path for diagnostics on failure.
            timeout: Per-request timeout.
        """
        super().__init__(
            backend=backend,
            gateway_port=0,
            timeout=timeout,
            auth_token=auth_token,
            instance_nonce=instance_nonce,
            gateway_log_path=gateway_log_path,
        )
        self._base_url = base_url.rstrip("/")
        self._http_timeout = timeout
        self._http_client: httpx.AsyncClient | None = None
        self._next_base_url: str | None = None
        self._next_token: str | None = None
        self._recovery_callback: Callable[[], Awaitable[None]] | None = None
        self._recovering = False

    @property
    def base_url(self) -> str:
        """The current gateway base URL."""
        return self._base_url

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._next_base_url is not None:
            token = self._next_token or self.auth_token
            self._http_client = httpx.AsyncClient(
                base_url=self._next_base_url or self._base_url,
                timeout=self._http_timeout,
                headers=(
                    {"Authorization": f"Bearer {token}"} if token else {}
                ),
            )
            self._base_url = self._next_base_url or self._base_url
            self.auth_token = token or self.auth_token
            self._next_base_url = None
            self._next_token = None
        return self._http_client

    async def exec_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
        include_auth: bool = True,
    ) -> tuple[int, bytes]:
        """Send a direct HTTP request (overrides the shim transport)."""
        path = f"{path}?{urlencode(params)}" if params else path
        try:
            try:
                client = self._get_client()
                headers: dict[str, str] = {}
                if not include_auth:
                    headers["Authorization"] = ""
                resp = await client.request(
                    method,
                    path,
                    json=body,
                    headers=headers,
                )
            except httpx.TransportError:
                if (
                    self._recovery_callback is None
                    or self._recovering
                    or path == "/health"
                ):
                    raise
                self._recovering = True
                try:
                    await self._recovery_callback()
                finally:
                    self._recovering = False
                if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                    # Delivery is ambiguous after a transport failure.
                    # Replaying a mutation could duplicate a tool call.
                    raise
                resp = await self._get_client().request(
                    method,
                    path,
                    json=body,
                    headers=headers,
                )
            return resp.status_code, resp.content
        except Exception as error:
            if path != "/health":
                await self._diagnose_failure(method, path, error)
            raise

    def set_recovery_callback(
        self,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """Set the workspace callback used after transport failures."""
        self._recovery_callback = callback

    async def aclose(self) -> None:
        """Close the HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def reconnect(
        self,
        new_base_url: str,
        new_auth_token: str | None = None,
        new_nonce: str | None = None,
    ) -> None:
        """Rebuild the HTTP client for a new target URL / token.

        Used during reconnect after a supervisor restart that may
        assign a new gateway port or rotate the auth token.
        """
        await self.aclose()
        self._next_base_url = new_base_url.rstrip("/")
        self._next_token = new_auth_token
        if new_nonce is not None:
            self.instance_nonce = new_nonce
