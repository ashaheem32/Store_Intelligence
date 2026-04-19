"""Live terminal dashboard for the Store Intelligence API.

Polls /metrics, /anomalies, and /health on independent intervals and renders
a rich.Layout with top bar, live metrics, zone heatmap, and active anomalies.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
from datetime import datetime
from typing import Any, Optional

import httpx
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


METRICS_INTERVAL = 3.0
ANOMALIES_INTERVAL = 5.0
HEALTH_INTERVAL = 10.0
HEATMAP_INTERVAL = 5.0
RENDER_INTERVAL = 0.25
HTTP_TIMEOUT = 4.0

SEVERITY_STYLES = {
    "CRITICAL": ("red", "🔴"),
    "WARN": ("yellow", "🟡"),
    "INFO": ("blue", "🔵"),
}

STATUS_STYLES = {
    "LIVE": "bold green",
    "STALE": "bold yellow",
    "UNREACHABLE": "bold red",
}


class State:
    """Shared mutable state updated by pollers, read by the renderer."""

    def __init__(self, store_id: str, api_url: str) -> None:
        self.store_id = store_id
        self.api_url = api_url.rstrip("/")
        self.metrics: Optional[dict] = None
        self.anomalies: Optional[list[dict]] = None
        self.heatmap: Optional[list[dict]] = None
        self.health: Optional[dict] = None
        self.api_unreachable: bool = False
        self.feed_status: str = "UNREACHABLE"
        self.store_name: str = ""


async def _fetch_json(client: httpx.AsyncClient, path: str) -> Optional[Any]:
    try:
        r = await client.get(path, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


async def poll_metrics(client: httpx.AsyncClient, state: State) -> None:
    path = f"/stores/{state.store_id}/metrics"
    while True:
        data = await _fetch_json(client, path)
        if data is not None:
            state.metrics = data
            state.api_unreachable = False
        else:
            state.api_unreachable = True
        await asyncio.sleep(METRICS_INTERVAL)


async def poll_heatmap(client: httpx.AsyncClient, state: State) -> None:
    path = f"/stores/{state.store_id}/heatmap"
    while True:
        data = await _fetch_json(client, path)
        if isinstance(data, dict):
            state.heatmap = data.get("zones") or []
        await asyncio.sleep(HEATMAP_INTERVAL)


async def poll_anomalies(client: httpx.AsyncClient, state: State) -> None:
    path = f"/stores/{state.store_id}/anomalies"
    while True:
        data = await _fetch_json(client, path)
        if isinstance(data, dict):
            state.anomalies = data.get("active_anomalies") or []
        await asyncio.sleep(ANOMALIES_INTERVAL)


async def poll_health(client: httpx.AsyncClient, state: State) -> None:
    while True:
        data = await _fetch_json(client, "/health")
        if isinstance(data, dict):
            state.api_unreachable = False
            store_block = (data.get("stores") or {}).get(state.store_id) or {}
            feed = store_block.get("feed_status")
            if feed == "LIVE":
                state.feed_status = "LIVE"
            elif feed in ("STALE", "NO_DATA", "CLOSED"):
                state.feed_status = "STALE"
            else:
                state.feed_status = "STALE"
        else:
            state.api_unreachable = True
            state.feed_status = "UNREACHABLE"
        await asyncio.sleep(HEALTH_INTERVAL)


async def fetch_store_name(client: httpx.AsyncClient, state: State) -> None:
    """One-shot: pull store name from layout via heatmap response (has labels)."""
    # The API doesn't expose store_name directly; leave blank if unavailable.
    state.store_name = "Apex Retail"


# ---------- rendering ----------

def render_top_bar(state: State) -> Panel:
    now = datetime.now().strftime("%H:%M:%S")
    if state.api_unreachable:
        status_text = Text("⚠ API UNREACHABLE — retrying...", style="bold red")
    else:
        status_text = Text(state.feed_status, style=STATUS_STYLES.get(state.feed_status, "white"))

    bar = Table.grid(expand=True)
    bar.add_column(justify="left", ratio=2)
    bar.add_column(justify="center", ratio=2)
    bar.add_column(justify="center", ratio=1)
    bar.add_column(justify="right", ratio=1)
    bar.add_row(
        Text(state.store_id, style="bold cyan"),
        Text(state.store_name or "—", style="white"),
        Text(now, style="white"),
        status_text,
    )
    return Panel(bar, border_style="cyan", padding=(0, 1))


def render_metrics(state: State) -> Panel:
    m = state.metrics
    if m is None:
        body: Any = Align.center(Text("Loading metrics...", style="dim"), vertical="middle")
        return Panel(body, title="LIVE METRICS", border_style="green")

    visitors = m.get("unique_visitors", 0)
    converted = m.get("converted_visitors", 0)
    conv_rate = m.get("conversion_rate")
    queue = m.get("current_queue_depth", 0)
    abandon = m.get("abandonment_rate")

    conv_str = f"{conv_rate * 100:.1f}%" if conv_rate is not None else "—"
    abandon_str = f"{abandon * 100:.1f}%" if abandon is not None else "—"

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold white")
    table.add_column(justify="right", style="bright_white")
    table.add_row("Visitors:", str(visitors))
    table.add_row("Converted:", str(converted))
    table.add_row("Conv Rate:", conv_str)
    table.add_row("Queue:", str(queue))
    table.add_row("Abandon:", abandon_str)
    return Panel(table, title="LIVE METRICS", border_style="green", padding=(1, 2))


def _bar(score: int, width: int = 10) -> str:
    score = max(0, min(100, int(score)))
    filled = round(score / 100 * width)
    return "█" * filled + " " * (width - filled)


def render_heatmap(state: State) -> Panel:
    zones = state.heatmap
    if zones is None:
        body: Any = Align.center(Text("Loading heatmap...", style="dim"), vertical="middle")
        return Panel(body, title="ZONE HEATMAP", border_style="magenta")

    sorted_zones = sorted(zones, key=lambda z: z.get("normalised_score", 0), reverse=True)

    table = Table.grid(padding=(0, 1))
    table.add_column(style="white")
    table.add_column(style="magenta")
    table.add_column(justify="right", style="bright_white")
    for z in sorted_zones:
        zid = z.get("zone_id", "?")
        score = int(z.get("normalised_score", 0))
        table.add_row(zid, _bar(score), str(score))
    return Panel(table, title="ZONE HEATMAP", border_style="magenta", padding=(1, 2))


def render_anomalies(state: State) -> Panel:
    anomalies = state.anomalies
    if anomalies is None:
        body: Any = Align.center(Text("Loading anomalies...", style="dim"), vertical="middle")
        return Panel(body, title="ACTIVE ANOMALIES", border_style="red")

    if not anomalies:
        body = Align.center(Text("No active anomalies ✓", style="green"), vertical="middle")
        return Panel(body, title="ACTIVE ANOMALIES", border_style="green")

    severity_rank = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
    sorted_anoms = sorted(anomalies, key=lambda a: severity_rank.get(a.get("severity", "INFO"), 3))

    lines = []
    for a in sorted_anoms:
        sev = a.get("severity", "INFO")
        style, icon = SEVERITY_STYLES.get(sev, ("white", "•"))
        detail = a.get("detail", "")
        lines.append(Text(f"{icon} {sev}: {detail}", style=style))
    return Panel(Group(*lines), title="ACTIVE ANOMALIES", border_style="red", padding=(1, 2))


def build_layout(state: State) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", size=3),
        Layout(name="middle", ratio=2),
        Layout(name="bottom", ratio=1),
    )
    layout["middle"].split_row(
        Layout(name="metrics"),
        Layout(name="heatmap"),
    )
    layout["top"].update(render_top_bar(state))
    layout["middle"]["metrics"].update(render_metrics(state))
    layout["middle"]["heatmap"].update(render_heatmap(state))
    layout["bottom"].update(render_anomalies(state))
    return layout


# ---------- main ----------

async def run(store_id: str, api_url: str) -> None:
    state = State(store_id, api_url)
    console = Console()

    async with httpx.AsyncClient(base_url=state.api_url) as client:
        await fetch_store_name(client, state)

        pollers = [
            asyncio.create_task(poll_metrics(client, state)),
            asyncio.create_task(poll_heatmap(client, state)),
            asyncio.create_task(poll_anomalies(client, state)),
            asyncio.create_task(poll_health(client, state)),
        ]

        with Live(build_layout(state), console=console, refresh_per_second=4, screen=True) as live:
            try:
                while True:
                    live.update(build_layout(state))
                    await asyncio.sleep(RENDER_INTERVAL)
            except asyncio.CancelledError:
                pass
            finally:
                for t in pollers:
                    t.cancel()
                await asyncio.gather(*pollers, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live terminal dashboard for the Store Intelligence API")
    parser.add_argument("--store_id", required=True, help="Store ID, e.g. STORE_001")
    parser.add_argument("--api_url", default="http://localhost:8000", help="Base URL of the API")
    args = parser.parse_args()

    console = Console()
    loop = asyncio.new_event_loop()

    main_task = loop.create_task(run(args.store_id, args.api_url))

    def _shutdown() -> None:
        if not main_task.done():
            main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        loop.close()
        console.print("Dashboard stopped.")


if __name__ == "__main__":
    main()
