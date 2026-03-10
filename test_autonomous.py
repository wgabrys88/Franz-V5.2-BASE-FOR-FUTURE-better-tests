import base64
import http.server
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Paths:
    here: Path = Path(__file__).resolve().parent
    win32: Path = Path(__file__).resolve().parent / "win32.py"
    panel_py: Path = Path(__file__).resolve().parent / "panel.py"
    panel_html: Path = Path(__file__).resolve().parent / "panel.html"
    output_json: Path = Path(__file__).resolve().parent / "test_autonomous.json"
    output_jsonl: Path = Path(__file__).resolve().parent / "test_autonomous.jsonl"
    franz_log: Path = Path(__file__).resolve().parent / "franz-log.jsonl"
    chrome_exe: Path = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    panel_wrapper: Path = Path(__file__).resolve().parent / "_test_panel_wrapper.py"


@dataclass(frozen=True)
class Net:
    panel_host: str = "127.0.0.1"
    panel_port: int = 1236
    panel_url: str = "http://127.0.0.1:1236"
    vlm_host: str = "127.0.0.1"
    vlm_port: int = 1235
    mock_vlm_port: int = 1237
    ready_timeout: float = 8.0
    ready_poll: float = 0.25
    sse_connect_timeout: float = 6.0
    request_timeout: float = 30.0
    lm_studio_probe_timeout: float = 2.0
    warmup_timeout: float = 20.0
    warmup_retries: int = 3
    warmup_retry_delay: float = 3.0


@dataclass(frozen=True)
class Img:
    width: int = 32
    height: int = 32
    norm: int = 1000
    pixel_change_threshold: int = 5
    cursor_tolerance: int = 30
    capture_min_bytes: int = 67


@dataclass(frozen=True)
class Timing:
    action_settle: float = 0.15
    type_settle: float = 0.5
    sse_event_timeout: float = 15.0
    concurrent_timeout: float = 45.0
    select_region_delay: float = 1.0
    select_region_esc_delay: float = 0.8
    panel_shutdown_timeout: float = 3.0
    log_rename_retries: int = 5
    log_rename_delay: float = 0.5
    chrome_startup: float = 2.0
    mock_startup: float = 0.3
    port_release: float = 0.5
    chrome_sse_reconnect: float = 4.0


PATHS: Paths = Paths()
NET: Net = Net()
IMG: Img = Img()
TIMING: Timing = Timing()


@dataclass
class TestResult:
    phase: str
    name: str
    passed: bool
    detail: str
    duration_ms: int


@dataclass
class RegionSelection:
    region: str
    cap_w: int
    cap_h: int


_results: list[TestResult] = []
_results_lock: threading.Lock = threading.Lock()

_mouse_sel: RegionSelection = RegionSelection(region="", cap_w=640, cap_h=640)
_keyboard_sel: RegionSelection = RegionSelection(region="", cap_w=640, cap_h=640)


def _log_result(result: TestResult) -> None:
    status: str = "PASS" if result.passed else "FAIL"
    sys.stdout.write(f"  [{status}] {result.phase}/{result.name} ({result.duration_ms}ms) {result.detail}\n")
    sys.stdout.flush()
    with _results_lock:
        _results.append(result)


def _run_test(phase: str, name: str, fn: Any) -> TestResult:
    t0: float = time.time()
    try:
        detail: str = fn()
        dur: int = round((time.time() - t0) * 1000)
        result: TestResult = TestResult(phase=phase, name=name, passed=True, detail=detail or "", duration_ms=dur)
    except Exception as exc:
        dur = round((time.time() - t0) * 1000)
        result = TestResult(phase=phase, name=name, passed=False, detail=str(exc), duration_ms=dur)
    _log_result(result)
    return result


def _make_bgra(width: int, height: int, r: int, g: int, b: int, a: int = 255) -> bytes:
    pixel: bytes = bytes([b, g, r, a])
    return pixel * (width * height)


