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
import re
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
# Every action swee may run on the unit. preflight() reads them all, because
# lychee-admin refuses any action while another one is running.
ACTIONS = ("start", "stop", "restart", "update")
# A terminal state with no usable startedAt (the action failed before its main
# process ran: ExecStartPre failed, or start-limit-hit) is taken as this run's
# once this long has passed since the wait began, well past the --no-block
# window in which the previous run's result can still be read.
UNDATED_GRACE_S = 10.0

SENTENCE_BUSY = "an action is already running on the server"
SENTENCE_NOT_ALLOWED = "swee is not allowed to do that"
SENTENCE_COULD_NOT_START = "lychee-admin could not start it"
SENTENCE_UNAVAILABLE = "host control is unavailable right now"

# Indirections the tests patch, so they never touch the asyncio or time
# modules every other caller shares.
_sleep = asyncio.sleep
_now = time.monotonic


class HostOpsError(Exception):
    def __init__(self, sentence: str, detail: str = "", status: int | None = None):
        super().__init__(sentence)
        self.sentence = sentence
        self.detail = detail
        # The HTTP status for a refusal; None when nothing answered.
        self.status = status


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
        return HostOpsError(SENTENCE_BUSY, detail, status)
    if status in (403, 404):
        return HostOpsError(SENTENCE_NOT_ALLOWED, detail, status)
    # 502 is lychee-admin saying the action could not be started. Anything
    # else unexpected (a 400, a 500) is reported the same way, with its text.
    sentence = f"{SENTENCE_COULD_NOT_START}: {message}" if message else SENTENCE_COULD_NOT_START
    return HostOpsError(sentence, detail, status)


def _last_line(log_tail: str) -> str | None:
    """The last complete, non-blank line of a log tail.

    steamcmd writes progress in pieces and redraws with a bare \\r, so a poll
    can catch an unfinished last segment ("[  0"). lychee-admin keeps a trailing
    newline exactly when the log file ends with one, so a tail that does not
    end in \\n or \\r has an unfinished final segment, which is dropped.
    """
    segments = re.split(r"\r\n|\n|\r", log_tail)
    if not log_tail.endswith(("\n", "\r")):
        segments = segments[:-1]
    for segment in reversed(segments):
        if segment.strip():
            return segment.strip()
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

    async def preflight(self) -> None:
        """Check, before swee warns any player, that a POST would be
        accepted: GET every action's state. A refusal propagates with its own
        sentence (403/404, unreachable, 5xx); a `running` action raises the
        409 sentence. Returns None when nothing is in the way.

        A check, not a reservation: an action can still start between this
        and the POST, which then meets its own 409."""
        for action in ACTIONS:
            current = await self.state(action)
            if current.state == "running":
                raise HostOpsError(SENTENCE_BUSY, f"{action} is running on {self.unit}", 409)

    async def wait(
        self,
        action: str,
        posted_at: datetime,
        timeout_s: float,
        on_line: Callable[[str], Awaitable[None]] | None = None,
    ) -> ActionState:
        """Poll until this run's terminal state.

        Tolerated, and polled past until the deadline: an unreachable socket
        (lychee-admin restarts on every deploy while the action itself keeps
        running) and a 5xx (lychee-admin could not read systemd's state this
        once). A 403/404/409 is raised.

        A terminal state is this run's when any of these holds: its startedAt
        is no earlier than posted_at less the slack; a `running` state has
        been seen since the POST (the POST succeeded only because nothing was
        running, so the next terminal state is ours); or UNDATED_GRACE_S has
        passed since the wait began, past the window in which the previous
        run's result can still be read.

        An exception from on_line is logged and the wait continues: failing
        to edit a progress message must not abandon a running action."""
        began = _now()
        deadline = began + timeout_s
        earliest = posted_at - STALE_SLACK
        seen_running = False
        last_reported: str | None = None
        while True:
            current: ActionState | None = None
            try:
                current = await self.state(action)
            except HostOpsError as error:
                transient = error.sentence == SENTENCE_UNAVAILABLE or (
                    error.status is not None and error.status >= 500
                )
                if not transient:
                    raise
                log.info("host-ops poll for %s did not answer: %s", action, error.detail)
            now = _now()
            if current is not None:
                if current.state == "running":
                    seen_running = True
                terminal = current.state in TERMINAL_STATES
                ours = (
                    (current.started_at is not None and current.started_at >= earliest)
                    or seen_running
                    or now - began >= UNDATED_GRACE_S
                )
                if not terminal or ours:
                    line = _last_line(current.log_tail)
                    if on_line is not None and line is not None and line != last_reported:
                        last_reported = line
                        try:
                            await on_line(line)
                        except Exception:
                            log.warning("host-ops on_line failed for %s; still polling", action, exc_info=True)
                    if terminal:
                        return current
            if now >= deadline:
                raise HostOpsError(SENTENCE_UNAVAILABLE, f"no result for {action} after {timeout_s:.0f}s")
            await _sleep(POLL_INTERVAL_S)


host_ops = HostOps()
