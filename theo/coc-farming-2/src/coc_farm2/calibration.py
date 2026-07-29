"""Browser-based point and rectangle selection for calibration."""

from __future__ import annotations

import json
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from typing import Any

from PIL import Image

from coc_farm2.models import AppBounds, PixelProbe, Point, Rect
from coc_farm2.pixels import build_stable_probe, sample_median_rgb

BrowserOpen = Callable[[str], object]
PathLike = str | Path


class CalibrationError(ValueError):
    """A selected pixel or region cannot form a reliable marker."""


def recommended_home_point(app_bounds: AppBounds) -> Point:
    """Screen-fixed point inside the bottom-left Attack button region."""
    return Point(
        x=app_bounds.left + round(app_bounds.width * 0.039),
        y=app_bounds.top + round(app_bounds.height * 0.81),
    )


def selection_page(*, natural_width: int, natural_height: int, mode: str) -> str:
    if mode == "point":
        instruction = "Click the exact pixel to use for this probe."
        script = _point_script()
    elif mode == "rect":
        instruction = "Click two opposite corners of the OCR crop region."
        script = _rect_script()
    else:
        raise ValueError(f"unknown calibration mode {mode!r}")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Calibration</title>
  <style>
    body {{ margin: 0; padding: 1rem; background: #16181d; color: #f5f7fa;
            font: 16px system-ui, sans-serif; }}
    canvas {{ display: block; max-width: 100%; height: auto; cursor: crosshair;
              outline: 1px solid #697386; }}
  </style>
</head>
<body>
  <p id="status">{instruction}</p>
  <canvas id="screenshot" width="{natural_width}" height="{natural_height}"></canvas>
  <script>
{script}
  </script>
</body>
</html>
"""


def _point_script() -> str:
    return """
    const canvas = document.getElementById("screenshot");
    const context = canvas.getContext("2d");
    const status = document.getElementById("status");
    const image = new Image();
    image.src = "screenshot.png";
    image.addEventListener("load", () => {
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      context.drawImage(image, 0, 0);
    });
    canvas.addEventListener("click", async (event) => {
      if (canvas.dataset.selected === "true") return;
      const rect = canvas.getBoundingClientRect();
      const x = Math.min(canvas.width - 1, Math.max(
        0, Math.floor((event.clientX - rect.left) * canvas.width / rect.width)
      ));
      const y = Math.min(canvas.height - 1, Math.max(
        0, Math.floor((event.clientY - rect.top) * canvas.height / rect.height)
      ));
      canvas.dataset.selected = "true";
      const response = await fetch("select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "point", x, y })
      });
      status.textContent = response.ok
        ? `Selected (${x}, ${y}). You can close this tab.`
        : "Selection failed. Return to the terminal and try again.";
    });
"""


def _rect_script() -> str:
    return """
    const canvas = document.getElementById("screenshot");
    const context = canvas.getContext("2d");
    const status = document.getElementById("status");
    const image = new Image();
    let corners = [];
    image.src = "screenshot.png";
    image.addEventListener("load", () => {
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      context.drawImage(image, 0, 0);
    });
    canvas.addEventListener("click", async (event) => {
      if (canvas.dataset.selected === "true") return;
      const rect = canvas.getBoundingClientRect();
      const x = Math.min(canvas.width - 1, Math.max(
        0, Math.floor((event.clientX - rect.left) * canvas.width / rect.width)
      ));
      const y = Math.min(canvas.height - 1, Math.max(
        0, Math.floor((event.clientY - rect.top) * canvas.height / rect.height)
      ));
      corners.push({ x, y });
      context.fillStyle = "#00e5ff";
      context.fillRect(x - 2, y - 2, 5, 5);
      if (corners.length === 1) {
        status.textContent = `Corner 1 (${x}, ${y}). Click the opposite corner.`;
        return;
      }
      canvas.dataset.selected = "true";
      const response = await fetch("select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: "rect",
          x1: corners[0].x, y1: corners[0].y,
          x2: corners[1].x, y2: corners[1].y
        })
      });
      status.textContent = response.ok
        ? "Region selected. You can close this tab."
        : "Selection failed. Return to the terminal and try again.";
    });
"""


def select_point(
    screenshot: Image.Image,
    *,
    open_browser: BrowserOpen = webbrowser.open,
    timeout_s: float = 120.0,
) -> Point:
    payload = _serve_selection(
        screenshot,
        mode="point",
        open_browser=open_browser,
        timeout_s=timeout_s,
    )
    return Point(x=int(payload["x"]), y=int(payload["y"]))


def select_rect(
    screenshot: Image.Image,
    *,
    open_browser: BrowserOpen = webbrowser.open,
    timeout_s: float = 120.0,
) -> Rect:
    payload = _serve_selection(
        screenshot,
        mode="rect",
        open_browser=open_browser,
        timeout_s=timeout_s,
    )
    x1, y1 = int(payload["x1"]), int(payload["y1"])
    x2, y2 = int(payload["x2"]), int(payload["y2"])
    return Rect(
        left=min(x1, x2),
        top=min(y1, y2),
        right=max(x1, x2) + 1,
        bottom=max(y1, y2) + 1,
    )


def _serve_selection(
    screenshot: Image.Image,
    *,
    mode: str,
    open_browser: BrowserOpen,
    timeout_s: float,
) -> dict[str, Any]:
    if screenshot.mode != "RGB":
        screenshot = screenshot.convert("RGB")
    png = BytesIO()
    screenshot.save(png, format="PNG")
    png_bytes = png.getvalue()
    html = selection_page(
        natural_width=screenshot.width,
        natural_height=screenshot.height,
        mode=mode,
    ).encode("utf-8")

    selected: dict[str, Any] = {}
    done = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/", "/index.html"}:
                self._send(200, "text/html; charset=utf-8", html)
            elif self.path == "/screenshot.png":
                self._send(200, "image/png", png_bytes)
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/select":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_error(400)
                return
            selected.clear()
            selected.update(payload)
            done.set()
            self._send(200, "application/json", b'{"ok":true}')

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host_raw, port = server.server_address[:2]
    host = host_raw if isinstance(host_raw, str) else str(host_raw)
    url = f"http://{host}:{port}/"
    try:
        open_browser(url)
        if not done.wait(timeout_s):
            raise CalibrationError(
                f"calibration timed out after {timeout_s:.0f}s — open {url} and select"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    if not selected:
        raise CalibrationError("no calibration selection received")
    return selected


def sample_probe_from_live(
    name: str,
    x: int,
    y: int,
    grab: Callable[[], Image.Image],
    *,
    frames: int = 3,
    radius: int = 2,
    tolerance: int = 24,
    sleeper: Callable[[float], None] | None = None,
) -> PixelProbe:
    import time

    sleep = sleeper or time.sleep
    samples = []
    for index in range(frames):
        image = grab()
        samples.append(sample_median_rgb(image, x, y, radius))
        if index + 1 < frames:
            sleep(0.2)
    try:
        return build_stable_probe(
            name,
            x,
            y,
            samples,
            radius=radius,
            tolerance=tolerance,
            sample_count=frames,
        )
    except ValueError as error:
        raise CalibrationError(str(error)) from error