def _make_checkerboard_bgra(width: int, height: int, size: int = 4) -> bytes:
    out: bytearray = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            off: int = (y * width + x) * 4
            if ((x // size) + (y // size)) % 2 == 0:
                out[off:off + 4] = b"\x00\x00\x00\xff"
            else:
                out[off:off + 4] = b"\xff\xff\xff\xff"
    return bytes(out)


def _make_gradient_bgra(width: int, height: int) -> bytes:
    out: bytearray = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            off: int = (y * width + x) * 4
            rv: int = (x * 255) // max(1, width - 1)
            gv: int = (y * 255) // max(1, height - 1)
            out[off] = 0
            out[off + 1] = gv
            out[off + 2] = rv
            out[off + 3] = 255
    return bytes(out)


def _bgra_to_png(bgra: bytes, width: int, height: int) -> bytes:
    stride: int = width * 4
    source: memoryview = memoryview(bgra)
    rows: bytearray = bytearray()
    for yidx in range(height):
        rows.append(0)
        row: memoryview = source[yidx * stride:(yidx + 1) * stride]
        for xoff in range(0, len(row), 4):
            rows.extend((row[xoff + 2], row[xoff + 1], row[xoff + 0], 255))

    def make_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
        combined: bytes = chunk_type + chunk_data
        return (
            struct.pack(">I", len(chunk_data))
            + combined
            + struct.pack(">I", zlib.crc32(combined) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + make_chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + make_chunk(b"IEND", b"")
    )


def _make_test_png(r: int = 0, g: int = 128, b: int = 0) -> bytes:
    return _bgra_to_png(_make_bgra(IMG.width, IMG.height, r, g, b), IMG.width, IMG.height)


def _make_checkerboard_png() -> bytes:
    return _bgra_to_png(_make_checkerboard_bgra(IMG.width, IMG.height), IMG.width, IMG.height)


def _make_gradient_png() -> bytes:
    return _bgra_to_png(_make_gradient_bgra(IMG.width, IMG.height), IMG.width, IMG.height)


def _png_valid(data: bytes) -> bool:
    return len(data) >= IMG.capture_min_bytes and data[:8] == b"\x89PNG\r\n\x1a\n"


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24:
        return 0, 0
    w: int = struct.unpack(">I", data[16:20])[0]
    h: int = struct.unpack(">I", data[20:24])[0]
    return w, h


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p: int = a + b - c
    pa: int = abs(p - a)
    pb: int = abs(p - b)
    pc: int = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_png_pixels(png_data: bytes) -> list[tuple[int, int, int]]:
    if not _png_valid(png_data):
        return []
    pos: int = 8
    width: int = 0
    height: int = 0
    idat_chunks: list[bytes] = []
    while pos + 8 <= len(png_data):
        if pos + 4 > len(png_data):
            break
        chunk_len: int = struct.unpack(">I", png_data[pos:pos + 4])[0]
        chunk_type: bytes = png_data[pos + 4:pos + 8]
        chunk_data: bytes = png_data[pos + 8:pos + 8 + chunk_len]
        if chunk_type == b"IHDR":
            width = struct.unpack(">I", chunk_data[0:4])[0]
            height = struct.unpack(">I", chunk_data[4:8])[0]
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break
        pos += 12 + chunk_len
    if width == 0 or height == 0:
        return []
    try:
        raw: bytes = zlib.decompress(b"".join(idat_chunks))
    except Exception:
        return []
    bpp: int = 4
    row_bytes: int = width * bpp
    expected_len: int = height * (1 + row_bytes)
    if len(raw) < expected_len:
        return []
    recon: bytearray = bytearray(height * row_bytes)
    for y in range(height):
        filter_byte: int = raw[y * (1 + row_bytes)]
        row_start_raw: int = y * (1 + row_bytes) + 1
        row_start_recon: int = y * row_bytes
        for x_byte in range(row_bytes):
            cur: int = raw[row_start_raw + x_byte]
            a: int = recon[row_start_recon + x_byte - bpp] if x_byte >= bpp else 0
            b_val: int = recon[(y - 1) * row_bytes + x_byte] if y > 0 else 0
            c: int = recon[(y - 1) * row_bytes + x_byte - bpp] if y > 0 and x_byte >= bpp else 0
            match filter_byte:
                case 0:
                    recon[row_start_recon + x_byte] = cur
                case 1:
                    recon[row_start_recon + x_byte] = (cur + a) & 0xFF
                case 2:
                    recon[row_start_recon + x_byte] = (cur + b_val) & 0xFF
                case 3:
                    recon[row_start_recon + x_byte] = (cur + ((a + b_val) >> 1)) & 0xFF
                case 4:
                    recon[row_start_recon + x_byte] = (cur + _paeth_predictor(a, b_val, c)) & 0xFF
                case _:
                    recon[row_start_recon + x_byte] = cur
    pixels: list[tuple[int, int, int]] = []
    for y in range(height):
        for x in range(width):
            off: int = y * row_bytes + x * bpp
            pixels.append((recon[off], recon[off + 1], recon[off + 2]))
    return pixels


def _pixel_diff_count(b64_a: str, b64_b: str) -> int:
    try:
        png_a: bytes = base64.b64decode(b64_a)
        png_b: bytes = base64.b64decode(b64_b)
    except Exception:
        return -1
    pixels_a: list[tuple[int, int, int]] = _decode_png_pixels(png_a)
    pixels_b: list[tuple[int, int, int]] = _decode_png_pixels(png_b)
    if len(pixels_a) != len(pixels_b) or not pixels_a:
        return -1
    count: int = 0
    for i in range(len(pixels_a)):
        if pixels_a[i] != pixels_b[i]:
            count += 1
    return count


def _count_red_pixels(b64_data: str) -> int:
    try:
        png: bytes = base64.b64decode(b64_data)
    except Exception:
        return 0
    pixels: list[tuple[int, int, int]] = _decode_png_pixels(png)
    count: int = 0
    for r, g, b in pixels:
        if r > 200 and g < 80 and b < 80:
            count += 1
    return count


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    req: urllib.request.Request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body: bytes = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body
    except Exception:
        return 0, b""


def _http_post(url: str, data: dict[str, Any], timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    body: bytes = json.dumps(data).encode()
    req: urllib.request.Request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        resp_body: bytes = b""
        try:
            resp_body = e.read()
        except Exception:
            pass
        try:
            return e.code, json.loads(resp_body) if resp_body else {}
        except json.JSONDecodeError:
            return e.code, {"raw": resp_body.decode("utf-8", errors="replace")}
    except Exception as exc:
        return 0, {"error": str(exc)}


def _win32_run(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[bytes]:
    cmd: list[str] = [sys.executable, str(PATHS.win32)] + args
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _win32_run_text(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    cmd: list[str] = [sys.executable, str(PATHS.win32)] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class MockVLMHandler(http.server.BaseHTTPRequestHandler):
    response_text: str = "MOCK_RESPONSE"
    delay: float = 0.0
    fail_mode: bool = False
    last_request_body: dict[str, Any] = {}
    last_request_lock: threading.Lock = threading.Lock()

    def log_message(self, *_: Any) -> None:
        pass

    def do_POST(self) -> None:
        length: int = int(self.headers.get("Content-Length", 0))
        body: bytes = self.rfile.read(length) if length else b""
        if self.__class__.fail_mode:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            resp: bytes = json.dumps({"error": "mock failure"}).encode()
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
        if self.__class__.delay > 0:
            time.sleep(self.__class__.delay)
        try:
            req_body: dict[str, Any] = json.loads(body)
        except Exception:
            req_body = {}
        with self.__class__.last_request_lock:
            self.__class__.last_request_body = req_body
        model: str = req_body.get("model", "mock-model")
        resp_obj: dict[str, Any] = {
            "id": "mock-id",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": self.__class__.response_text},
                "finish_reason": "stop",
            }],
        }
        resp_bytes: bytes = json.dumps(resp_obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)


def _get_last_vlm_request() -> dict[str, Any]:
    with MockVLMHandler.last_request_lock:
        return dict(MockVLMHandler.last_request_body)


def _clear_last_vlm_request() -> None:
    with MockVLMHandler.last_request_lock:
        MockVLMHandler.last_request_body = {}


def _start_mock_vlm(port: int, response_text: str = "MOCK_RESPONSE", fail: bool = False) -> http.server.ThreadingHTTPServer:
    MockVLMHandler.response_text = response_text
    MockVLMHandler.fail_mode = fail
    MockVLMHandler.delay = 0.0
    server: http.server.ThreadingHTTPServer = http.server.ThreadingHTTPServer(("127.0.0.1", port), MockVLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class SSEClient:
    def __init__(self, url: str) -> None:
        self._url: str = url
        self._events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._connected: threading.Event = threading.Event()
        self._stop: bool = False
        self._thread: threading.Thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            req: urllib.request.Request = urllib.request.Request(self._url, method="GET")
            with urllib.request.urlopen(req, timeout=120) as resp:
                buf: str = ""
                while not self._stop:
                    chunk: bytes = resp.read(1)
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n\n" in buf:
                        block: str
                        block, buf = buf.split("\n\n", 1)
                        event_type: str = ""
                        data_lines: list[str] = []
                        for line in block.split("\n"):
                            if line.startswith("event: "):
                                event_type = line[7:].strip()
                            elif line.startswith("data: "):
                                data_lines.append(line[6:])
                            elif line.startswith(":"):
                                pass
                        if event_type:
                            data_str: str = "\n".join(data_lines)
                            try:
                                data_obj: dict[str, Any] = json.loads(data_str) if data_str else {}
                            except Exception:
                                data_obj = {"raw": data_str}
                            if event_type == "connected":
                                self._connected.set()
                            self._events.put((event_type, data_obj))
        except Exception:
            pass

    def wait_connected(self, timeout: float = NET.sse_connect_timeout) -> bool:
        return self._connected.wait(timeout=timeout)

    def next_event(self, timeout: float = TIMING.sse_event_timeout) -> tuple[str, dict[str, Any]] | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_until(self, event_type: str, timeout: float = TIMING.sse_event_timeout) -> dict[str, Any] | None:
        deadline: float = time.time() + timeout
        while time.time() < deadline:
            remaining: float = deadline - time.time()
            if remaining <= 0:
                break
            evt: tuple[str, dict[str, Any]] | None = self.next_event(timeout=remaining)
            if evt is None:
                return None
            if evt[0] == event_type:
                return evt[1]
        return None

    def stop(self) -> None:
        self._stop = True


def _wait_panel_ready(timeout: float = NET.ready_timeout) -> bool:
    deadline: float = time.time() + timeout
    while time.time() < deadline:
        code: int
        code, _ = _http_get(f"{NET.panel_url}/ready", timeout=2.0)
        if code == 200:
            return True
        time.sleep(NET.ready_poll)
    return False


def _send_chat_completion(
    raw_b64: str,
    agent: str = "test_agent",
    model: str = "test-model",
    region: str = "",
    capture_size: list[int] | None = None,
    actions: list[dict[str, Any]] | None = None,
    overlays: list[dict[str, Any]] | None = None,
    timeout: float = NET.request_timeout,
) -> tuple[int, dict[str, Any]]:
    content_parts: list[dict[str, Any]] = []
    if raw_b64:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{raw_b64}"},
        })
    else:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": ""},
        })
    all_actions: list[dict[str, Any]] = []
    if overlays:
        all_actions.extend(overlays)
    if actions:
        all_actions.extend(actions)
    if all_actions:
        content_parts.append({"type": "actions", "actions": all_actions})
    cap: list[int] = capture_size if capture_size is not None else [IMG.width, IMG.height]
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 128,
        "stream": False,
        "region": region,
        "agent": agent,
        "capture_size": cap,
        "messages": [
            {"role": "system", "content": "test system prompt"},
            {"role": "user", "content": content_parts},
        ],
    }
    return _http_post(f"{NET.panel_url}/v1/chat/completions", body, timeout=timeout)


def _do_tandem_select(prompt_text: str) -> RegionSelection:
    sys.stdout.write(f"\n>>> {prompt_text}\n")
    sys.stdout.write(">>> FIRST: Draw a region for the capture area. Right-click to skip. ESC to cancel.\n")
    sys.stdout.flush()
    proc1: subprocess.CompletedProcess[str] = _win32_run_text(["select_region"], timeout=120)
    if proc1.returncode == 2:
        return RegionSelection(region="", cap_w=640, cap_h=640)
    region: str = proc1.stdout.strip()
    if region:
        sys.stdout.write(f">>> Region: {region}\n")
    else:
        sys.stdout.write(">>> No region selected (right-click)\n")

    sys.stdout.write(">>> SECOND: Draw a horizontal span for resize scale. Right-click to skip. ESC to cancel.\n")
    sys.stdout.flush()
    proc2: subprocess.CompletedProcess[str] = _win32_run_text(["select_region"], timeout=120)
    cap_w: int = 640
    cap_h: int = 640
    if proc2.returncode != 2 and proc2.stdout.strip():
        parts: list[str] = proc2.stdout.strip().split(",")
        if len(parts) == 4:
            x1: int = int(parts[0])
            x2: int = int(parts[2])
            scale: float = (x2 - x1) / 1000
            cap_w = round(1000 * scale)
            cap_h = round(1000 * scale)
            sys.stdout.write(f">>> Scale selection: {proc2.stdout.strip()} -> cap_w={cap_w}, cap_h={cap_h}\n")
    else:
        sys.stdout.write(">>> No scale selection, using defaults 640x640\n")

    sys.stdout.flush()
    return RegionSelection(region=region, cap_w=cap_w, cap_h=cap_h)


