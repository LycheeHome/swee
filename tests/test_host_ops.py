"""Tests for swee/host_ops.py, the client for lychee-admin's per-client
host-operations socket (A-OPS-1). All but the last run against httpx's
MockTransport; the last makes one real HTTP/1.1 request over a temporary Unix
socket, so the default transport (uds=...) is exercised end to end.
"""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("ADMIN_ROLE_ID", "1")
os.environ.setdefault("RELAY_CHANNEL_ID", "1")
os.environ.setdefault("STATS_CHANNEL_ID", "1")
os.environ.setdefault("ACTIVITY_CHANNEL_ID", "1")
os.environ.setdefault("ALERTS_CHANNEL_ID", "1")
os.environ.setdefault("ADMIN_CHANNEL_ID", "1")
os.environ.setdefault("COMMANDS_CHANNEL_ID", "1")
os.environ.setdefault("BOT_UPDATES_CHANNEL_ID", "1")
os.environ.setdefault("REST_HOST", "x")
os.environ.setdefault("REST_PORT", "1")
os.environ.setdefault("REST_USER", "x")
os.environ.setdefault("REST_PASSWORD", "x")
os.environ.setdefault("PALWORLD_SETTINGS_INI_PATH", "/tmp/x")

import httpx

import swee.host_ops as host_ops
from swee.host_ops import ActionState, HostOps, HostOpsError

UNAVAILABLE = "host control is unavailable right now"
POSTED_AT = datetime(2026, 10, 14, 17, 46, 9, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def body(state, started=None, finished=None, log_tail=""):
    return {
        "unit": "palworld-palchuds",
        "action": "restart",
        "state": state,
        "startedAt": iso(started) if started else None,
        "finishedAt": iso(finished) if finished else None,
        "logTail": log_tail,
    }


def sequence_transport(responses, seen=None):
    """A MockTransport answering each request with the next item of
    `responses`: an exception instance is raised, anything else is a
    (status, json) pair."""
    items = iter(responses)

    def handler(request):
        if seen is not None:
            seen.append(request)
        item = next(items)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


async def no_sleep(_seconds):
    return None


class HostOpsRequestTests(unittest.TestCase):
    def test_start_posts_and_returns_state(self):
        seen = []
        transport = sequence_transport(
            [(202, body("running", started=POSTED_AT, log_tail="starting"))], seen
        )
        client = HostOps(socket_path="/nonexistent", unit="palworld-palchuds", transport=transport)

        result = asyncio.run(client.start("restart"))

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].method, "POST")
        self.assertEqual(seen[0].url.path, "/v1/units/palworld-palchuds/actions/restart")
        self.assertEqual(
            result,
            ActionState(state="running", started_at=POSTED_AT, finished_at=None, log_tail="starting"),
        )

    def test_refusals_map_to_sentences(self):
        cases = {
            409: "an action is already running on the server",
            403: "swee is not allowed to do that",
            404: "swee is not allowed to do that",
            502: "lychee-admin could not start it: x",
        }
        for status, sentence in cases.items():
            for call in ("start", "state"):
                with self.subTest(status=status, call=call):
                    client = HostOps(transport=sequence_transport([(status, {"error": "x"})]))
                    with self.assertRaises(HostOpsError) as caught:
                        asyncio.run(getattr(client, call)("restart"))
                    self.assertEqual(caught.exception.sentence, sentence)
                    self.assertIn("x", caught.exception.detail)

    def test_unreachable_is_its_own_sentence(self):
        for error in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow")):
            with self.subTest(error=type(error).__name__):
                self.assertIsInstance(error, (httpx.ConnectError, httpx.TimeoutException))
                client = HostOps(transport=sequence_transport([error]))
                with self.assertRaises(HostOpsError) as caught:
                    asyncio.run(client.state("restart"))
                self.assertEqual(caught.exception.sentence, UNAVAILABLE)
                self.assertTrue(caught.exception.detail)

    def test_every_request_has_a_10s_timeout(self):
        seen = []
        transport = sequence_transport(
            [(202, body("running", started=POSTED_AT)), (200, body("idle"))], seen
        )
        client = HostOps(transport=transport)

        asyncio.run(client.start("update"))
        asyncio.run(client.state("update"))

        self.assertEqual(len(seen), 2)
        for request in seen:
            timeout = request.extensions["timeout"]
            self.assertEqual(set(timeout.values()), {10.0}, timeout)

    def test_ping_is_true_on_2xx_and_false_otherwise(self):
        self.assertTrue(asyncio.run(HostOps(transport=sequence_transport([(200, body("idle"))])).ping()))
        self.assertFalse(asyncio.run(HostOps(transport=sequence_transport([httpx.ConnectError("x")])).ping()))
        self.assertFalse(asyncio.run(HostOps(transport=sequence_transport([(403, {"error": "x"})])).ping()))

    def test_unparsable_times_are_none(self):
        payload = body("succeeded", log_tail="ok")
        payload["startedAt"] = "not a time"
        client = HostOps(transport=sequence_transport([(200, payload)]))
        self.assertIsNone(asyncio.run(client.state("restart")).started_at)


