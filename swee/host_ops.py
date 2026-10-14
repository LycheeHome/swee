"""Client for lychee-admin's per-client host-operations socket (A-OPS-1).

swee reaches the game server's systemd unit only through this: an HTTP API
served on a Unix socket that lychee-admin creates for swee alone. A POST to
/v1/units/<unit>/actions/<action> starts the action (202 with its state); a
GET of the same path reads the state back:
{"unit", "action", "state", "startedAt", "finishedAt", "logTail"}, with
state one of idle, running, succeeded, failed. Errors are JSON
{"error": "..."}.

Every failure becomes a HostOpsError carrying the one sentence swee shows in
Discord (`sentence`) and the raw text for the log (`detail`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx

from swee.config import HOST_OPS_SOCKET, HOST_OPS_UNIT

log = logging.getLogger("swee.host_ops")

REQUEST_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 2.0
# A GET right after a --no-block start can still return the previous run's
# terminal state. A terminal state counts as this run's only if it started no
# earlier than our POST, less this much clock slack.
STALE_SLACK = timedelta(seconds=5)
TERMINAL_STATES = frozenset({"succeeded", "failed"})

SENTENCE_BUSY = "an action is already running on the server"
SENTENCE_NOT_ALLOWED = "swee is not allowed to do that"
SENTENCE_COULD_NOT_START = "lychee-admin could not start it"
SENTENCE_UNAVAILABLE = "host control is unavailable right now"

# Indirections the tests patch, so they never touch the asyncio or time
# modules every other caller shares.
_sleep = asyncio.sleep
_now = time.monotonic


class HostOpsError(Exception):
    def __init__(self, sentence: str, detail: str = ""):
        super().__init__(sentence)
        self.sentence = sentence
        self.detail = detail


@dataclass
class ActionState:
    state: str
    started_at: datetime | None
    finished_at: datetime | None
    log_tail: str


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_state(payload: object) -> ActionState:
    if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
        raise HostOpsError(SENTENCE_COULD_NOT_START, f"unexpected response: {payload!r}")
    log_tail = payload.get("logTail")
    return ActionState(
        state=payload["state"],
        started_at=_parse_time(payload.get("startedAt")),
        finished_at=_parse_time(payload.get("finishedAt")),
        log_tail=log_tail if isinstance(log_tail, str) else "",
    )


def _refusal(response: httpx.Response) -> HostOpsError:
    detail = f"HTTP {response.status_code}: {response.text}"
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    message = error if isinstance(error, str) and error else response.text.strip()
    status = response.status_code
    if status == 409:
        return HostOpsError(SENTENCE_BUSY, detail)
    if status in (403, 404):
        return HostOpsError(SENTENCE_NOT_ALLOWED, detail)
    # 502 is lychee-admin saying the action could not be started. Anything
    # else unexpected (a 400, a 500) is reported the same way, with its text.
    sentence = f"{SENTENCE_COULD_NOT_START}: {message}" if message else SENTENCE_COULD_NOT_START
    return HostOpsError(sentence, detail)


def _last_line(log_tail: str) -> str | None:
    for line in reversed(log_tail.splitlines()):
        if line.strip():
            return line
    return None


class HostOps:
    def __init__(
        self,
        socket_path: str = HOST_OPS_SOCKET,
        unit: str = HOST_OPS_UNIT,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.socket_path = socket_path
        self.unit = unit
        self._transport = transport

    def _path(self, action: str) -> str:
        return f"/v1/units/{self.unit}/actions/{action}"

    async def _request(self, method: str, action: str) -> ActionState:
        # A fresh client (and, by default, a fresh transport) per request: the
        # module-level instance is created at import, before any event loop,
        # and must not hold a connection pool bound to one.
        owned = self._transport is None
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path) if owned else self._transport
        client = httpx.AsyncClient(transport=transport, base_url="http://swee", timeout=REQUEST_TIMEOUT_S)
        try:
            response = await client.request(method, self._path(action))
        except (httpx.TransportError, OSError) as error:
            # Connection refused, socket missing, timeout: lychee-admin is not
            # answering, usually for seconds during its own deploy.
            raise HostOpsError(SENTENCE_UNAVAILABLE, f"{type(error).__name__}: {error}") from error
        finally:
            if owned:
                await client.aclose()
        if not response.is_success:
            raise _refusal(response)
        try:
            payload = response.json()
        except ValueError as error:
            raise HostOpsError(SENTENCE_COULD_NOT_START, f"unparsable response: {response.text}") from error
        return _parse_state(payload)

    async def start(self, action: str) -> ActionState:
        return await self._request("POST", action)

    async def state(self, action: str) -> ActionState:
        return await self._request("GET", action)

    async def ping(self) -> bool:
        try:
            await self.state("restart")
        except HostOpsError as error:
            log.info("host-ops socket did not answer: %s (%s)", error.sentence, error.detail)
            return False
        return True

    async def wait(
        self,
        action: str,
        posted_at: datetime,
        timeout_s: float,
        on_line: Callable[[str], Awaitable[None]] | None = None,
    ) -> ActionState:
        """Poll until this run's terminal state. Unreachable polls are
        tolerated (lychee-admin restarts on every deploy while the action
        itself keeps running); any other refusal is raised. A terminal state
        with no startedAt cannot be shown to be this run's, so it is waited
        past like a stale one."""
        deadline = _now() + timeout_s
        earliest = posted_at - STALE_SLACK
        last_reported: str | None = None
        while True:
            try:
                current = await self.state(action)
            except HostOpsError as error:
                if error.sentence != SENTENCE_UNAVAILABLE:
                    raise
                log.info("host-ops poll for %s unreachable: %s", action, error.detail)
            else:
                fresh = current.started_at is not None and current.started_at >= earliest
                if current.state in TERMINAL_STATES and not fresh:
                    pass  # the previous run's result
                else:
                    line = _last_line(current.log_tail)
                    if on_line is not None and line is not None and line != last_reported:
                        last_reported = line
                        await on_line(line)
                    if current.state in TERMINAL_STATES:
                        return current
            if _now() >= deadline:
                raise HostOpsError(SENTENCE_UNAVAILABLE, f"no result for {action} after {timeout_s:.0f}s")
            await _sleep(POLL_INTERVAL_S)


host_ops = HostOps()