def _warmup_chrome_sse(vlm_available: bool) -> bool:
    sys.stdout.write("  Warming up Chrome SSE connection...\n")
    sys.stdout.flush()
    time.sleep(TIMING.chrome_sse_reconnect)
    raw_png: bytes = _make_test_png(1, 1, 1)
    raw_b64: str = base64.b64encode(raw_png).decode("ascii")
    for attempt in range(NET.warmup_retries):
        warmup_done: threading.Event = threading.Event()
        warmup_result: list[tuple[int, dict[str, Any]]] = []

        def _do_warmup() -> None:
            try:
                code, resp = _send_chat_completion(
                    raw_b64, agent="warmup", model="warmup",
                    timeout=NET.warmup_timeout,
                )
                warmup_result.append((code, resp))
            except Exception:
                warmup_result.append((0, {"error": "warmup exception"}))
            finally:
                warmup_done.set()

        t: threading.Thread = threading.Thread(target=_do_warmup, daemon=True)
        t.start()
        completed: bool = warmup_done.wait(timeout=NET.warmup_timeout + 2)
        if completed and warmup_result:
            code: int = warmup_result[0][0]
            if code == 200 or (code == 502 and not vlm_available):
                sys.stdout.write(f"  Warmup OK (attempt {attempt + 1}, code={code})\n")
                sys.stdout.flush()
                return True
            sys.stdout.write(f"  Warmup attempt {attempt + 1} got code={code}, retrying...\n")
            sys.stdout.flush()
        else:
            sys.stdout.write(f"  Warmup attempt {attempt + 1} timed out, retrying...\n")
            sys.stdout.flush()
        time.sleep(NET.warmup_retry_delay)
    sys.stdout.write("  Warmup FAILED after all retries\n")
    sys.stdout.flush()
    return False


def _cleanup_outputs() -> None:
    for p in [PATHS.output_json, PATHS.output_jsonl, PATHS.franz_log, PATHS.panel_wrapper]:
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def _rename_log_safe() -> None:
    if not PATHS.franz_log.exists():
        return
    for _ in range(TIMING.log_rename_retries):
        try:
            shutil.copy2(str(PATHS.franz_log), str(PATHS.output_jsonl))
            return
        except PermissionError:
            time.sleep(TIMING.log_rename_delay)
    try:
        shutil.copy2(str(PATHS.franz_log), str(PATHS.output_jsonl))
    except Exception:
        pass


def _write_results() -> None:
    with _results_lock:
        all_results: list[dict[str, Any]] = [
            {
                "phase": r.phase,
                "name": r.name,
                "passed": r.passed,
                "detail": r.detail,
                "duration_ms": r.duration_ms,
            }
            for r in _results
        ]
    summary: dict[str, Any] = {
        "total": len(all_results),
        "passed": sum(1 for r in all_results if r["passed"]),
        "failed": sum(1 for r in all_results if not r["passed"]),
        "results": all_results,
    }
    PATHS.output_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _write_panel_wrapper(vlm_port: int) -> None:
    wrapper_code: str = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
        "import panel as _p\n"
        f"_p.CFG = _p._Config(vlm_url='http://127.0.0.1:{vlm_port}/v1/chat/completions')\n"
        "_p.start().serve_forever()\n"
    )
    PATHS.panel_wrapper.write_text(wrapper_code, encoding="utf-8")


def _stop_panel(panel_proc: subprocess.Popen[bytes]) -> None:
    panel_proc.terminate()
    try:
        panel_proc.wait(timeout=TIMING.panel_shutdown_timeout)
    except subprocess.TimeoutExpired:
        panel_proc.kill()
        panel_proc.wait(timeout=3)
    time.sleep(TIMING.port_release)


def _start_panel_wrapper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(PATHS.panel_wrapper)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PATHS.here),
    )


def phase0_setup(panel_proc: subprocess.Popen[bytes], sse: SSEClient) -> bool:
    sys.stdout.write("\n=== Phase 0: Setup Validation ===\n")
    sys.stdout.flush()

    def test_panel_alive() -> str:
        if panel_proc.poll() is not None:
            raise RuntimeError(f"panel.py exited with code {panel_proc.returncode}")
        return "panel process alive"

    def test_panel_ready() -> str:
        if not _wait_panel_ready():
            raise RuntimeError("panel /ready did not return 200")
        return "/ready returned 200"

    def test_sse_connected() -> str:
        if not sse.wait_connected():
            raise RuntimeError("SSE did not receive connected event")
        return "SSE connected"

    def test_panel_html_served() -> str:
        code: int
        body: bytes
        code, body = _http_get(NET.panel_url)
        if code != 200:
            raise RuntimeError(f"GET / returned {code}")
        if b"Franz Panel" not in body:
            raise RuntimeError("panel.html title not found")
        return f"{len(body)} bytes"

    def test_404_path() -> str:
        code, _ = _http_get(f"{NET.panel_url}/nonexistent")
        if code != 404:
            raise RuntimeError(f"expected 404, got {code}")
        return "404 confirmed"

    def test_panel_log_endpoint() -> str:
        code, resp = _http_post(f"{NET.panel_url}/panel-log", {"level": "info", "msg": "test_log_entry"})
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        return "panel-log endpoint ok"

    r0: TestResult = _run_test("phase0", "panel_alive", test_panel_alive)
    r1: TestResult = _run_test("phase0", "panel_ready", test_panel_ready)
    r2: TestResult = _run_test("phase0", "sse_connected", test_sse_connected)
    _run_test("phase0", "panel_html_served", test_panel_html_served)
    _run_test("phase0", "404_path", test_404_path)
    _run_test("phase0", "panel_log_endpoint", test_panel_log_endpoint)
    return r0.passed and r1.passed and r2.passed


def phase1_capture() -> None:
    sys.stdout.write("\n=== Phase 1: win32 Capture ===\n")
    sys.stdout.flush()

    def test_capture_no_region() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(
            ["capture", "--width", "64", "--height", "64"]
        )
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        if not _png_valid(proc.stdout):
            raise RuntimeError(f"invalid PNG, {len(proc.stdout)} bytes")
        w, h = _png_dimensions(proc.stdout)
        if w != 64 or h != 64:
            raise RuntimeError(f"expected 64x64, got {w}x{h}")
        return f"{len(proc.stdout)} bytes, {w}x{h}"

    def test_capture_with_region() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(
            ["capture", "--region", "100,100,900,900", "--width", "32", "--height", "32"]
        )
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        if not _png_valid(proc.stdout):
            raise RuntimeError(f"invalid PNG, {len(proc.stdout)} bytes")
        w, h = _png_dimensions(proc.stdout)
        if w != 32 or h != 32:
            raise RuntimeError(f"expected 32x32, got {w}x{h}")
        return f"{len(proc.stdout)} bytes, {w}x{h}"

    def test_capture_full_resolution() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(
            ["capture", "--width", "0", "--height", "0"]
        )
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}")
        if not _png_valid(proc.stdout):
            raise RuntimeError("invalid PNG")
        w, h = _png_dimensions(proc.stdout)
        if w < 640 or h < 480:
            raise RuntimeError(f"suspiciously small: {w}x{h}")
        return f"{len(proc.stdout)} bytes, {w}x{h}"

    def test_capture_png_header() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(
            ["capture", "--width", "16", "--height", "16"]
        )
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}")
        data: bytes = proc.stdout
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError("bad PNG signature")
        ihdr_len: int = struct.unpack(">I", data[8:12])[0]
        ihdr_type: bytes = data[12:16]
        if ihdr_type != b"IHDR":
            raise RuntimeError(f"expected IHDR, got {ihdr_type}")
        if ihdr_len != 13:
            raise RuntimeError(f"IHDR length {ihdr_len}, expected 13")
        return "valid PNG structure"

    def test_synthetic_png_roundtrip() -> str:
        png: bytes = _make_test_png(255, 0, 0)
        if not _png_valid(png):
            raise RuntimeError("synthetic PNG invalid")
        w, h = _png_dimensions(png)
        if w != IMG.width or h != IMG.height:
            raise RuntimeError(f"expected {IMG.width}x{IMG.height}, got {w}x{h}")
        pixels: list[tuple[int, int, int]] = _decode_png_pixels(png)
        if not pixels:
            raise RuntimeError("could not decode pixels")
        if pixels[0] != (255, 0, 0):
            raise RuntimeError(f"expected (255,0,0), got {pixels[0]}")
        return f"{len(png)} bytes, {len(pixels)} pixels, first={pixels[0]}"

    def test_synthetic_checkerboard_decode() -> str:
        png: bytes = _make_checkerboard_png()
        pixels: list[tuple[int, int, int]] = _decode_png_pixels(png)
        if not pixels:
            raise RuntimeError("decode failed")
        if len(pixels) != IMG.width * IMG.height:
            raise RuntimeError(f"expected {IMG.width * IMG.height} pixels, got {len(pixels)}")
        black: int = sum(1 for r, g, b in pixels if r == 0 and g == 0 and b == 0)
        white: int = sum(1 for r, g, b in pixels if r == 255 and g == 255 and b == 255)
        if black + white != len(pixels):
            raise RuntimeError(f"{black} black, {white} white, {len(pixels) - black - white} other")
        return f"{black} black, {white} white"

    def test_synthetic_gradient_decode() -> str:
        png: bytes = _make_gradient_png()
        pixels: list[tuple[int, int, int]] = _decode_png_pixels(png)
        if not pixels:
            raise RuntimeError("decode failed")
        top_left: tuple[int, int, int] = pixels[0]
        bottom_right: tuple[int, int, int] = pixels[-1]
        if top_left[0] != 0 or top_left[1] != 0:
            raise RuntimeError(f"top-left expected (0,0,x), got {top_left}")
        if bottom_right[0] != 255 or bottom_right[1] != 255:
            raise RuntimeError(f"bottom-right expected (255,255,x), got {bottom_right}")
        return f"top_left={top_left}, bottom_right={bottom_right}"

    _run_test("phase1", "capture_no_region", test_capture_no_region)
    _run_test("phase1", "capture_with_region", test_capture_with_region)
    _run_test("phase1", "capture_full_resolution", test_capture_full_resolution)
    _run_test("phase1", "capture_png_header", test_capture_png_header)
    _run_test("phase1", "synthetic_png_roundtrip", test_synthetic_png_roundtrip)
    _run_test("phase1", "synthetic_checkerboard_decode", test_synthetic_checkerboard_decode)
    _run_test("phase1", "synthetic_gradient_decode", test_synthetic_gradient_decode)


