from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from aiohttp import web
from meshagent.api import RoomClient, WebSocketClientProtocol, websocket_room_url


LOGGER = logging.getLogger("task_queue_dashboard")
DEFAULT_QUEUE_NAME = f"scheduled-task-demo-{uuid4().hex[:8]}"
TASK_COUNT = int(os.getenv("TASK_QUEUE_TASK_COUNT", "6"))
TASK_INTERVAL_SECONDS = float(os.getenv("TASK_QUEUE_INTERVAL_SECONDS", "20"))
QUEUE_NAME = os.getenv("TASK_QUEUE_NAME", DEFAULT_QUEUE_NAME)
PORT = int(os.getenv("PORT", "8000"))


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Task Queue Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f4f6f8;
      color: #152033;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: #f4f6f8;
    }
    header {
      background: #ffffff;
      border-bottom: 1px solid #d8dee9;
    }
    .shell {
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
      padding: 24px 0;
    }
    h1 {
      margin: 0 0 12px;
      font-size: 2rem;
      line-height: 1.1;
      letter-spacing: 0;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: #4f5f75;
      font-size: 0.95rem;
    }
    .meta span {
      border: 1px solid #d8dee9;
      border-radius: 999px;
      padding: 6px 10px;
      background: #ffffff;
    }
    main {
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
      padding: 24px 0 36px;
      display: grid;
      gap: 22px;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
    }
    .metric {
      background: #ffffff;
      border: 1px solid #d8dee9;
      border-radius: 8px;
      padding: 14px;
      min-height: 92px;
    }
    .metric .label {
      margin: 0 0 8px;
      color: #5f6f85;
      font-size: 0.86rem;
      font-weight: 650;
    }
    .metric .value {
      margin: 0;
      color: #152033;
      font-size: 2rem;
      font-weight: 750;
      line-height: 1;
    }
    section {
      display: grid;
      gap: 10px;
    }
    h2 {
      margin: 0;
      font-size: 1.05rem;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .table-wrap {
      width: 100%;
      overflow-x: auto;
      background: #ffffff;
      border: 1px solid #d8dee9;
      border-radius: 8px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 720px;
    }
    th, td {
      padding: 12px 14px;
      border-bottom: 1px solid #e6ebf2;
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }
    th {
      color: #5f6f85;
      font-size: 0.8rem;
      text-transform: uppercase;
      font-weight: 700;
    }
    td {
      color: #25324a;
      font-size: 0.94rem;
    }
    tr:last-child td { border-bottom: 0; }
    .status {
      display: inline-block;
      border-radius: 999px;
      padding: 4px 8px;
      background: #edf2f7;
      color: #25324a;
      font-size: 0.82rem;
      font-weight: 700;
    }
    .status.dequeued { background: #e8f8ef; color: #166534; }
    .status.enqueued { background: #eaf2ff; color: #174ea6; }
    .status.failed { background: #fff1f0; color: #9f1c13; }
    .events {
      background: #172033;
      border-radius: 8px;
      color: #ecf2ff;
      min-height: 180px;
      padding: 14px;
      overflow: auto;
      font: 0.9rem ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .events div { padding: 3px 0; }
    .empty { color: #97a6ba; }
    @media (max-width: 840px) {
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      h1 { font-size: 1.6rem; }
    }
  </style>
</head>
<body>
  <header>
    <div class="shell">
      <h1>Task Queue Dashboard</h1>
      <div class="meta">
        <span id="queue-name">Queue</span>
        <span id="mode">Mode</span>
        <span id="room-name">Room</span>
        <span id="updated-at">Updated</span>
      </div>
    </div>
  </header>
  <main>
    <div class="metrics">
      <div class="metric"><p class="label">Scheduled</p><p class="value" id="metric-scheduled">0</p></div>
      <div class="metric"><p class="label">Pending</p><p class="value" id="metric-pending">0</p></div>
      <div class="metric"><p class="label">Enqueued</p><p class="value" id="metric-enqueued">0</p></div>
      <div class="metric"><p class="label">Dequeued</p><p class="value" id="metric-dequeued">0</p></div>
      <div class="metric"><p class="label">On Queue</p><p class="value" id="metric-current">0</p></div>
    </div>
    <section>
      <h2>Scheduled Tasks</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Scheduled</th>
              <th>Status</th>
              <th>Enqueued</th>
              <th>Dequeued</th>
              <th>Text</th>
            </tr>
          </thead>
          <tbody id="task-rows"></tbody>
        </table>
      </div>
    </section>
    <section>
      <h2>Queue Log</h2>
      <div class="events" id="events"></div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const formatTime = (value) => value ? new Date(value).toLocaleTimeString() : "";
    const setText = (id, value) => { $(id).textContent = value; };

    function renderTasks(tasks) {
      const rows = tasks.map((task) => {
        const tr = document.createElement("tr");
        const fields = [
          task.id,
          formatTime(task.scheduled_at),
          task.status,
          formatTime(task.enqueued_at),
          formatTime(task.dequeued_at),
          task.text,
        ];
        fields.forEach((value, index) => {
          const td = document.createElement("td");
          if (index === 2) {
            const status = document.createElement("span");
            status.className = `status ${value}`;
            status.textContent = value;
            td.appendChild(status);
          } else {
            td.textContent = value || "";
          }
          tr.appendChild(td);
        });
        return tr;
      });
      $("task-rows").replaceChildren(...rows);
    }

    function renderEvents(events) {
      if (events.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "Waiting for queue events";
        $("events").replaceChildren(empty);
        return;
      }
      const lines = events.map((event) => {
        const line = document.createElement("div");
        line.textContent = `${formatTime(event.at)} ${event.message}`;
        return line;
      });
      $("events").replaceChildren(...lines);
    }

    async function refresh() {
      const response = await fetch("/api/dashboard", { cache: "no-store" });
      const data = await response.json();
      setText("queue-name", `Queue ${data.queue.name}`);
      setText("mode", data.queue.mode);
      setText("room-name", data.queue.room_name || "local");
      setText("updated-at", `Updated ${formatTime(data.updated_at)}`);
      setText("metric-scheduled", data.metrics.scheduled);
      setText("metric-pending", data.metrics.pending);
      setText("metric-enqueued", data.metrics.enqueued);
      setText("metric-dequeued", data.metrics.dequeued);
      setText("metric-current", data.metrics.current_on_queue);
      renderTasks(data.tasks);
      renderEvents(data.events);
    }

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


@dataclass(slots=True)
class ScheduledEntry:
    id: str
    text: str
    scheduled_at: datetime
    status: str = "scheduled"
    enqueued_at: datetime | None = None
    dequeued_at: datetime | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "scheduled_at": isoformat(self.scheduled_at),
            "status": self.status,
            "enqueued_at": isoformat(self.enqueued_at),
            "dequeued_at": isoformat(self.dequeued_at),
            "error": self.error,
        }


@dataclass(slots=True)
class DashboardState:
    queue_name: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list[ScheduledEntry] = field(default_factory=list)
    events: list[dict[str, str]] = field(default_factory=list)
    queue_mode: str = "starting"
    room_name: str | None = None
    enqueued_count: int = 0
    dequeued_count: int = 0
    current_on_queue: int = 0
    last_error: str | None = None

    async def set_tasks(self, tasks: list[ScheduledEntry]) -> None:
        async with self.lock:
            self.tasks = tasks

    async def set_connection(self, *, mode: str, room_name: str | None) -> None:
        async with self.lock:
            self.queue_mode = mode
            self.room_name = room_name
            self.last_error = None

    async def set_queue_size(self, size: int) -> None:
        async with self.lock:
            self.current_on_queue = size

    async def mark_enqueued(self, *, task_id: str) -> None:
        async with self.lock:
            now = now_utc()
            for task in self.tasks:
                if task.id == task_id:
                    task.status = "enqueued"
                    task.enqueued_at = now
                    task.error = None
                    break
            self.enqueued_count += 1
            self.events.append({"at": isoformat(now), "message": f"enqueued {task_id}"})
            self.events = self.events[-30:]

    async def mark_dequeued(
        self, *, task_id: str | None, text: str, queue_size: int | None
    ) -> None:
        async with self.lock:
            now = now_utc()
            if task_id is not None:
                for task in self.tasks:
                    if task.id == task_id:
                        task.status = "dequeued"
                        task.dequeued_at = now
                        task.error = None
                        break
            self.dequeued_count += 1
            if queue_size is not None:
                self.current_on_queue = queue_size
            else:
                self.current_on_queue = max(
                    0, self.enqueued_count - self.dequeued_count
                )
            self.events.append(
                {
                    "at": isoformat(now),
                    "message": f"dequeued {task_id or 'message'}: {text}",
                }
            )
            self.events = self.events[-30:]

    async def mark_failed(self, *, task_id: str, error: str) -> None:
        async with self.lock:
            now = now_utc()
            for task in self.tasks:
                if task.id == task_id:
                    task.status = "failed"
                    task.error = error
                    break
            self.last_error = error
            self.events.append(
                {"at": isoformat(now), "message": f"failed {task_id}: {error}"}
            )
            self.events = self.events[-30:]

    async def set_error(self, error: str) -> None:
        async with self.lock:
            now = now_utc()
            self.last_error = error
            self.events.append({"at": isoformat(now), "message": f"error: {error}"})
            self.events = self.events[-30:]

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            tasks = [task.to_json() for task in self.tasks]
            pending = sum(1 for task in self.tasks if task.status == "scheduled")
            return {
                "updated_at": isoformat(now_utc()),
                "queue": {
                    "name": self.queue_name,
                    "mode": self.queue_mode,
                    "room_name": self.room_name,
                    "last_error": self.last_error,
                },
                "metrics": {
                    "scheduled": len(self.tasks),
                    "pending": pending,
                    "enqueued": self.enqueued_count,
                    "dequeued": self.dequeued_count,
                    "current_on_queue": self.current_on_queue,
                },
                "tasks": tasks,
                "events": list(self.events),
            }


class LocalQueueAdapter:
    def __init__(self, *, queue_name: str) -> None:
        self.queue_name = queue_name
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def open(self) -> None:
        return None

    async def send(self, message: dict[str, Any]) -> None:
        await self._queue.put(message)

    async def receive(self) -> dict[str, Any]:
        return await self._queue.get()

    async def size(self) -> int:
        return self._queue.qsize()


class RoomQueueAdapter:
    def __init__(self, *, room: RoomClient, queue_name: str) -> None:
        self.room = room
        self.queue_name = queue_name

    async def open(self) -> None:
        await self.room.queues.open(name=self.queue_name)

    async def send(self, message: dict[str, Any]) -> None:
        await self.room.queues.send(name=self.queue_name, message=message, create=True)

    async def receive(self) -> dict[str, Any] | str | None:
        return await self.room.queues.receive(
            name=self.queue_name,
            create=True,
            wait=True,
        )

    async def size(self) -> int:
        queues = await self.room.queues.list()
        for queue in queues:
            if queue.name == self.queue_name:
                return queue.size
        return 0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_schedule() -> list[ScheduledEntry]:
    start = now_utc()
    return [
        ScheduledEntry(
            id=f"task-{index}",
            text=f"scheduled text item {index}",
            scheduled_at=start + timedelta(seconds=(index - 1) * TASK_INTERVAL_SECONDS),
        )
        for index in range(1, TASK_COUNT + 1)
    ]


def normalize_message(message: dict[str, Any] | str | None) -> tuple[str | None, str]:
    if isinstance(message, dict):
        text = str(message.get("text") or json.dumps(message, sort_keys=True))
        task_id = message.get("task_id")
        return (str(task_id) if task_id is not None else None), text
    if message is None:
        return None, ""
    return None, str(message)


async def schedule_demo_tasks(
    *,
    state: DashboardState,
    adapter: LocalQueueAdapter | RoomQueueAdapter,
    stop: asyncio.Event,
) -> None:
    tasks = build_schedule()
    await state.set_tasks(tasks)
    for task in tasks:
        delay = max(0.0, (task.scheduled_at - now_utc()).total_seconds())
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except TimeoutError:
            pass

        payload = {
            "type": "scheduled_text_item",
            "task_id": task.id,
            "text": task.text,
            "scheduled_at": isoformat(task.scheduled_at),
        }
        try:
            await adapter.send(payload)
            await state.mark_enqueued(task_id=task.id)
            await state.set_queue_size(await adapter.size())
            LOGGER.info("Enqueued %s on %s: %s", task.id, adapter.queue_name, task.text)
        except Exception as exc:
            LOGGER.exception("Unable to enqueue %s", task.id)
            await state.mark_failed(task_id=task.id, error=str(exc))


async def listen_for_queue_items(
    *,
    state: DashboardState,
    adapter: LocalQueueAdapter | RoomQueueAdapter,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            message = await adapter.receive()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Queue listener error")
            await state.set_error(str(exc))
            await asyncio.sleep(1)
            continue

        task_id, text = normalize_message(message)
        if text == "":
            continue
        queue_size: int | None = None
        with contextlib.suppress(Exception):
            queue_size = await adapter.size()
        await state.mark_dequeued(task_id=task_id, text=text, queue_size=queue_size)
        LOGGER.info("Dequeued text item from %s: %s", adapter.queue_name, text)


async def refresh_queue_size(
    *,
    state: DashboardState,
    adapter: LocalQueueAdapter | RoomQueueAdapter,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await state.set_queue_size(await adapter.size())
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass


async def run_demo_with_adapter(
    *,
    state: DashboardState,
    adapter: LocalQueueAdapter | RoomQueueAdapter,
    stop: asyncio.Event,
) -> None:
    await adapter.open()
    await state.set_queue_size(await adapter.size())
    workers = [
        asyncio.create_task(
            schedule_demo_tasks(state=state, adapter=adapter, stop=stop)
        ),
        asyncio.create_task(
            listen_for_queue_items(state=state, adapter=adapter, stop=stop)
        ),
        asyncio.create_task(
            refresh_queue_size(state=state, adapter=adapter, stop=stop)
        ),
    ]
    try:
        await stop.wait()
    finally:
        for worker in workers:
            worker.cancel()
        for worker in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker


async def run_queue_demo(*, state: DashboardState, stop: asyncio.Event) -> None:
    try:
        room_name = os.getenv("MESHAGENT_ROOM")
        token = os.getenv("MESHAGENT_TOKEN")
        if not room_name or not token:
            LOGGER.info("MESHAGENT_ROOM or MESHAGENT_TOKEN not set; using local queue")
            await state.set_connection(mode="local-memory", room_name=None)
            adapter = LocalQueueAdapter(queue_name=state.queue_name)
            await run_demo_with_adapter(state=state, adapter=adapter, stop=stop)
            return

        protocol = WebSocketClientProtocol(
            url=websocket_room_url(room_name=room_name),
            token=token,
        )
        async with RoomClient(protocol_factory=protocol.create_factory()) as room:
            await state.set_connection(mode="meshagent-room", room_name=room.room_name)
            adapter = RoomQueueAdapter(room=room, queue_name=state.queue_name)
            await run_demo_with_adapter(state=state, adapter=adapter, stop=stop)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        LOGGER.exception("Task queue demo stopped")
        await state.set_error(str(exc))
        await stop.wait()


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def dashboard(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


async def api_dashboard(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    return web.json_response(await state.snapshot())


async def status(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    snapshot = await state.snapshot()
    return web.json_response(
        {
            "ready": True,
            "queue": snapshot["queue"],
            "metrics": snapshot["metrics"],
        }
    )


async def start_web_server(state: DashboardState) -> web.AppRunner:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/", dashboard)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/api/dashboard", api_dashboard)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    LOGGER.info("Dashboard available at http://127.0.0.1:%s/", PORT)
    return runner


async def open_local_dashboard() -> None:
    url = os.getenv("TASK_QUEUE_DASHBOARD_LOCAL_URL")
    if not url or os.getenv("TASK_QUEUE_DASHBOARD_OPEN_BROWSER", "1") == "0":
        return
    await asyncio.sleep(0.2)
    LOGGER.info("Opening dashboard at %s", url)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(webbrowser.open, url)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    state = DashboardState(queue_name=QUEUE_NAME)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    runner = await start_web_server(state)
    await open_local_dashboard()
    demo_task = asyncio.create_task(run_queue_demo(state=state, stop=stop))
    try:
        await stop.wait()
    finally:
        stop.set()
        demo_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await demo_task
        await runner.cleanup()
        print("Stopped task queue dashboard.")


if __name__ == "__main__":
    asyncio.run(main())
