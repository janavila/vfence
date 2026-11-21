import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import WebSocketDisconnect

from app.database import Database
from app.realtime import RealtimeHub, _CLIENT_QUEUE_CAPACITY
from app.serial_reader import GATEWAY_SERIAL_SCHEMA, SerialReader
from test_metric_summaries import FakeByteConnection, hdop_json, metric_rows, serial_line


class FakeWebSocket:
    def __init__(self, *, slow=False, slow_close=False, failure=None):
        self.accepted = asyncio.Event()
        self.send_started = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.allow_send = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.sent = []
        self.close_codes = []
        self.failure = failure
        if not slow:
            self.allow_send.set()
        if not slow_close:
            self.allow_close.set()

    async def accept(self):
        self.accepted.set()

    async def send_json(self, message):
        self.send_started.set()
        await self.allow_send.wait()
        if self.failure is not None:
            raise self.failure
        self.sent.append(message)

    async def receive_text(self):
        await self.disconnected.wait()
        raise WebSocketDisconnect(1000)

    async def close(self, code):
        self.close_codes.append(code)
        await self.allow_close.wait()
        self.disconnected.set()


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = RealtimeHub()
        self.sessions = []

    async def asyncTearDown(self):
        await asyncio.wait_for(self.hub.close(), 4)
        await asyncio.wait_for(asyncio.gather(*self.sessions), 4)

    async def connect(self, socket, initial=()):
        session = asyncio.create_task(self.hub.serve(socket, initial))
        self.sessions.append(session)
        await asyncio.wait_for(socket.accepted.wait(), 2)
        return session

    async def wait_for(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), 3)

    async def test_clients_get_initial_messages_and_updates_in_fifo_order(self):
        clients = [FakeWebSocket(), FakeWebSocket()]
        initial = ({"type": "gateway", "data": {}}, {"type": "fence", "data": {}})
        for client in clients:
            await self.connect(client, initial)
        updates = [
            {"type": "telemetry", "data": {"sequence": 2}},
            {"type": "gateway_event", "data": {"kind": "event"}},
            {"type": "fence_transfer", "data": {"state": "confirmed"}},
            {"type": "central", "data": {"status": "online"}},
        ]
        for update in updates:
            await self.hub.broadcast(update)
        await self.wait_for(lambda: all(len(client.sent) == 6 for client in clients))
        self.assertEqual([client.sent for client in clients], [[*initial, *updates]] * 2)
        self.assertEqual(self.hub.dropped_updates, {})

    async def test_slow_client_times_out_without_delaying_healthy_client(self):
        slow, healthy = FakeWebSocket(slow=True), FakeWebSocket()
        await self.connect(healthy)
        with patch("app.realtime._CLIENT_IO_TIMEOUT_SECONDS", 0.3), self.assertLogs("realtime", "WARNING") as logs:
            slow_session = await self.connect(slow)
            await self.hub.broadcast({"type": "telemetry", "data": {"sequence": 1}})
            await asyncio.wait_for(slow.send_started.wait(), 1)
            await self.hub.broadcast({"type": "gateway_event", "data": {}})
            await self.wait_for(lambda: len(healthy.sent) == 2)
            self.assertFalse(slow_session.done())
            await asyncio.wait_for(slow_session, 1)
            await self.hub.broadcast({"type": "fence", "data": {}})
            await self.wait_for(lambda: len(healthy.sent) == 3)
        self.assertEqual(slow.close_codes, [1013])
        self.assertEqual(self.hub.send_timeouts, 1)
        self.assertEqual(self.hub.dropped_updates, {"telemetry": 1, "gateway_event": 1})
        self.assertIn("websocket send timed out", "\n".join(logs.output))

    async def test_full_client_queue_disconnects_and_counts_discarded_update_types(self):
        socket = FakeWebSocket(slow=True)
        with patch("app.realtime._CLIENT_IO_TIMEOUT_SECONDS", 0.3), self.assertLogs("realtime", "WARNING") as logs:
            session = await self.connect(socket)
            await self.hub.broadcast({"type": "gateway", "data": {}})
            await asyncio.wait_for(socket.send_started.wait(), 1)
            for _ in range(_CLIENT_QUEUE_CAPACITY - 1):
                await self.hub.broadcast({"type": "telemetry", "data": {}})
            await self.hub.broadcast({"type": "gateway_event", "data": {}})
            await self.hub.broadcast({"type": "fence_transfer", "data": {}})
            self.assertEqual(self.hub.full_queues, 1)
            self.assertEqual(self.hub._connections[socket].queue.qsize(), 1)  # apenas o sinal de parada
            socket.allow_send.set()
            await asyncio.wait_for(session, 1)
        self.assertEqual(socket.close_codes, [1013])
        self.assertEqual(self.hub.dropped_updates, {
            "telemetry": _CLIENT_QUEUE_CAPACITY - 1, "gateway_event": 1, "fence_transfer": 1,
        })
        self.assertIn("33 updates discarded", "\n".join(logs.output))

    async def test_client_capacity_and_shutdown_during_blocked_send_and_close(self):
        socket = FakeWebSocket(slow=True, slow_close=True)
        with patch("app.realtime._MAX_CLIENTS", 1), patch("app.realtime._CLIENT_IO_TIMEOUT_SECONDS", 0.05), \
                self.assertLogs("realtime", "WARNING") as logs:
            session = await self.connect(socket, ({"type": "gateway", "data": {}},))
            await asyncio.wait_for(socket.send_started.wait(), 1)
            extra = FakeWebSocket()
            await self.hub.serve(extra, ())
            self.assertFalse(extra.accepted.is_set())
            self.assertEqual(extra.close_codes, [1013])
            await asyncio.wait_for(self.hub.close(), 1)
            await asyncio.wait_for(session, 1)
        self.assertEqual(socket.close_codes, [1001])
        self.assertEqual(self.hub.dropped_updates, {"gateway": 1})
        self.assertIn("websocket close timed out", "\n".join(logs.output))
        self.assertEqual(self.hub._connections, {})
        self.assertFalse(any(t.get_name().startswith("vfence-websocket-") for t in asyncio.all_tasks()))

    async def test_unexpected_send_failure_is_reported_and_does_not_stop_publication(self):
        broken = FakeWebSocket(failure=RuntimeError("simulated broken send"))
        healthy = FakeWebSocket()
        await self.connect(healthy)
        with self.assertLogs("realtime", "ERROR") as logs:
            session = await self.connect(broken)
            await self.hub.broadcast({"type": "gateway_event", "data": {}})
            await asyncio.wait_for(session, 1)
        await self.hub.broadcast({"type": "fence", "data": {}})
        await self.wait_for(lambda: len(healthy.sent) == 2)
        self.assertEqual(broken.close_codes, [1011])
        self.assertIn("simulated broken send", "\n".join(logs.output))

    async def test_serial_keeps_resolving_control_while_websocket_send_is_blocked(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Database(f"sqlite:///{Path(directory.name) / 'serial.db'}")
        database.initialize()
        reader = SerialReader(SimpleNamespace(
            serial_port="test", serial_baud=115200, reconnect_seconds=0,
            mock_serial=False, central_sync_enabled=False, max_line_length=512,
            metrics_installation_id="INSTALL-TEST", metrics_gateway_id="GATEWAY-TEST",
        ), database, self.hub)
        link = FakeByteConnection()
        slow, healthy = FakeWebSocket(slow=True), FakeWebSocket()
        await self.connect(healthy)
        with patch("serial.Serial", return_value=link), patch("app.realtime._CLIENT_IO_TIMEOUT_SECONDS", 0.7):
            slow_session = await self.connect(slow)
            task = asyncio.create_task(reader.run())
            command = None
            try:
                await asyncio.wait_for(slow.send_started.wait(), 2)
                command = asyncio.create_task(reader.send_gateway_command("GET_STATUS", timeout_seconds=0.5))
                await self.wait_for(lambda: bool(link.writes))
                request = json.loads(link.writes[0].removeprefix(b"\x00\n")[6:])
                reader._fence_request_id, reader._fence_collar_id, reader._fence_version = 94, 3, 7
                fence = reader._fence_result = asyncio.get_running_loop().create_future()
                link.feed(
                    serial_line(hdop_json()) +
                    b"V1|COL02|POS|-31|-54|SEGURO|9|0.90|7\n" +
                    serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "fence_transfer",
                                 "requestId": 94, "collarId": 3, "fenceVersion": 7, "state": "confirmed"}) +
                    serial_line({"schema": GATEWAY_SERIAL_SCHEMA, "kind": "command_response",
                                 "requestId": request["requestId"], "command": "GET_STATUS", "ok": True})
                )
                self.assertTrue((await command)["ok"])
                self.assertEqual(await asyncio.wait_for(fence, 0.5), (True, "collar_confirmed"))
                self.assertFalse(slow_session.done())
                await asyncio.wait_for(slow_session, 2)
                link.feed(b"V1|COL02|POS|-31|-54|FORA|9|0.90|8\n")
                await self.wait_for(lambda: [m["data"]["sequence"] for m in healthy.sent
                                              if m["type"] == "telemetry"] == [7, 8])
            finally:
                await reader.stop()
                await asyncio.wait_for(task, 4)
                if command is not None:
                    if not command.done():
                        command.cancel()
                    await asyncio.gather(command, return_exceptions=True)
        self.assertEqual([r["collar_id"] for r in metric_rows(database)], [1])
        self.assertEqual(database.get_collar("COL02")["zone"], "FORA")
        self.assertEqual(len(link.writes), 1)