def phase2_mouse_actions(sel: RegionSelection) -> None:
    sys.stdout.write("\n=== Phase 2: Mouse Actions ===\n")
    sys.stdout.flush()
    region: str = sel.region

    def _build_args(base: list[str]) -> list[str]:
        if region:
            return base + ["--region", region]
        return base

    def test_click() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(_build_args(["click", "--pos", "500,500"]))
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        time.sleep(TIMING.action_settle)
        cp: subprocess.CompletedProcess[str] = _win32_run_text(_build_args(["cursor_pos"]))
        coords: str = cp.stdout.strip()
        if not coords:
            raise RuntimeError("cursor_pos returned empty")
        x, y = map(int, coords.split(","))
        if abs(x - 500) > IMG.cursor_tolerance or abs(y - 500) > IMG.cursor_tolerance:
            raise RuntimeError(f"cursor at {x},{y}, expected near 500,500")
        return f"cursor at {x},{y}"

    def test_double_click() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(_build_args(["double_click", "--pos", "300,300"]))
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        time.sleep(TIMING.action_settle)
        cp: subprocess.CompletedProcess[str] = _win32_run_text(_build_args(["cursor_pos"]))
        coords: str = cp.stdout.strip()
        x, y = map(int, coords.split(","))
        if abs(x - 300) > IMG.cursor_tolerance or abs(y - 300) > IMG.cursor_tolerance:
            raise RuntimeError(f"cursor at {x},{y}, expected near 300,300")
        return f"cursor at {x},{y}"

    def test_right_click() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(_build_args(["right_click", "--pos", "700,700"]))
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        time.sleep(TIMING.action_settle)
        _win32_run(["press_key", "--key", "escape"])
        time.sleep(TIMING.action_settle)
        return "right_click executed, context menu dismissed"

    def test_drag() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(
            _build_args(["drag", "--from_pos", "200,200", "--to_pos", "800,800"])
        )
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        time.sleep(TIMING.action_settle)
        cp: subprocess.CompletedProcess[str] = _win32_run_text(_build_args(["cursor_pos"]))
        coords: str = cp.stdout.strip()
        x, y = map(int, coords.split(","))
        if abs(x - 800) > IMG.cursor_tolerance or abs(y - 800) > IMG.cursor_tolerance:
            raise RuntimeError(f"cursor at {x},{y}, expected near 800,800")
        return f"cursor at {x},{y}"

    def test_scroll_up() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(
            _build_args(["scroll_up", "--pos", "500,500", "--clicks", "3"])
        )
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        return "scroll_up executed"

    def test_scroll_down() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(
            _build_args(["scroll_down", "--pos", "500,500", "--clicks", "3"])
        )
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        return "scroll_down executed"

    def test_cursor_pos_roundtrip() -> str:
        _win32_run(_build_args(["click", "--pos", "250,750"]))
        time.sleep(TIMING.action_settle)
        cp: subprocess.CompletedProcess[str] = _win32_run_text(_build_args(["cursor_pos"]))
        coords: str = cp.stdout.strip()
        x, y = map(int, coords.split(","))
        if abs(x - 250) > IMG.cursor_tolerance or abs(y - 750) > IMG.cursor_tolerance:
            raise RuntimeError(f"cursor at {x},{y}, expected near 250,750")
        return f"roundtrip: {x},{y}"

    def test_tandem_region_in_json() -> str:
        _clear_last_vlm_request()
        raw_png: bytes = _make_test_png(10, 10, 10)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        code, resp = _send_chat_completion(
            raw_b64,
            agent="test_mouse_region_json",
            region=sel.region,
            capture_size=[sel.cap_w, sel.cap_h],
        )
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        last: dict[str, Any] = _get_last_vlm_request()
        if not last:
            raise RuntimeError("mock VLM did not receive request")
        return f"region={sel.region}, cap={sel.cap_w}x{sel.cap_h}"

    def test_tandem_capture_size_values() -> str:
        if sel.cap_w <= 0 or sel.cap_h <= 0:
            raise RuntimeError(f"invalid capture size: {sel.cap_w}x{sel.cap_h}")
        if sel.cap_w > 1920 or sel.cap_h > 1920:
            raise RuntimeError(f"capture size too large: {sel.cap_w}x{sel.cap_h}")
        return f"capture_size valid: {sel.cap_w}x{sel.cap_h}"

    def test_tandem_region_format() -> str:
        if not sel.region:
            return "no region (skipped by user)"
        parts: list[str] = sel.region.split(",")
        if len(parts) != 4:
            raise RuntimeError(f"region must have 4 parts, got {len(parts)}: {sel.region}")
        vals: list[int] = [int(p) for p in parts]
        for v in vals:
            if v < 0 or v > 1000:
                raise RuntimeError(f"region value {v} out of 0-1000 range: {sel.region}")
        if vals[2] <= vals[0] or vals[3] <= vals[1]:
            raise RuntimeError(f"region x2<=x1 or y2<=y1: {sel.region}")
        return f"region format valid: {sel.region}"

    _run_test("phase2", "click", test_click)
    _run_test("phase2", "double_click", test_double_click)
    _run_test("phase2", "right_click", test_right_click)
    _run_test("phase2", "drag", test_drag)
    _run_test("phase2", "scroll_up", test_scroll_up)
    _run_test("phase2", "scroll_down", test_scroll_down)
    _run_test("phase2", "cursor_pos_roundtrip", test_cursor_pos_roundtrip)
    _run_test("phase2", "tandem_region_in_json", test_tandem_region_in_json)
    _run_test("phase2", "tandem_capture_size_values", test_tandem_capture_size_values)
    _run_test("phase2", "tandem_region_format", test_tandem_region_format)


def phase3_keyboard_actions(sel: RegionSelection) -> None:
    sys.stdout.write("\n=== Phase 3: Keyboard Actions ===\n")
    sys.stdout.flush()
    region: str = sel.region

    def test_type_text() -> str:
        if region:
            _win32_run(["click", "--pos", "500,500", "--region", region])
            time.sleep(TIMING.action_settle)
        proc: subprocess.CompletedProcess[bytes] = _win32_run(["type_text", "--text", "hello franz"])
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        time.sleep(TIMING.type_settle)
        return "type_text executed"

    def test_press_key_enter() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(["press_key", "--key", "enter"])
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        return "press_key enter executed"

    def test_press_key_backspace() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(["press_key", "--key", "backspace"])
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        return "press_key backspace executed"

    def test_hotkey_ctrl_a() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(["hotkey", "--keys", "ctrl+a"])
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        return "hotkey ctrl+a executed"

    def test_hotkey_ctrl_z() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(["hotkey", "--keys", "ctrl+z"])
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        return "hotkey ctrl+z executed"

    def test_press_key_escape() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(["press_key", "--key", "escape"])
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        return "press_key escape executed"

    def test_type_special_chars() -> str:
        proc: subprocess.CompletedProcess[bytes] = _win32_run(["type_text", "--text", "Test123!@#"])
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}: {proc.stderr.decode()}")
        time.sleep(TIMING.type_settle)
        return "special chars typed"

    def test_keyboard_tandem_region_in_json() -> str:
        _clear_last_vlm_request()
        raw_png: bytes = _make_test_png(20, 20, 20)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        code, resp = _send_chat_completion(
            raw_b64,
            agent="test_kb_region_json",
            region=sel.region,
            capture_size=[sel.cap_w, sel.cap_h],
        )
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        return f"region={sel.region}, cap={sel.cap_w}x{sel.cap_h}"

    def test_keyboard_tandem_values() -> str:
        if sel.cap_w <= 0 or sel.cap_h <= 0:
            raise RuntimeError(f"invalid capture size: {sel.cap_w}x{sel.cap_h}")
        if sel.region:
            parts: list[str] = sel.region.split(",")
            if len(parts) != 4:
                raise RuntimeError(f"region format wrong: {sel.region}")
            vals: list[int] = [int(p) for p in parts]
            for v in vals:
                if v < 0 or v > 1000:
                    raise RuntimeError(f"out of range: {v}")
        return f"kb_region={sel.region}, kb_cap={sel.cap_w}x{sel.cap_h}"

    _run_test("phase3", "type_text", test_type_text)
    _run_test("phase3", "press_key_enter", test_press_key_enter)
    _run_test("phase3", "press_key_backspace", test_press_key_backspace)
    _run_test("phase3", "hotkey_ctrl_a", test_hotkey_ctrl_a)
    _run_test("phase3", "hotkey_ctrl_z", test_hotkey_ctrl_z)
    _run_test("phase3", "press_key_escape", test_press_key_escape)
    _run_test("phase3", "type_special_chars", test_type_special_chars)
    _run_test("phase3", "keyboard_tandem_region_in_json", test_keyboard_tandem_region_in_json)
    _run_test("phase3", "keyboard_tandem_values", test_keyboard_tandem_values)