class HostOpsWaitTests(unittest.TestCase):
    def run_wait(self, client, *, timeout_s=120.0, on_line=None):
        with patch.object(host_ops, "_sleep", no_sleep):
            return asyncio.run(client.wait("restart", POSTED_AT, timeout_s, on_line=on_line))

    def test_wait_ignores_terminal_state_from_before_the_post(self):
        stale = POSTED_AT - timedelta(seconds=60)
        fresh = POSTED_AT + timedelta(seconds=1)
        client = HostOps(transport=sequence_transport([
            (200, body("succeeded", started=stale, finished=stale, log_tail="old run")),
            (200, body("running", started=fresh)),
            (200, body("succeeded", started=fresh, finished=fresh + timedelta(seconds=3), log_tail="new run")),
        ]))

        result = self.run_wait(client)

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.started_at, fresh)
        self.assertEqual(result.log_tail, "new run")

    def test_wait_accepts_a_start_within_the_clock_slack(self):
        slightly_early = POSTED_AT - timedelta(seconds=4)
        client = HostOps(transport=sequence_transport([
            (200, body("failed", started=slightly_early, finished=POSTED_AT, log_tail="boom")),
        ]))
        self.assertEqual(self.run_wait(client).state, "failed")

    def test_wait_survives_a_few_unreachable_polls(self):
        fresh = POSTED_AT + timedelta(seconds=1)
        client = HostOps(transport=sequence_transport([
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            (200, body("succeeded", started=fresh, finished=fresh)),
        ]))

        self.assertEqual(self.run_wait(client).state, "succeeded")

    def test_wait_does_not_swallow_a_refusal(self):
        client = HostOps(transport=sequence_transport([(403, {"error": "x"})]))
        with self.assertRaises(HostOpsError) as caught:
            self.run_wait(client)
        self.assertEqual(caught.exception.sentence, "swee is not allowed to do that")

    def test_wait_reports_each_new_log_line_once(self):
        fresh = POSTED_AT + timedelta(seconds=1)
        client = HostOps(transport=sequence_transport([
            (200, body("running", started=fresh, log_tail="a\nb")),
            (200, body("running", started=fresh, log_tail="a\nb\n")),
            (200, body("running", started=fresh, log_tail="a\nb\nc")),
            (200, body("succeeded", started=fresh, finished=fresh, log_tail="a\nb\nc")),
        ]))
        lines = []

        async def on_line(line):
            lines.append(line)

        self.run_wait(client, on_line=on_line)

        self.assertEqual(lines, ["b", "c"])

    def test_wait_gives_up_after_timeout(self):
        fresh = POSTED_AT + timedelta(seconds=1)
        client = HostOps(transport=sequence_transport(
            [(200, body("running", started=fresh))] * 10
        ))
        clock = iter([0.0, 50.0, 100.0, 150.0, 200.0, 250.0])

        with patch.object(host_ops, "_now", lambda: next(clock)):
            with self.assertRaises(HostOpsError) as caught:
                self.run_wait(client, timeout_s=120.0)

        self.assertEqual(caught.exception.sentence, UNAVAILABLE)


class HostOpsRealSocketTests(unittest.TestCase):
    def test_over_a_real_unix_socket(self):
        directory = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        socket_path = os.path.join(directory, "api.sock")
        requests = []

        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            requests.append(head.decode().split("\r\n", 1)[0])
            payload = json.dumps(body("idle")).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        async def scenario():
            server = await asyncio.start_unix_server(handle, path=socket_path)
            async with server:
                return await HostOps(socket_path=socket_path).state("restart")

        result = asyncio.run(scenario())

        self.assertEqual(result.state, "idle")
        self.assertEqual(requests, ["GET /v1/units/palworld-palchuds/actions/restart HTTP/1.1"])


if __name__ == "__main__":
    unittest.main()
