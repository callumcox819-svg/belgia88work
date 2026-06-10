"""Context guard: SMTP must run only inside ProxySMTPContext."""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Optional

_smtp_via_proxy: contextvars.ContextVar[bool] = contextvars.ContextVar("smtp_via_proxy", default=False)
_smtp_direct_allowed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "smtp_direct_allowed", default=False
)


def smtp_proxy_guard_enter() -> contextvars.Token[bool]:
    return _smtp_via_proxy.set(True)


def smtp_proxy_guard_exit(token: contextvars.Token[bool]) -> None:
    _smtp_via_proxy.reset(token)


@contextmanager
def allow_direct_smtp() -> Iterator[None]:
    """Разрешить прямой SMTP без SOCKS5 (только тест-маил)."""
    token = _smtp_direct_allowed.set(True)
    try:
        yield
    finally:
        _smtp_direct_allowed.reset(token)


def smtp_proxy_required_error() -> Optional[str]:
    if not _smtp_via_proxy.get() and not _smtp_direct_allowed.get():
        return "PROXY_ERROR|no_proxy_context|SMTP send without proxy is forbidden"
    return None