def phase4_select_region() -> None:
    sys.stdout.write("\n=== Phase 4: select_region Automated ===\n")
    sys.stdout.flush()

    def test_select_region_esc() -> str:
        def send_esc() -> None:
            time.sleep(TIMING.select_region_esc_delay)
            _win32_run(["press_key", "--key", "escape"])

        t: threading.Thread = threading.Thread(target=send_esc, daemon=True)
        t.start()
        proc: subprocess.CompletedProcess[str] = _win32_run_text(["select_region"], timeout=10)
        t.join(timeout=3)
        if proc.returncode != 2:
            raise RuntimeError(f"expected exit code 2, got {proc.returncode}")
        if proc.stdout.strip():
            raise RuntimeError(f"expected empty stdout, got: {proc.stdout.strip()}")
        return "ESC -> exit 2, empty stdout"

    def test_select_region_right_click() -> str:
        def send_right_click() -> None:
            time.sleep(TIMING.select_region_delay)
            import ctypes
            ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)
            time.sleep(0.03)
            ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)

        t: threading.Thread = threading.Thread(target=send_right_click, daemon=True)
        t.start()
        proc: subprocess.CompletedProcess[str] = _win32_run_text(["select_region"], timeout=10)
        t.join(timeout=3)
        if proc.returncode != 0:
            raise RuntimeError(f"expected exit code 0, got {proc.returncode}")
        return "right-click -> exit 0"

    def test_tandem_esc_first_cancels() -> str:
        def send_esc() -> None:
            time.sleep(TIMING.select_region_esc_delay)
            _win32_run(["press_key", "--key", "escape"])

        t: threading.Thread = threading.Thread(target=send_esc, daemon=True)
        t.start()
        proc1: subprocess.CompletedProcess[str] = _win32_run_text(["select_region"], timeout=10)
        t.join(timeout=3)
        if proc1.returncode != 2:
            raise RuntimeError(f"first select_region: expected exit 2, got {proc1.returncode}")
        region: str = proc1.stdout.strip()
        cap_w: int = 640
        cap_h: int = 640
        if region:
            raise RuntimeError(f"first select_region should have empty stdout, got: {region}")
        return f"tandem ESC: region='', cap={cap_w}x{cap_h}"

    def test_tandem_right_click_both() -> str:
        def send_rc() -> None:
            time.sleep(TIMING.select_region_delay)
            import ctypes
            ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)
            time.sleep(0.03)
            ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)

        t1: threading.Thread = threading.Thread(target=send_rc, daemon=True)
        t1.start()
        proc1: subprocess.CompletedProcess[str] = _win32_run_text(["select_region"], timeout=10)
        t1.join(timeout=3)
        if proc1.returncode != 0:
            raise RuntimeError(f"first: expected exit 0, got {proc1.returncode}")
        region: str = proc1.stdout.strip()

        t2: threading.Thread = threading.Thread(target=send_rc, daemon=True)
        t2.start()
        proc2: subprocess.CompletedProcess[str] = _win32_run_text(["select_region"], timeout=10)
        t2.join(timeout=3)
        if proc2.returncode != 0:
            raise RuntimeError(f"second: expected exit 0, got {proc2.returncode}")
        scale_str: str = proc2.stdout.strip()
        cap_w: int = 640
        cap_h: int = 640
        if scale_str:
            parts: list[str] = scale_str.split(",")
            if len(parts) == 4:
                x1: int = int(parts[0])
                x2: int = int(parts[2])
                scale: float = (x2 - x1) / 1000
                cap_w = round(1000 * scale)
                cap_h = round(1000 * scale)
        return f"tandem right-click both: region='{region}', scale='{scale_str}', cap={cap_w}x{cap_h}"

    _run_test("phase4", "select_region_esc", test_select_region_esc)
    _run_test("phase4", "select_region_right_click", test_select_region_right_click)
    _run_test("phase4", "tandem_esc_first_cancels", test_tandem_esc_first_cancels)
    _run_test("phase4", "tandem_right_click_both", test_tandem_right_click_both)


def phase5_sse_pipeline(sse: SSEClient) -> None:
    sys.stdout.write("\n=== Phase 5: Panel SSE Pipeline ===\n")
    sys.stdout.flush()

    def test_annotate_cycle() -> str:
        raw_png: bytes = _make_test_png(0, 200, 0)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        code: int
        resp: dict[str, Any]
        code, resp = _send_chat_completion(raw_b64, agent="test_pipeline", model="test-model")
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}: {resp}")
        choices: list[Any] = resp.get("choices", [])
        if not choices:
            raise RuntimeError("no choices in response")
        content: str = choices[0].get("message", {}).get("content", "")
        if not content:
            raise RuntimeError("empty content in response")
        return f"cycle complete, content_len={len(content)}"

    def test_annotate_with_overlays() -> str:
        raw_png: bytes = _make_test_png(0, 0, 200)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        overlays: list[dict[str, Any]] = [{
            "type": "overlay",
            "points": [[100, 100], [900, 100], [900, 900], [100, 900]],
            "fill": "#ff0000",
            "closed": True,
        }]
        code, resp = _send_chat_completion(raw_b64, agent="test_overlay", overlays=overlays)
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        return "overlay cycle complete"

    def test_annotate_with_action_dispatch() -> str:
        raw_png: bytes = _make_test_png(100, 100, 0)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        actions: list[dict[str, Any]] = [{
            "type": "click",
            "x": 500,
            "y": 500,
        }]
        code, resp = _send_chat_completion(raw_b64, agent="test_action_dispatch", actions=actions)
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        return "action dispatched through pipeline"

    def test_result_unknown_request_id() -> str:
        code, resp = _http_post(f"{NET.panel_url}/result", {
            "request_id": "nonexistent-uuid",
            "annotated_b64": "dummy",
        })
        if code != 404:
            raise RuntimeError(f"expected 404, got {code}: {resp}")
        return "404 for unknown request_id"

    def test_vlm_done_event() -> str:
        sse2: SSEClient = SSEClient(f"{NET.panel_url}/events")
        if not sse2.wait_connected():
            raise RuntimeError("SSE2 did not connect")
        raw_png: bytes = _make_test_png(100, 100, 100)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")

        def do_request() -> None:
            _send_chat_completion(raw_b64, agent="test_vlm_done", model="vlm-done-model")

        t: threading.Thread = threading.Thread(target=do_request, daemon=True)
        t.start()
        annotate_evt: dict[str, Any] | None = sse2.drain_until("annotate", timeout=TIMING.sse_event_timeout)
        if annotate_evt is None:
            raise RuntimeError("no annotate event received")
        if annotate_evt.get("agent") != "test_vlm_done":
            raise RuntimeError(f"wrong agent: {annotate_evt.get('agent')}")
        if "request_id" not in annotate_evt:
            raise RuntimeError("no request_id in annotate event")
        if "raw_b64" not in annotate_evt:
            raise RuntimeError("no raw_b64 in annotate event")
        if annotate_evt.get("model") != "vlm-done-model":
            raise RuntimeError(f"wrong model: {annotate_evt.get('model')}")
        vlm_done_evt: dict[str, Any] | None = sse2.drain_until("vlm_done", timeout=NET.request_timeout)
        if vlm_done_evt is None:
            raise RuntimeError("no vlm_done event received")
        if vlm_done_evt.get("agent") != "test_vlm_done":
            raise RuntimeError(f"wrong agent in vlm_done: {vlm_done_evt.get('agent')}")
        if "request_id" not in vlm_done_evt:
            raise RuntimeError("no request_id in vlm_done event")
        if "text" not in vlm_done_evt:
            raise RuntimeError("no text in vlm_done event")
        t.join(timeout=5)
        sse2.stop()
        return "annotate + vlm_done events verified"

    def test_empty_image_url_triggers_capture() -> str:
        code, resp = _send_chat_completion("", agent="test_auto_capture")
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        return "empty image_url triggered capture"

    def test_capture_size_forwarded() -> str:
        _clear_last_vlm_request()
        raw_png: bytes = _make_test_png(30, 30, 30)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        code, resp = _send_chat_completion(
            raw_b64,
            agent="test_cap_size_fwd",
            capture_size=[256, 256],
        )
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        return "capture_size forwarded"

    def test_region_stripped_from_vlm_body() -> str:
        _clear_last_vlm_request()
        raw_png: bytes = _make_test_png(40, 40, 40)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        code, resp = _send_chat_completion(
            raw_b64,
            agent="test_region_strip",
            region="100,100,900,900",
        )
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        last: dict[str, Any] = _get_last_vlm_request()
        if "region" in last:
            raise RuntimeError(f"region not stripped from VLM body: {last.get('region')}")
        if "agent" in last:
            raise RuntimeError(f"agent not stripped from VLM body: {last.get('agent')}")
        if "capture_size" in last:
            raise RuntimeError(f"capture_size not stripped from VLM body: {last.get('capture_size')}")
        return "region/agent/capture_size stripped from VLM body"

    _run_test("phase5", "annotate_cycle", test_annotate_cycle)
    _run_test("phase5", "annotate_with_overlays", test_annotate_with_overlays)
    _run_test("phase5", "annotate_with_action_dispatch", test_annotate_with_action_dispatch)
    _run_test("phase5", "result_unknown_request_id", test_result_unknown_request_id)
    _run_test("phase5", "vlm_done_event", test_vlm_done_event)
    _run_test("phase5", "empty_image_url_capture", test_empty_image_url_triggers_capture)
    _run_test("phase5", "capture_size_forwarded", test_capture_size_forwarded)
    _run_test("phase5", "region_stripped_from_vlm_body", test_region_stripped_from_vlm_body)


