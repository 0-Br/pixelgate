"""测试共用夹具：样例配置、临时输出目录、合成图片与回环上的假服务。

假服务一律绑 `127.0.0.1:0`，端口由内核分配，测试不碰真实网关端口，也不发任何外网请求。
"""

import base64
import email
import hashlib
import io
import itertools
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, PngImagePlugin

from pixelgate.schemas import Config, ImageRef, parse_config

CONFIG_EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"
#: 假 HOME 里写入的合成 client key，形态与 helper 契约的要求一致。
CLIENT_KEY = "ab" * 32
#: 假 HOME 下 key 文件的相对位置，与 README 里 helper 示例读取的位置相同。
KEY_FILE = Path(".config") / "pixelgate" / "client.key"
#: 替身 helper：按 README 的契约无参数运行、stdout 输出 key 文件的内容。
KEY_HELPER_SCRIPT = f'#!/bin/sh\nexec cat "$HOME/{KEY_FILE}"\n'
MIB = 1024 * 1024


def png_bytes(width: int = 64, height: int = 64, mode: str = "RGB") -> bytes:
    """一张合成 PNG 的字节。"""
    buffer = io.BytesIO()
    Image.new(mode, (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


def png_bytes_of_size(total: int) -> bytes:
    """构造恰好 `total` 字节且 Pillow 能完整解码的 PNG。

    多出来的体积放进一个未压缩的 `tEXt` 块，所以文件体积可以逐字节调准，而图像本身仍然
    很小、解码很快。返回的字节数保证等于 `total`。
    """
    base = Image.new("RGB", (8, 8))
    pad = 0
    data = b""
    for _ in range(4):
        info = PngImagePlugin.PngInfo()
        if pad > 0:
            info.add_text("pad", "x" * pad)
        buffer = io.BytesIO()
        base.save(buffer, format="PNG", pnginfo=info)
        data = buffer.getvalue()
        if len(data) == total:
            return data
        pad += total - len(data)
        if pad < 0:
            raise ValueError(f"目标体积 {total} 小于最小 PNG")
    raise ValueError(f"PNG 体积未收敛到 {total}，最后一次 {len(data)}")


@dataclass
class RecordedRequest:
    """假服务收到的一次请求。"""

    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any] | None
    fields: dict[str, list[bytes]]


def _parse_body(
    content_type: str, raw: bytes
) -> tuple[dict[str, Any] | None, dict[str, list[bytes]]]:
    """把请求体解析成 JSON 对象或 multipart 字段表，两者取其一。"""
    if content_type.startswith("application/json"):
        parsed: dict[str, Any] = json.loads(raw)
        return parsed, {}
    if content_type.startswith("multipart/form-data"):
        message = email.message_from_bytes(
            b"Content-Type: "
            + content_type.encode()
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + raw
        )
        fields: dict[str, list[bytes]] = {}
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if isinstance(name, str) and isinstance(payload, bytes):
                fields.setdefault(name, []).append(payload)
        return None, fields
    return None, {}


class FakeGateway:
    """回环上的假网关：记录每个请求，按 `mode` 决定怎么应答。

    支持的 mode：`ok`、`status_5xx`、`drop_mid_body`、`sleep`、`redirect_302`、
    `no_data`、`two_data`、`bad_b64`、`oversize_body`。只计数的陷阱服务用同一个类，
    保持默认 `ok` 即可，断言看 `count`。
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.requests: list[RecordedRequest] = []
        self.image_bytes = png_bytes()
        self.model: str | None = "gpt-image-2.5-sunburst"
        self.revised_prompt: str | None = "上游回写的提示词"
        self.usage: dict[str, Any] | None = {
            "input_tokens": 11,
            "input_tokens_details": {"image_tokens": 4, "text_tokens": 7},
            "output_tokens": 22,
            "output_tokens_details": {"image_tokens": 22, "text_tokens": 0},
            "total_tokens": 33,
        }
        self.request_id: str | None = "test-req-1"
        self.sleep_seconds = 3.0
        self.redirect_to = ""
        self.oversize_bytes = 64 * MIB + 1
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        """内核分配的监听端口。"""
        return int(self._server.server_address[1])

    @property
    def count(self) -> int:
        """收到的请求数。"""
        return len(self.requests)

    @property
    def last(self) -> RecordedRequest:
        """最后一个请求；没有请求时直接 IndexError，便于定位断言写错的用例。"""
        return self.requests[-1]

    def payload(self) -> dict[str, Any]:
        """成功响应的 JSON 体，形态取自 Images API 的实际返回。"""
        image: dict[str, Any] = {
            "b64_json": base64.b64encode(self.image_bytes).decode("ascii")
        }
        if self.revised_prompt is not None:
            image["revised_prompt"] = self.revised_prompt
        body: dict[str, Any] = {"created": 1789000000, "data": [image]}
        if self.model is not None:
            body["model"] = self.model
        if self.usage is not None:
            body["usage"] = self.usage
        return body

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_handler(gateway: FakeGateway) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self._record()
            self._reply()

        def do_GET(self) -> None:
            self._record()
            self._reply()

        def do_CONNECT(self) -> None:
            self._record()
            self.send_error(405)

        def log_message(self, format: str, *args: Any) -> None:
            """压掉 stderr 上的访问日志，测试输出只留断言信息。"""

        def _record(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            body, fields = _parse_body(self.headers.get("Content-Type", ""), raw)
            gateway.requests.append(
                RecordedRequest(
                    method=self.command,
                    path=self.path,
                    headers={key.lower(): value for key, value in self.headers.items()},
                    body=body,
                    fields=fields,
                )
            )

        def _reply(self) -> None:
            mode = gateway.mode
            if mode == "sleep":
                time.sleep(gateway.sleep_seconds)
                mode = "ok"
            if mode == "status_5xx":
                self._send_json(503, {"error": {"message": "上游暂时不可用"}})
                return
            if mode == "redirect_302":
                self.send_response(302)
                self.send_header("Location", gateway.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if mode == "drop_mid_body":
                self._send_half()
                return
            if mode == "oversize_body":
                self._send_oversize()
                return
            body = gateway.payload()
            if mode == "no_data":
                body["data"] = []
            elif mode == "two_data":
                body["data"] = body["data"] * 2
            elif mode == "bad_b64":
                body["data"] = [{"b64_json": "!!!! 这不是 base64 !!!!"}]
            self._send_json(200, body)

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if gateway.request_id is not None:
                self.send_header("x-request-id", gateway.request_id)
            self.end_headers()
            self.wfile.write(raw)

        def _send_half(self) -> None:
            """声明一个长度，只写一半就断开，制造落地前的传输中断。"""
            raw = json.dumps(gateway.payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw[: len(raw) // 2])
            self.wfile.flush()
            self.close_connection = True

        def _send_oversize(self) -> None:
            """按 1 MiB 一块吐出超预算的响应体，客户端中止读取后收工。"""
            chunk = b"x" * MIB
            total = gateway.oversize_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(total))
            self.end_headers()
            sent = 0
            try:
                while sent < total:
                    piece = chunk[: min(len(chunk), total - sent)]
                    self.wfile.write(piece)
                    sent += len(piece)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    return Handler


def _running_gateway() -> Iterator[FakeGateway]:
    gateway = FakeGateway()
    try:
        yield gateway
    finally:
        gateway.close()


@pytest.fixture
def key_helper(tmp_path: Path) -> Path:
    """写在临时目录里的替身 helper，可执行，读假 HOME 下的 key 文件。"""
    path = tmp_path / "bin" / "read_client_key.sh"
    path.parent.mkdir()
    path.write_text(KEY_HELPER_SCRIPT, encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.fixture
def config_data(key_helper: Path) -> dict[str, Any]:
    """样例配置的独立副本，`client_key_helper` 换成替身 helper。

    样例里的 helper 路径是占位值，照原样校验必然不过；其余字段原样保留。测试可以随意
    改写返回值而不影响别的用例。
    """
    with CONFIG_EXAMPLE.open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    data["client_key_helper"] = str(key_helper)
    return data


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> Path:
    """一个真实存在的输出目录，供 output_dir 校验用。"""
    path = tmp_path / "output"
    path.mkdir()
    return path


@pytest.fixture
def make_png(tmp_path: Path) -> Callable[..., ImageRef]:
    """生成一张 PNG 并返回带真实 sha256 的引用。

    默认 64x64 的 RGB 图；mode 传 "RGBA" 得到带 alpha 通道的图，传 "L" 得到灰度图。
    每次调用写一个新文件，同一个测试里可以多次取图。
    """
    images = tmp_path / "images"
    images.mkdir()
    counter = itertools.count()

    def _make(width: int = 64, height: int = 64, mode: str = "RGB") -> ImageRef:
        path = images / f"img-{next(counter)}.png"
        Image.new(mode, (width, height)).save(path, format="PNG")
        return ImageRef(
            path=str(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    return _make


@pytest.fixture
def write_image(tmp_path: Path) -> Callable[[bytes], ImageRef]:
    """把任意字节写成文件并返回引用。

    用来构造 `make_png` 造不出的输入：损坏的文件、恰好某个体积的 PNG、非图片内容。
    引用里的 sha256 按写入的字节算，所以默认是「声明与内容一致」的情形。
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    counter = itertools.count()

    def _write(data: bytes) -> ImageRef:
        path = raw_dir / f"raw-{next(counter)}.png"
        path.write_bytes(data)
        return ImageRef(path=str(path), sha256=hashlib.sha256(data).hexdigest())

    return _write


@pytest.fixture
def loose_umask() -> Iterator[None]:
    """把 umask 放到最松，检验权限位是代码显式设定的，而不是 umask 顺带给的。"""
    previous = os.umask(0o000)
    try:
        yield None
    finally:
        os.umask(previous)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """假 HOME，内含一枚 mode 600 的合成 client key。

    替身 helper 读 `$HOME/.config/pixelgate/client.key`，所以经这个夹具就能在不碰用户
    真实凭据的前提下走完整条取 key 的路径。
    """
    home = tmp_path / "home"
    key_file = home / KEY_FILE
    key_file.parent.mkdir(parents=True)
    key_file.write_text(f"{CLIENT_KEY}\n", encoding="utf-8")
    key_file.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def fake_gateway() -> Iterator[FakeGateway]:
    """假网关，配置里的 base_url 指向它。"""
    yield from _running_gateway()


@pytest.fixture
def proxy_trap() -> Iterator[FakeGateway]:
    """代理陷阱：代理环境变量指向它，收到任何请求都说明出站路径被劫持。"""
    yield from _running_gateway()


@pytest.fixture
def decoy_gateway() -> Iterator[FakeGateway]:
    """诱饵服务：目标守卫的阳性对照，收到任何请求都说明守卫没拦住。"""
    yield from _running_gateway()


@pytest.fixture
def gateway_config(config_data: dict[str, Any], fake_gateway: FakeGateway) -> Config:
    """指向假网关的配置；`client_key_helper` 是替身 helper。"""
    config_data["base_url"] = f"http://127.0.0.1:{fake_gateway.port}/v1"
    return parse_config(config_data)