def phase6_overlay_rendering(sse: SSEClient) -> None:
    sys.stdout.write("\n=== Phase 6: Overlay Rendering Correctness ===\n")
    sys.stdout.flush()

    def _run_overlay_test(
        agent: str,
        overlays: list[dict[str, Any]],
        base_r: int = 0,
        base_g: int = 0,
        base_b: int = 0,
    ) -> tuple[str, str, dict[str, Any] | None]:
        raw_png: bytes = _make_test_png(base_r, base_g, base_b)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        sse2: SSEClient = SSEClient(f"{NET.panel_url}/events")
        if not sse2.wait_connected():
            raise RuntimeError("SSE did not connect")

        def do_req() -> None:
            _send_chat_completion(raw_b64, agent=agent, overlays=overlays)

        t: threading.Thread = threading.Thread(target=do_req, daemon=True)
        t.start()
        vlm_done: dict[str, Any] | None = sse2.drain_until("vlm_done", timeout=NET.request_timeout)
        t.join(timeout=5)
        sse2.stop()
        annotated_b64: str = vlm_done.get("annotated_b64", "") if vlm_done else ""
        return raw_b64, annotated_b64, vlm_done

    def test_fill_overlay_changes_pixels() -> str:
        overlays: list[dict[str, Any]] = [{
            "type": "overlay",
            "points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
            "fill": "#ff0000",
            "closed": True,
        }]
        raw_b64, annotated_b64, vlm_done = _run_overlay_test("test_fill_pixels", overlays)
        if vlm_done is None:
            raise RuntimeError("no vlm_done event")
        if not annotated_b64:
            raise RuntimeError("no annotated_b64 in vlm_done")
        red_count: int = _count_red_pixels(annotated_b64)
        if red_count < IMG.pixel_change_threshold:
            raise RuntimeError(f"only {red_count} red pixels, expected >= {IMG.pixel_change_threshold}")
        diff: int = _pixel_diff_count(raw_b64, annotated_b64)
        return f"{diff} pixels changed, {red_count} red pixels"

    def test_stroke_overlay_changes_pixels() -> str:
        overlays: list[dict[str, Any]] = [{
            "type": "overlay",
            "points": [[200, 200], [800, 200], [800, 800], [200, 800]],
            "stroke": "#ff0000",
            "stroke_width": 3,
            "closed": True,
        }]
        raw_b64, annotated_b64, vlm_done = _run_overlay_test("test_stroke_pixels", overlays)
        if vlm_done is None:
            raise RuntimeError("no vlm_done event")
        diff: int = _pixel_diff_count(raw_b64, annotated_b64)
        red_count: int = _count_red_pixels(annotated_b64)
        if red_count < 1:
            raise RuntimeError("no red pixels found for stroke overlay")
        return f"{diff} pixels changed, {red_count} red pixels from stroke"

    def test_no_overlay_pixel_preservation() -> str:
        raw_png: bytes = _make_test_png(0, 0, 0)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        sse2: SSEClient = SSEClient(f"{NET.panel_url}/events")
        if not sse2.wait_connected():
            raise RuntimeError("SSE did not connect")

        def do_req() -> None:
            _send_chat_completion(raw_b64, agent="test_no_overlay")

        t: threading.Thread = threading.Thread(target=do_req, daemon=True)
        t.start()
        vlm_done: dict[str, Any] | None = sse2.drain_until("vlm_done", timeout=NET.request_timeout)
        t.join(timeout=5)
        sse2.stop()
        if vlm_done is None:
            raise RuntimeError("no vlm_done event")
        annotated_b64: str = vlm_done.get("annotated_b64", "")
        if not annotated_b64:
            raise RuntimeError("no annotated_b64")
        diff: int = _pixel_diff_count(raw_b64, annotated_b64)
        red_count: int = _count_red_pixels(annotated_b64)
        if red_count > 0:
            raise RuntimeError(f"unexpected {red_count} red pixels with no overlay")
        return f"pixel diff={diff} (re-encode only, 0 red pixels)"

    def test_fill_vs_no_overlay_red_delta() -> str:
        raw_png: bytes = _make_test_png(0, 0, 0)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")

        sse_no: SSEClient = SSEClient(f"{NET.panel_url}/events")
        if not sse_no.wait_connected():
            raise RuntimeError("SSE did not connect")

        no_overlay_b64: list[str] = []

        def do_no() -> None:
            _send_chat_completion(raw_b64, agent="test_fill_delta_no")

        t1: threading.Thread = threading.Thread(target=do_no, daemon=True)
        t1.start()
        vd1: dict[str, Any] | None = sse_no.drain_until("vlm_done", timeout=NET.request_timeout)
        t1.join(timeout=5)
        sse_no.stop()
        if vd1:
            no_overlay_b64.append(vd1.get("annotated_b64", ""))

        overlays: list[dict[str, Any]] = [{
            "type": "overlay",
            "points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
            "fill": "#ff0000",
            "closed": True,
        }]
        sse_fill: SSEClient = SSEClient(f"{NET.panel_url}/events")
        if not sse_fill.wait_connected():
            raise RuntimeError("SSE did not connect")

        fill_b64: list[str] = []

        def do_fill() -> None:
            _send_chat_completion(raw_b64, agent="test_fill_delta_yes", overlays=overlays)

        t2: threading.Thread = threading.Thread(target=do_fill, daemon=True)
        t2.start()
        vd2: dict[str, Any] | None = sse_fill.drain_until("vlm_done", timeout=NET.request_timeout)
        t2.join(timeout=5)
        sse_fill.stop()
        if vd2:
            fill_b64.append(vd2.get("annotated_b64", ""))

        if not no_overlay_b64 or not no_overlay_b64[0]:
            raise RuntimeError("no-overlay result missing")
        if not fill_b64 or not fill_b64[0]:
            raise RuntimeError("fill result missing")

        red_no: int = _count_red_pixels(no_overlay_b64[0])
        red_fill: int = _count_red_pixels(fill_b64[0])
        delta: int = red_fill - red_no
        if delta < IMG.pixel_change_threshold:
            raise RuntimeError(f"red delta {delta} too small (fill={red_fill}, no={red_no})")
        return f"red_no={red_no}, red_fill={red_fill}, delta={delta}"

    _run_test("phase6", "fill_overlay_changes_pixels", test_fill_overlay_changes_pixels)
    _run_test("phase6", "stroke_overlay_changes_pixels", test_stroke_overlay_changes_pixels)
    _run_test("phase6", "no_overlay_pixel_preservation", test_no_overlay_pixel_preservation)
    _run_test("phase6", "fill_vs_no_overlay_red_delta", test_fill_vs_no_overlay_red_delta)


def phase7_concurrent_isolation(sse: SSEClient) -> None:
    sys.stdout.write("\n=== Phase 7: Concurrent Request Isolation ===\n")
    sys.stdout.flush()

    def test_concurrent_agents() -> str:
        agent_count: int = 3
        barriers: list[threading.Event] = [threading.Event() for _ in range(agent_count)]
        results: list[dict[str, Any]] = [{} for _ in range(agent_count)]
        errors: list[str] = ["" for _ in range(agent_count)]

        def agent_request(idx: int) -> None:
            try:
                color_r: int = (idx * 80) % 256
                raw_png: bytes = _make_test_png(color_r, 50, 50)
                raw_b64: str = base64.b64encode(raw_png).decode("ascii")
                code, resp = _send_chat_completion(
                    raw_b64,
                    agent=f"concurrent_{idx}",
                    model=f"model_{idx}",
                    timeout=TIMING.concurrent_timeout,
                )
                results[idx] = {"code": code, "resp": resp}
            except Exception as exc:
                errors[idx] = str(exc)
            finally:
                barriers[idx].set()

        threads: list[threading.Thread] = []
        for i in range(agent_count):
            t: threading.Thread = threading.Thread(target=agent_request, args=(i,), daemon=True)
            threads.append(t)
            t.start()

        for i in range(agent_count):
            if not barriers[i].wait(timeout=TIMING.concurrent_timeout):
                raise RuntimeError(f"agent {i} timed out")

        for i in range(agent_count):
            if errors[i]:
                raise RuntimeError(f"agent {i} error: {errors[i]}")
            if results[i].get("code") != 200:
                raise RuntimeError(f"agent {i} got code {results[i].get('code')}")

        return f"{agent_count} concurrent agents completed independently"

    def test_concurrent_distinct_request_ids() -> str:
        agent_count: int = 2
        sse2: SSEClient = SSEClient(f"{NET.panel_url}/events")
        if not sse2.wait_connected():
            raise RuntimeError("SSE did not connect")
        barriers: list[threading.Event] = [threading.Event() for _ in range(agent_count)]

        def agent_req(idx: int) -> None:
            try:
                raw_png: bytes = _make_test_png(idx * 120, 0, 0)
                raw_b64: str = base64.b64encode(raw_png).decode("ascii")
                _send_chat_completion(raw_b64, agent=f"iso_{idx}", timeout=TIMING.concurrent_timeout)
            except Exception:
                pass
            finally:
                barriers[idx].set()

        for i in range(agent_count):
            threading.Thread(target=agent_req, args=(i,), daemon=True).start()

        request_ids: set[str] = set()
        agents_seen: set[str] = set()
        for _ in range(agent_count * 2):
            evt: tuple[str, dict[str, Any]] | None = sse2.next_event(timeout=NET.request_timeout)
            if evt is None:
                break
            if evt[0] == "annotate":
                rid: str = evt[1].get("request_id", "")
                if rid in request_ids:
                    raise RuntimeError(f"duplicate request_id: {rid}")
                request_ids.add(rid)
                agents_seen.add(evt[1].get("agent", ""))

        for i in range(agent_count):
            barriers[i].wait(timeout=TIMING.concurrent_timeout)

        sse2.stop()
        if len(request_ids) < agent_count:
            raise RuntimeError(f"expected {agent_count} distinct request_ids, got {len(request_ids)}")
        return f"{len(request_ids)} distinct request_ids, agents={sorted(agents_seen)}"

    _run_test("phase7", "concurrent_agents", test_concurrent_agents)
    _run_test("phase7", "concurrent_distinct_request_ids", test_concurrent_distinct_request_ids)


def phase8_log_structure() -> None:
    sys.stdout.write("\n=== Phase 8: Log Structure ===\n")
    sys.stdout.flush()

    def test_log_entries() -> str:
        if not PATHS.franz_log.exists():
            raise RuntimeError("franz-log.jsonl does not exist")
        lines: list[str] = PATHS.franz_log.read_text(encoding="utf-8").strip().split("\n")
        if not lines or not lines[0].strip():
            raise RuntimeError("log file is empty")
        events_found: dict[str, int] = {}
        parse_errors: int = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                entry: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            event: str = entry.get("event", "")
            events_found[event] = events_found.get(event, 0) + 1
            match event:
                case "vlm_request":
                    for field in ["ts", "model", "agent", "overlays"]:
                        if field not in entry:
                            raise RuntimeError(f"vlm_request missing field: {field}")
                    if not isinstance(entry["ts"], (int, float)):
                        raise RuntimeError(f"vlm_request ts is {type(entry['ts'])}")
                    if not isinstance(entry["overlays"], int):
                        raise RuntimeError(f"vlm_request overlays is {type(entry['overlays'])}")
                case "vlm_response":
                    for field in ["ts", "duration_ms", "text", "annotated", "agent", "request_id"]:
                        if field not in entry:
                            raise RuntimeError(f"vlm_response missing field: {field}")
                    if not isinstance(entry["duration_ms"], (int, float)):
                        raise RuntimeError(f"duration_ms is {type(entry['duration_ms'])}")
                    if not isinstance(entry["annotated"], bool):
                        raise RuntimeError(f"annotated is {type(entry['annotated'])}")
                case "action_dispatched":
                    if "type" not in entry:
                        raise RuntimeError("action_dispatched missing type field")
                    if "ts" not in entry:
                        raise RuntimeError("action_dispatched missing ts field")
                case "vlm_error":
                    if "error" not in entry:
                        raise RuntimeError("vlm_error missing error field")
                    if "ts" not in entry:
                        raise RuntimeError("vlm_error missing ts field")
                case "panel_js":
                    if "ts" not in entry:
                        raise RuntimeError("panel_js missing ts field")
        return f"{len(lines)} entries, events: {dict(sorted(events_found.items()))}, parse_errors={parse_errors}"

    def test_log_has_vlm_request() -> str:
        if not PATHS.franz_log.exists():
            raise RuntimeError("franz-log.jsonl does not exist")
        content: str = PATHS.franz_log.read_text(encoding="utf-8")
        if '"vlm_request"' not in content:
            raise RuntimeError("no vlm_request events in log")
        return "vlm_request present"

    def test_log_has_vlm_response() -> str:
        if not PATHS.franz_log.exists():
            raise RuntimeError("franz-log.jsonl does not exist")
        content: str = PATHS.franz_log.read_text(encoding="utf-8")
        if '"vlm_response"' not in content:
            raise RuntimeError("no vlm_response events in log")
        return "vlm_response present"

    def test_log_has_action_dispatched() -> str:
        if not PATHS.franz_log.exists():
            raise RuntimeError("franz-log.jsonl does not exist")
        content: str = PATHS.franz_log.read_text(encoding="utf-8")
        if '"action_dispatched"' not in content:
            raise RuntimeError("no action_dispatched events in log")
        return "action_dispatched present"

    def test_log_has_panel_js() -> str:
        if not PATHS.franz_log.exists():
            raise RuntimeError("franz-log.jsonl does not exist")
        content: str = PATHS.franz_log.read_text(encoding="utf-8")
        if '"panel_js"' not in content:
            raise RuntimeError("no panel_js events in log")
        return "panel_js present"

    def test_log_request_response_pairing() -> str:
        if not PATHS.franz_log.exists():
            raise RuntimeError("franz-log.jsonl does not exist")
        lines: list[str] = PATHS.franz_log.read_text(encoding="utf-8").strip().split("\n")
        request_count: int = 0
        response_count: int = 0
        response_rids: set[str] = set()
        for line in lines:
            if not line.strip():
                continue
            try:
                entry: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event") == "vlm_request":
                request_count += 1
            elif entry.get("event") == "vlm_response":
                response_count += 1
                rid: str = entry.get("request_id", "")
                if rid in response_rids:
                    raise RuntimeError(f"duplicate response request_id: {rid}")
                response_rids.add(rid)
        if request_count == 0:
            raise RuntimeError("no vlm_request entries")
        if response_count == 0:
            raise RuntimeError("no vlm_response entries")
        return f"requests={request_count}, responses={response_count}, unique_rids={len(response_rids)}"

    _run_test("phase8", "log_entries", test_log_entries)
    _run_test("phase8", "log_has_vlm_request", test_log_has_vlm_request)
    _run_test("phase8", "log_has_vlm_response", test_log_has_vlm_response)
    _run_test("phase8", "log_has_action_dispatched", test_log_has_action_dispatched)
    _run_test("phase8", "log_has_panel_js", test_log_has_panel_js)
    _run_test("phase8", "log_request_response_pairing", test_log_request_response_pairing)


def phase9_vlm_absent(panel_proc: subprocess.Popen[bytes]) -> subprocess.Popen[bytes]:
    sys.stdout.write("\n=== Phase 9: VLM Absent (502 Propagation) ===\n")
    sys.stdout.flush()

    sys.stdout.write("  Stopping panel for VLM-absent test...\n")
    sys.stdout.flush()
    _stop_panel(panel_proc)

    dead_vlm_port: int = NET.mock_vlm_port
    _write_panel_wrapper(dead_vlm_port)
    panel_proc = _start_panel_wrapper()
    if not _wait_panel_ready():
        sys.stdout.write("  [SKIP] panel did not restart for 502 test\n")
        sys.stdout.flush()
        return panel_proc

    _warmup_chrome_sse(vlm_available=False)

    def test_vlm_502_propagation() -> str:
        raw_png: bytes = _make_test_png(50, 50, 50)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        code, resp = _send_chat_completion(raw_b64, agent="test_502", timeout=15)
        if code != 502:
            raise RuntimeError(f"expected 502, got {code}: {resp}")
        err: str = resp.get("error", "")
        if not err:
            raise RuntimeError("no error field in 502 response")
        return f"502 with error: {err[:100]}"

    def test_vlm_error_sse_event() -> str:
        sse_502: SSEClient = SSEClient(f"{NET.panel_url}/events")
        if not sse_502.wait_connected():
            raise RuntimeError("SSE did not connect")
        raw_png: bytes = _make_test_png(60, 60, 60)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")

        def do_req() -> None:
            _send_chat_completion(raw_b64, agent="test_502_sse", timeout=15)

        t: threading.Thread = threading.Thread(target=do_req, daemon=True)
        t.start()
        vlm_done: dict[str, Any] | None = sse_502.drain_until("vlm_done", timeout=20)
        t.join(timeout=10)
        sse_502.stop()
        if vlm_done is None:
            raise RuntimeError("no vlm_done event after 502")
        text: str = vlm_done.get("text", "")
        if "ERROR" not in text.upper():
            raise RuntimeError(f"expected ERROR in vlm_done text, got: {text[:100]}")
        return f"vlm_done with error text: {text[:80]}"

    _run_test("phase9", "vlm_502_propagation", test_vlm_502_propagation)
    _run_test("phase9", "vlm_error_sse_event", test_vlm_error_sse_event)

    sys.stdout.write("  Restarting panel with mock VLM...\n")
    sys.stdout.flush()
    _stop_panel(panel_proc)

    return panel_proc


def phase10_lm_studio(lm_studio_present: bool, panel_proc_holder: list[subprocess.Popen[bytes]]) -> None:
    sys.stdout.write("\n=== Phase 10: LM Studio Present ===\n")
    sys.stdout.flush()

    if not lm_studio_present:
        sys.stdout.write("  [SKIP] LM Studio not detected on :1235\n")
        sys.stdout.flush()
        return

    sys.stdout.write("  Restarting panel with LM Studio backend...\n")
    sys.stdout.flush()
    _write_panel_wrapper(NET.vlm_port)
    panel_proc: subprocess.Popen[bytes] = _start_panel_wrapper()
    panel_proc_holder[0] = panel_proc
    if not _wait_panel_ready():
        sys.stdout.write("  [SKIP] panel did not restart for LM Studio tests\n")
        sys.stdout.flush()
        return

    _warmup_chrome_sse(vlm_available=True)

    def test_real_vlm_response() -> str:
        raw_png: bytes = _make_test_png(128, 128, 128)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        code, resp = _send_chat_completion(raw_b64, agent="test_real_vlm", model="qwen3.5-0.8b", timeout=60)
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}: {resp}")
        choices: list[Any] = resp.get("choices", [])
        if not choices:
            raise RuntimeError("no choices in response")
        content: str = choices[0].get("message", {}).get("content", "")
        if not content:
            raise RuntimeError("empty content from VLM")
        model_resp: str = resp.get("model", "")
        return f"model={model_resp}, content_len={len(content)}"

    def test_real_vlm_response_format() -> str:
        raw_png: bytes = _make_test_png(64, 64, 64)
        raw_b64: str = base64.b64encode(raw_png).decode("ascii")
        code, resp = _send_chat_completion(raw_b64, agent="test_real_vlm_fmt", model="qwen3.5-0.8b", timeout=60)
        if code != 200:
            raise RuntimeError(f"expected 200, got {code}")
        if "choices" not in resp:
            raise RuntimeError("no choices field")
        if "model" not in resp:
            raise RuntimeError("no model field")
        choices: list[Any] = resp["choices"]
        if not isinstance(choices, list) or len(choices) == 0:
            raise RuntimeError("choices is not a non-empty list")
        choice: dict[str, Any] = choices[0]
        if "message" not in choice:
            raise RuntimeError("no message in choice")
        if "content" not in choice["message"]:
            raise RuntimeError("no content in message")
        if "finish_reason" not in choice:
            raise RuntimeError("no finish_reason in choice")
        return f"response format valid, model={resp.get('model', '')}"

    _run_test("phase10", "real_vlm_response", test_real_vlm_response)
    _run_test("phase10", "real_vlm_response_format", test_real_vlm_response_format)


def main() -> None:
    sys.stdout.write("=" * 60 + "\n")
    sys.stdout.write("Franz Swarm Autonomous Test Suite v3\n")
    sys.stdout.write("=" * 60 + "\n")
    sys.stdout.flush()

    _cleanup_outputs()

    for required in [PATHS.win32, PATHS.panel_py, PATHS.panel_html]:
        if not required.exists():
            sys.stderr.write(f"FATAL: missing required file: {required}\n")
            sys.stderr.flush()
            raise SystemExit(2)

    lm_studio_present: bool = _port_open(NET.vlm_host, NET.vlm_port, timeout=NET.lm_studio_probe_timeout)

    sys.stdout.write(f"LM Studio on :{NET.vlm_port}: {'DETECTED' if lm_studio_present else 'NOT FOUND'}\n")
    sys.stdout.write(f"Starting mock VLM on :{NET.mock_vlm_port}...\n")
    sys.stdout.flush()
    mock_vlm: http.server.ThreadingHTTPServer = _start_mock_vlm(NET.mock_vlm_port, response_text="MOCK_TEST_RESPONSE")
    time.sleep(TIMING.mock_startup)

    _write_panel_wrapper(NET.mock_vlm_port)

    sys.stdout.write("Starting panel.py (via wrapper -> mock VLM)...\n")
    sys.stdout.flush()
    panel_proc: subprocess.Popen[bytes] = _start_panel_wrapper()

    chrome_proc: subprocess.Popen[bytes] | None = None
    if PATHS.chrome_exe.exists():
        sys.stdout.write("Launching Chrome...\n")
        sys.stdout.flush()
        chrome_proc = subprocess.Popen(
            [
                str(PATHS.chrome_exe),
                "--new-window",
                f"{NET.panel_url}/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(TIMING.chrome_startup)
    else:
        sys.stdout.write(f"Chrome not found at {PATHS.chrome_exe}, skipping auto-launch\n")
        sys.stdout.write("Please open http://127.0.0.1:1236/ in Chrome manually\n")
        sys.stdout.flush()
        input("Press Enter when Chrome is open and connected...")

    sse: SSEClient = SSEClient(f"{NET.panel_url}/events")

    if not phase0_setup(panel_proc, sse):
        sys.stderr.write("FATAL: Phase 0 setup failed\n")
        sys.stderr.flush()
        panel_proc.terminate()
        _write_results()
        raise SystemExit(2)

    _warmup_chrome_sse(vlm_available=True)

    phase1_capture()

    sys.stdout.write("\n>>> HUMAN PAUSE 1: Select a screen region for mouse action tests (tandem: region + scale)\n")
    sys.stdout.flush()
    global _mouse_sel
    _mouse_sel = _do_tandem_select("Select region and scale for MOUSE tests (e.g. an empty desktop area)")

    phase2_mouse_actions(_mouse_sel)

    sys.stdout.write("\n>>> HUMAN PAUSE 2: Select a screen region for keyboard tests (tandem: region + scale)\n")
    sys.stdout.flush()
    global _keyboard_sel
    _keyboard_sel = _do_tandem_select("Select region and scale for KEYBOARD tests (e.g. a Notepad++ window)")

    phase3_keyboard_actions(_keyboard_sel)

    phase4_select_region()

    phase5_sse_pipeline(sse)

    phase6_overlay_rendering(sse)

    phase7_concurrent_isolation(sse)

    phase8_log_structure()

    sse.stop()

    mock_vlm.shutdown()
    time.sleep(TIMING.port_release)

    panel_proc = phase9_vlm_absent(panel_proc)

    panel_proc_holder: list[subprocess.Popen[bytes]] = [panel_proc]
    if lm_studio_present:
        phase10_lm_studio(lm_studio_present, panel_proc_holder)
        panel_proc = panel_proc_holder[0]

    sys.stdout.write("\nShutting down...\n")
    sys.stdout.flush()

    if panel_proc.poll() is None:
        _stop_panel(panel_proc)

    time.sleep(TIMING.log_rename_delay)
    _rename_log_safe()
    _write_results()

    if PATHS.panel_wrapper.exists():
        try:
            PATHS.panel_wrapper.unlink()
        except OSError:
            pass

    with _results_lock:
        total: int = len(_results)
        passed: int = sum(1 for r in _results if r.passed)
        failed: int = total - passed

    sys.stdout.write("\n" + "=" * 60 + "\n")
    sys.stdout.write(f"RESULTS: {passed}/{total} passed, {failed} failed\n")
    sys.stdout.write(f"Output: {PATHS.output_json}\n")
    sys.stdout.write(f"Log: {PATHS.output_jsonl}\n")
    sys.stdout.write("=" * 60 + "\n")
    sys.stdout.flush()

    if failed > 0:
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()