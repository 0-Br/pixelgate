"""client 模块：配置读取、凭据 helper、目标守卫、传输保护与两条调用全流程。

本文件的一切请求都发往夹具起在回环上的假服务，端口由内核分配；没有任何用例会连到真实
网关或外网。
"""

import hashlib
import io
import json
import logging
import os
import random
import socket
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import openai
import pytest
from conftest import CLIENT_KEY, KEY_FILE, MIB, FakeGateway, png_bytes
from PIL import Image

from pixelgate import artifacts, client
from pixelgate.client import (
    CLEARED_ENV_VARS,
    HELPER_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    RESPONSE_MAX_BYTES,
    STATE_BY_CATEGORY,
    GuardedTransport,
    build_client,
    load_config,
    read_client_key,
    run_edit,
    run_generate,
)
from pixelgate.schemas import (
    Config,
    EditRequest,
    ErrorCategory,
    GenerateRequest,
    ImageRef,
    ToolError,
    parse_config,
)

PROMPT = "一张示意图"
SUNBURST = "gpt-image-2.5-sunburst"


class _ChunkStream(httpx2.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


class _ChunkTransport(httpx2.BaseTransport):
    """只吐固定块的内层传输，用来精确压响应体积的边界。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"x-request-id": "stub-req"},
            stream=_ChunkStream(self._chunks),
            request=request,
        )


def image_request(url: str) -> httpx2.Request:
    return httpx2.Request("POST", url)


def noise_png(size: int, seed: int = 20260913) -> bytes:
    """高熵 PNG：缩到最小一档预览仍然超预算，用来触发 preview_omitted。

    字节取固定种子的伪随机序列而不是 os.urandom：判据贴着体积预算的边界，用真随机数据
    会让同一份代码在不同轮次落到相邻档位，绿得不稳定。
    """
    image = Image.frombytes(
        "RGB", (size, size), random.Random(seed).randbytes(size * size * 3)
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=0)
    return buffer.getvalue()


def artifact_dir_of(receipt_dir: str | None) -> Path:
    assert receipt_dir is not None
    return Path(receipt_dir)


def _trap_environment(trap: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> str:
    """把六个代理变量指向陷阱并清空两种写法的 no_proxy，返回陷阱地址。"""
    address = f"http://127.0.0.1:{trap.port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, address)
        monkeypatch.setenv(name.lower(), address)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    return address


# --- 配置读取 ---


def test_load_config_reads_the_example(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    assert load_config(path).port == 8317


def test_load_config_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as exc_info:
        load_config(tmp_path / "absent.json")
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_load_config_reports_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(ToolError) as exc_info:
        load_config(path)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_load_config_reports_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as exc_info:
        load_config(tmp_path)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


# --- 凭据 helper ---


def test_read_client_key_returns_the_key(
    fake_home: Path, config_data: dict[str, object]
) -> None:
    assert fake_home.is_dir()
    assert read_client_key(parse_config(config_data)) == CLIENT_KEY


def test_read_client_key_reports_a_nonzero_exit(
    fake_home: Path, config_data: dict[str, object]
) -> None:
    """key 文件不在时替身 helper 的 cat 以非零码退出。"""
    (fake_home / KEY_FILE).unlink()
    with pytest.raises(ToolError) as exc_info:
        read_client_key(parse_config(config_data))
    assert exc_info.value.category is ErrorCategory.AUTH_HELPER_FAILED


def test_read_client_key_reports_a_malformed_key(
    fake_home: Path, config_data: dict[str, object]
) -> None:
    """helper 正常退出，但输出不是一行 64 位小写十六进制。"""
    key_file = fake_home / KEY_FILE
    key_file.write_text("不是十六进制\n", encoding="utf-8")
    key_file.chmod(0o600)
    with pytest.raises(ToolError) as exc_info:
        read_client_key(parse_config(config_data))
    assert exc_info.value.category is ErrorCategory.AUTH_HELPER_FAILED


def test_read_client_key_reports_a_timeout(
    tmp_path: Path, config_data: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "slow-helper.sh"
    helper.write_text("#!/usr/bin/env bash\nsleep 10\n", encoding="utf-8")
    helper.chmod(0o755)
    config_data["client_key_helper"] = str(helper)
    monkeypatch.setattr(client, "HELPER_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(ToolError) as exc_info:
        read_client_key(parse_config(config_data))
    assert exc_info.value.category is ErrorCategory.AUTH_HELPER_FAILED


def test_helper_stderr_is_redacted_before_truncation(
    tmp_path: Path,
    config_data: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """helper 把一枚 key 打到 stderr、且让它横跨截断边界时，日志里不留它的任何长前缀。

    脱敏必须发生在截断之前：先切再脱敏的话，落在切片里的那一段不足 64 位、匹配不上正则，
    会原样进日志，而这个洞恰好在「写坏了的 helper」这一个用场上。
    """
    leaked = "ab12cd34" * 8
    assert len(leaked) == 64
    padding = "x" * 150
    helper = tmp_path / "leaky-helper.sh"
    helper.write_text(
        f'#!/usr/bin/env bash\nprintf "{padding}{leaked}" >&2\nexit 1\n',
        encoding="utf-8",
    )
    helper.chmod(0o755)
    config_data["client_key_helper"] = str(helper)
    with (
        caplog.at_level(logging.ERROR, logger="pixelgate.client"),
        pytest.raises(ToolError) as exc_info,
    ):
        read_client_key(parse_config(config_data))
    assert exc_info.value.category is ErrorCategory.AUTH_HELPER_FAILED
    recorded = caplog.text
    assert leaked not in recorded
    assert leaked[:32] not in recorded
    assert "<redacted>" in recorded


def test_helper_timeout_is_the_documented_value() -> None:
    assert HELPER_TIMEOUT_SECONDS == 5


def test_read_client_key_passes_no_arguments(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    record = tmp_path / "argv.txt"
    helper = tmp_path / "echo-helper.sh"
    helper.write_text(
        f'#!/usr/bin/env bash\nprintf "%s" "$*" > {record}\nprintf "{CLIENT_KEY}\\n"\n',
        encoding="utf-8",
    )
    helper.chmod(0o755)
    config_data["client_key_helper"] = str(helper)
    assert read_client_key(parse_config(config_data)) == CLIENT_KEY
    assert record.read_text(encoding="utf-8") == ""


# --- 目标守卫与传输保护 ---


def test_transport_allows_the_two_image_paths(gateway_config: Config) -> None:
    transport = GuardedTransport(gateway_config.port, inner=_ChunkTransport([b"ok"]))
    for path in ("/v1/images/generations", "/v1/images/edits"):
        url = f"http://127.0.0.1:{gateway_config.port}{path}"
        assert transport.handle_request(image_request(url)).status_code == 200


def test_transport_denies_another_port(
    gateway_config: Config, decoy_gateway: FakeGateway
) -> None:
    transport = GuardedTransport(gateway_config.port)
    url = f"http://127.0.0.1:{decoy_gateway.port}/v1/images/generations"
    with pytest.raises(ToolError) as exc_info:
        transport.handle_request(image_request(url))
    assert exc_info.value.category is ErrorCategory.TARGET_DENIED
    assert decoy_gateway.count == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:{port}/v1/images/generations",
        "http://127.0.0.2:{port}/v1/images/generations",
        "http://localhost:{port}/v1/images/generations",
        "http://127.0.0.1:{port}/v1/models",
        "http://127.0.0.1:{port}/v1/images/generations/extra",
        "http://127.0.0.1:{port}/",
    ],
)
def test_transport_denies_other_targets(gateway_config: Config, url: str) -> None:
    transport = GuardedTransport(gateway_config.port)
    with pytest.raises(ToolError) as exc_info:
        transport.handle_request(image_request(url.format(port=gateway_config.port)))
    assert exc_info.value.category is ErrorCategory.TARGET_DENIED


def test_transport_records_the_upstream_request_id(gateway_config: Config) -> None:
    transport = GuardedTransport(gateway_config.port, inner=_ChunkTransport([b"ok"]))
    url = f"http://127.0.0.1:{gateway_config.port}/v1/images/generations"
    transport.handle_request(image_request(url)).read()
    assert transport.last_request_id == "stub-req"


def test_transport_passes_a_response_at_the_budget(gateway_config: Config) -> None:
    chunk = b"x" * MIB
    inner = _ChunkTransport([chunk] * 64)
    transport = GuardedTransport(gateway_config.port, inner=inner)
    url = f"http://127.0.0.1:{gateway_config.port}/v1/images/generations"
    response = transport.handle_request(image_request(url))
    assert len(response.read()) == RESPONSE_MAX_BYTES
    assert transport.bytes_read == RESPONSE_MAX_BYTES


def test_transport_aborts_one_byte_over_the_budget(gateway_config: Config) -> None:
    chunk = b"x" * MIB
    inner = _ChunkTransport([chunk] * 64 + [b"x"])
    transport = GuardedTransport(gateway_config.port, inner=inner)
    url = f"http://127.0.0.1:{gateway_config.port}/v1/images/generations"
    response = transport.handle_request(image_request(url))
    with pytest.raises(ToolError) as exc_info:
        response.read()
    assert exc_info.value.category is ErrorCategory.RESPONSE_TOO_LARGE
    assert transport.bytes_read <= RESPONSE_MAX_BYTES + MIB


def test_response_budget_is_the_documented_value() -> None:
    assert RESPONSE_MAX_BYTES == 64 * MIB


# --- 客户端构造与环境清理 ---


def test_build_client_targets_the_configured_gateway(gateway_config: Config) -> None:
    api, transport = build_client(gateway_config, CLIENT_KEY)
    assert str(api.base_url).startswith(gateway_config.base_url)
    assert api.max_retries == 0
    assert isinstance(transport, GuardedTransport)


def test_build_client_restores_the_environment(
    gateway_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in CLEARED_ENV_VARS:
        monkeypatch.setenv(name, "哨兵")
    build_client(gateway_config, CLIENT_KEY)
    assert all(os.environ.get(name) == "哨兵" for name in CLEARED_ENV_VARS)


def test_the_http_client_is_built_inside_the_cleared_window(
    gateway_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx 客户端也要在环境被摘掉的窗口里构造，不然清代理变量对它根本不生效。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Sentinel: leak")
    watched = ("HTTP_PROXY", "OPENAI_CUSTOM_HEADERS")
    seen: dict[str, str | None] = {}

    # 用子类而不是替身函数：SDK 会对传进去的 http_client 做 isinstance 检查，换成函数
    # 会让那个检查当场报 TypeError，测出来的就不是本用例要测的东西了。
    class RecordingClient(httpx2.Client):
        def __init__(
            self,
            *,
            trust_env: bool,
            follow_redirects: bool,
            transport: httpx2.BaseTransport,
            timeout: float,
        ) -> None:
            seen.update({name: os.environ.get(name) for name in watched})
            super().__init__(
                trust_env=trust_env,
                follow_redirects=follow_redirects,
                transport=transport,
                timeout=timeout,
            )

    monkeypatch.setattr(httpx2, "Client", RecordingClient)
    build_client(gateway_config, CLIENT_KEY)
    assert seen == {"HTTP_PROXY": None, "OPENAI_CUSTOM_HEADERS": None}


def test_cleared_env_vars_cover_both_cases() -> None:
    assert "OPENAI_CUSTOM_HEADERS" in CLEARED_ENV_VARS
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        assert name in CLEARED_ENV_VARS
        assert name.upper() in CLEARED_ENV_VARS


# --- 生成的成功路径 ---


def test_run_generate_sends_one_request_with_the_fixed_fields(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    request = GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    receipt = run_generate(gateway_config, request)
    assert receipt.state == "completed"
    assert fake_gateway.count == 1
    recorded = fake_gateway.last
    assert recorded.path == "/v1/images/generations"
    assert recorded.body is not None
    assert recorded.body["model"] == SUNBURST
    assert recorded.body["n"] == 1
    assert recorded.body["output_format"] == "png"
    assert recorded.body["stream"] is False
    assert recorded.body["background"] == "auto"
    assert recorded.body["prompt"] == PROMPT


def test_run_generate_sends_a_transparent_background_and_records_it(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    request = GenerateRequest(
        prompt=PROMPT, output_dir=str(tmp_output_dir), background="transparent"
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.state == "completed"
    assert receipt.requested_background == "transparent"
    assert fake_gateway.last.body is not None
    assert fake_gateway.last.body["background"] == "transparent"
    assert receipt.artifact_dir is not None
    record = json.loads(
        (Path(receipt.artifact_dir) / "request.json").read_text(encoding="utf-8")
    )
    assert record["background"] == "transparent"


def test_run_generate_sends_the_route_name_not_the_alias(
    config_data: dict[str, object],
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    routes = config_data["model_routes"]
    assert isinstance(routes, dict)
    routes[SUNBURST] = "gateway-sunburst"
    config_data["base_url"] = f"http://127.0.0.1:{fake_gateway.port}/v1"
    config = parse_config(config_data)
    receipt = run_generate(
        config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert fake_gateway.last.body is not None
    assert fake_gateway.last.body["model"] == "gateway-sunburst"
    assert receipt.requested_model == SUNBURST
    assert receipt.route_model == "gateway-sunburst"


def test_run_generate_carries_the_bearer_key(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert fake_gateway.last.headers["authorization"] == f"Bearer {CLIENT_KEY}"


def test_run_generate_writes_the_artifact_set(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    assert fake_gateway.count == 0
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    directory = artifact_dir_of(receipt.artifact_dir)
    assert {item.name for item in directory.iterdir()} == {
        "prompt.txt",
        "request.json",
        "receipt.json",
        "image.png",
        "preview.jpg",
    }
    assert (directory / "prompt.txt").read_text(encoding="utf-8") == PROMPT


def test_run_generate_records_the_landed_image(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.image_bytes = png_bytes(80, 48)
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.output is not None
    landed = Path(receipt.output.path)
    assert landed.read_bytes() == fake_gateway.image_bytes
    assert receipt.output.sha256 == hashlib.sha256(landed.read_bytes()).hexdigest()
    assert (receipt.output.width, receipt.output.height) == (80, 48)
    assert receipt.output.bytes == landed.stat().st_size


def test_run_generate_records_the_upstream_side_facts(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.actual_model == SUNBURST
    assert receipt.upstream_request_id == "test-req-1"
    assert receipt.revised_prompt == "上游回写的提示词"
    assert receipt.usage is not None
    assert receipt.usage.model_dump() == {
        "input_tokens": 11,
        "output_tokens": 22,
        "total_tokens": 33,
    }


def test_run_generate_leaves_upstream_facts_null_when_absent(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.model = None
    fake_gateway.usage = None
    fake_gateway.revised_prompt = None
    fake_gateway.request_id = None
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.state == "completed"
    assert receipt.actual_model is None
    assert receipt.usage is None
    assert receipt.revised_prompt is None
    assert receipt.upstream_request_id is None


def test_run_generate_flags_a_size_mismatch(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.image_bytes = png_bytes(64, 64)
    request = GenerateRequest(
        prompt=PROMPT, output_dir=str(tmp_output_dir), size="1024x1024"
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.state == "completed"
    assert receipt.size_mismatch is True
    assert receipt.warnings == ["size_mismatch"]


def test_run_generate_reports_an_omitted_preview(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    """预览三档都超预算时，receipt 记 preview_omitted，图片本身照常落盘。"""
    assert fake_home.is_dir()
    fake_gateway.image_bytes = noise_png(512)
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.state == "completed"
    assert receipt.preview is None
    assert receipt.warnings == ["preview_omitted"]
    directory = artifact_dir_of(receipt.artifact_dir)
    assert (directory / "image.png").exists()
    assert not (directory / "preview.jpg").exists()


def test_run_generate_keeps_the_request_record_free_of_secrets(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    directory = artifact_dir_of(receipt.artifact_dir)
    recorded = (directory / "request.json").read_text(encoding="utf-8")
    assert CLIENT_KEY not in recorded
    assert PROMPT not in recorded
    assert json.loads(recorded)["n"] == 1
    assert CLIENT_KEY not in (directory / "receipt.json").read_text(encoding="utf-8")


def test_run_generate_with_references_uses_the_edit_endpoint(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    make_png: Callable[..., ImageRef],
) -> None:
    assert fake_home.is_dir()
    reference = make_png(32, 32)
    request = GenerateRequest(
        prompt=PROMPT, output_dir=str(tmp_output_dir), references=[reference]
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.state == "completed"
    assert receipt.parent is None
    assert fake_gateway.last.path == "/v1/images/edits"
    assert fake_gateway.last.fields["image[]"] == [Path(reference.path).read_bytes()]


# --- 编辑的成功路径 ---


def test_run_edit_sends_parent_first_then_references(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    make_png: Callable[..., ImageRef],
) -> None:
    assert fake_home.is_dir()
    parent = make_png(64, 64)
    reference = make_png(32, 32)
    mask = make_png(64, 64, "RGBA")
    request = EditRequest(
        prompt=PROMPT,
        parent=parent,
        output_dir=str(tmp_output_dir),
        references=[reference],
        mask=mask,
    )
    receipt = run_edit(gateway_config, request)
    assert receipt.state == "completed"
    assert receipt.operation == "edit"
    assert receipt.parent == parent
    recorded = fake_gateway.last
    assert recorded.path == "/v1/images/edits"
    assert recorded.fields["image[]"] == [
        Path(parent.path).read_bytes(),
        Path(reference.path).read_bytes(),
    ]
    assert recorded.fields["mask"] == [Path(mask.path).read_bytes()]
    assert recorded.fields["model"] == [SUNBURST.encode()]
    assert recorded.fields["n"] == [b"1"]
    assert recorded.fields["output_format"] == [b"png"]


def test_run_edit_snapshots_the_inputs_into_the_artifact_dir(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    make_png: Callable[..., ImageRef],
) -> None:
    assert fake_home.is_dir()
    parent = make_png(64, 64)
    request = EditRequest(
        prompt=PROMPT,
        parent=parent,
        output_dir=str(tmp_output_dir),
        references=[make_png(32, 32)],
        mask=make_png(64, 64, "RGBA"),
    )
    receipt = run_edit(gateway_config, request)
    inputs = artifact_dir_of(receipt.artifact_dir) / "inputs"
    assert {item.name for item in inputs.iterdir()} == {
        "parent.png",
        "ref-1.png",
        "mask.png",
    }
    assert (inputs / "parent.png").read_bytes() == Path(parent.path).read_bytes()


# --- 预检失败：一个请求都不发 ---


def test_run_edit_rejects_a_mask_that_does_not_match(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    make_png: Callable[..., ImageRef],
) -> None:
    assert fake_home.is_dir()
    request = EditRequest(
        prompt=PROMPT,
        parent=make_png(64, 64),
        output_dir=str(tmp_output_dir),
        mask=make_png(32, 32, "RGBA"),
    )
    receipt = run_edit(gateway_config, request)
    assert receipt.state == "failed"
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.MASK_MISMATCH
    assert fake_gateway.count == 0
    assert list(tmp_output_dir.iterdir()) == []


def test_run_generate_rejects_a_missing_reference(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    tmp_path: Path,
) -> None:
    assert fake_home.is_dir()
    ref = ImageRef(path=str(tmp_path / "absent.png"), sha256="a3f1" * 16)
    request = GenerateRequest(
        prompt=PROMPT, output_dir=str(tmp_output_dir), references=[ref]
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.INPUT_MISSING
    assert receipt.state == "failed"
    assert fake_gateway.count == 0


def test_run_generate_rejects_a_lying_hash(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    make_png: Callable[..., ImageRef],
) -> None:
    assert fake_home.is_dir()
    real = make_png()
    request = GenerateRequest(
        prompt=PROMPT,
        output_dir=str(tmp_output_dir),
        references=[ImageRef(path=real.path, sha256="a3f1" * 16)],
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.INPUT_HASH_MISMATCH
    assert fake_gateway.count == 0


def test_run_generate_rejects_a_broken_reference(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    write_image: Callable[[bytes], ImageRef],
) -> None:
    assert fake_home.is_dir()
    ref = write_image(png_bytes()[:40] + os.urandom(200))
    request = GenerateRequest(
        prompt=PROMPT, output_dir=str(tmp_output_dir), references=[ref]
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.INPUT_UNDECODABLE
    assert fake_gateway.count == 0


def test_run_generate_rejects_an_oversized_reference(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    write_image: Callable[[bytes], ImageRef],
) -> None:
    assert fake_home.is_dir()
    ref = write_image(png_bytes(8208, 16))
    request = GenerateRequest(
        prompt=PROMPT, output_dir=str(tmp_output_dir), references=[ref]
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.INPUT_TOO_LARGE
    assert fake_gateway.count == 0


@pytest.mark.parametrize(
    ("field", "value", "category"),
    [
        ("model", "gpt-image-2.5", ErrorCategory.MODEL_NOT_ALLOWED),
        ("size", "1000x1000", ErrorCategory.SIZE_INVALID),
        ("size", "3840x2160", ErrorCategory.SIZE_EXPERIMENTAL),
        ("quality", "ultra", ErrorCategory.QUALITY_INVALID),
        ("background", "checkerboard", ErrorCategory.BACKGROUND_INVALID),
    ],
)
def test_run_generate_rejects_out_of_domain_parameters(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    field: str,
    value: str,
    category: ErrorCategory,
) -> None:
    assert fake_home.is_dir()
    request = GenerateRequest.model_validate(
        {"prompt": PROMPT, "output_dir": str(tmp_output_dir), field: value}
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.error is not None
    assert receipt.error.category is category
    assert receipt.state == "failed"
    assert fake_gateway.count == 0


def test_run_generate_reports_a_failing_helper(
    config_data: dict[str, object],
    fake_gateway: FakeGateway,
    tmp_output_dir: Path,
    tmp_path: Path,
) -> None:
    helper = tmp_path / "broken-helper.sh"
    helper.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
    helper.chmod(0o755)
    config_data["client_key_helper"] = str(helper)
    config_data["base_url"] = f"http://127.0.0.1:{fake_gateway.port}/v1"
    receipt = run_generate(
        parse_config(config_data),
        GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir)),
    )
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.AUTH_HELPER_FAILED
    assert fake_gateway.count == 0
    assert list(tmp_output_dir.iterdir()) == []


def test_run_generate_reports_a_directory_collision(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fake_home.is_dir()
    monkeypatch.setattr(artifacts, "_artifact_dir_name", lambda: "fixed-name")
    (tmp_output_dir / "fixed-name").mkdir()
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.ARTIFACT_COLLISION
    assert receipt.state == "failed"
    assert fake_gateway.count == 0


# --- 上游异常 ---


def test_run_generate_reports_an_upstream_status_error(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.mode = "status_5xx"
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert fake_gateway.count == 1
    assert receipt.state == "failed"
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.UPSTREAM_ERROR
    assert receipt.error.http_status == 503


def test_run_generate_reports_a_broken_stream_as_an_interrupted_upstream(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    """请求已送达、响应读到一半断开：上游可能已经生成并扣量，所以结果未知。"""
    assert fake_home.is_dir()
    fake_gateway.mode = "drop_mid_body"
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert fake_gateway.count == 1
    assert receipt.state == "unknown"
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.UPSTREAM_INTERRUPTED


def test_a_closed_port_is_reported_as_gateway_unreachable(
    config_data: dict[str, object],
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    """连都没连上：请求一个字节都没发出去，上游必定什么都没发生，所以是确定的失败。"""
    assert fake_home.is_dir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    config_data["base_url"] = f"http://127.0.0.1:{closed_port}/v1"
    receipt = run_generate(
        parse_config(config_data),
        GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir)),
    )
    assert receipt.state == "failed"
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.GATEWAY_UNREACHABLE
    assert fake_gateway.count == 0


def test_run_generate_reports_a_timeout_as_unknown(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.mode = "sleep"
    fake_gateway.sleep_seconds = 2.0
    monkeypatch.setattr(client, "REQUEST_TIMEOUT_SECONDS", 0.5)
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert fake_gateway.count == 1
    assert receipt.state == "unknown"
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.TIMEOUT


def test_request_timeout_is_the_documented_value() -> None:
    assert REQUEST_TIMEOUT_SECONDS == 600


def test_run_generate_does_not_follow_a_redirect(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    decoy_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.mode = "redirect_302"
    fake_gateway.redirect_to = (
        f"http://127.0.0.1:{decoy_gateway.port}/v1/images/generations"
    )
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert fake_gateway.count == 1
    assert decoy_gateway.count == 0
    assert receipt.state == "failed"
    assert receipt.error is not None
    # 302 原样交给 SDK 处理，客户端既不跟随也不重发，第二个地址一个请求都收不到。
    assert receipt.error.category is ErrorCategory.UPSTREAM_ERROR
    assert receipt.error.http_status == 302


@pytest.mark.parametrize("mode", ["no_data", "two_data"])
def test_run_generate_requires_exactly_one_image(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    mode: str,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.mode = mode
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.state == "failed"
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.UPSTREAM_NO_IMAGE


def test_run_generate_reports_an_undecodable_image(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.mode = "bad_b64"
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.state == "failed"
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.UPSTREAM_BAD_IMAGE


def test_run_generate_aborts_an_oversized_response(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.mode = "oversize_body"
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert fake_gateway.count == 1
    assert receipt.state == "unknown"
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.RESPONSE_TOO_LARGE


# --- 出站路径不被环境劫持 ---


def test_run_generate_ignores_proxy_environment(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    proxy_trap: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fake_home.is_dir()
    _trap_environment(proxy_trap, monkeypatch)
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.state == "completed"
    assert fake_gateway.count == 1
    assert proxy_trap.count == 0


def test_the_proxy_trap_would_catch_an_unprotected_client(
    fake_gateway: FakeGateway,
    proxy_trap: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阳性对照：同一组环境变量下，一个信任环境的客户端确实会被代理劫走。

    没有这条，上一个用例的「陷阱计数为 0」既可能是防护生效，也可能是环境变量没起作用。
    本机平时就带着一套指向别处的代理环境，所以这里逐个变量按同样的方式设置：大小写都设，
    两种写法的 no_proxy 都清空，否则 urllib 会挑到外面那一套、绕过陷阱。
    """
    _trap_environment(proxy_trap, monkeypatch)
    target = f"http://127.0.0.1:{fake_gateway.port}/v1/images/generations"
    with httpx2.Client(trust_env=True) as unprotected:
        unprotected.post(target, json={})
    assert proxy_trap.count == 1
    assert fake_gateway.count == 0


def test_an_unprotected_sdk_client_would_inject_the_sentinel_header(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阳性对照：不清环境时，SDK 确实会把 OPENAI_CUSTOM_HEADERS 注进请求头。"""
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Sentinel: leak")
    transport = GuardedTransport(gateway_config.port)
    with httpx2.Client(trust_env=False, transport=transport) as http_client:
        api = openai.OpenAI(
            base_url=gateway_config.base_url,
            api_key=CLIENT_KEY,
            max_retries=0,
            http_client=http_client,
        )
        with api:
            api.images.generate(
                prompt=PROMPT, model=SUNBURST, n=1, output_format="png", stream=False
            )
    assert fake_gateway.last.headers["x-sentinel"] == "leak"


def test_run_generate_ignores_the_openai_identity_environment(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组织与项目两个变量会被 SDK 读成请求头，构造期一并清掉，不发给本机网关。"""
    assert fake_home.is_dir()
    identity = ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_WEBHOOK_SECRET")
    # 哨兵取 ASCII：头值只能是 ASCII，用中文的话一旦漏清就炸在编码处，看不出是漏清。
    for name in identity:
        assert name in CLEARED_ENV_VARS
        monkeypatch.setenv(name, "sentinel")
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.state == "completed"
    headers = fake_gateway.last.headers
    assert "openai-organization" not in headers
    assert "openai-project" not in headers
    assert "sentinel" not in set(headers.values())
    # 只在构造期摘掉，构造完原样放回：清空的作用域是这一次构造，不是整个进程。
    for name in identity:
        assert os.environ[name] == "sentinel"


def test_an_unprotected_sdk_client_would_send_the_identity_headers(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阳性对照：不清环境时，SDK 确实把组织与项目读成请求头发出去。"""
    monkeypatch.setenv("OPENAI_ORG_ID", "org-sentinel")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-sentinel")
    transport = GuardedTransport(gateway_config.port)
    with httpx2.Client(trust_env=False, transport=transport) as http_client:
        api = openai.OpenAI(
            base_url=gateway_config.base_url,
            api_key=CLIENT_KEY,
            max_retries=0,
            http_client=http_client,
        )
        with api:
            api.images.generate(
                prompt=PROMPT, model=SUNBURST, n=1, output_format="png", stream=False
            )
    assert fake_gateway.last.headers["openai-organization"] == "org-sentinel"
    assert fake_gateway.last.headers["openai-project"] == "proj-sentinel"


def test_run_generate_ignores_openai_environment(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    proxy_trap: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fake_home.is_dir()
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Sentinel: leak")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{proxy_trap.port}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key-unused")
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.state == "completed"
    assert fake_gateway.count == 1
    assert proxy_trap.count == 0
    assert "x-sentinel" not in fake_gateway.last.headers


# --- 权限、落盘与状态表 ---


def test_run_generate_keeps_artifacts_private_under_loose_umask(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    make_png: Callable[..., ImageRef],
    loose_umask: None,
) -> None:
    """带一张参考图跑，产物目录里才会有 inputs/，目录与文件两种权限位都验到。"""
    assert fake_home.is_dir()
    request = GenerateRequest(
        prompt=PROMPT, output_dir=str(tmp_output_dir), references=[make_png()]
    )
    receipt = run_generate(gateway_config, request)
    assert receipt.state == "completed"
    directory = artifact_dir_of(receipt.artifact_dir)
    assert directory.stat().st_mode & 0o777 == 0o700
    inputs = directory / "inputs"
    assert inputs.is_dir()
    assert inputs.stat().st_mode & 0o777 == 0o700
    for item in [*directory.iterdir(), *inputs.iterdir()]:
        expected = 0o700 if item.is_dir() else 0o600
        assert item.stat().st_mode & 0o777 == expected


def test_run_generate_writes_the_final_receipt_to_disk(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    directory = artifact_dir_of(receipt.artifact_dir)
    written = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    assert written["state"] == "completed"
    assert written["request_id"] == receipt.request_id
    assert "artifact_dir" not in written


def test_failed_run_still_leaves_a_receipt_on_disk(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
) -> None:
    assert fake_home.is_dir()
    fake_gateway.mode = "status_5xx"
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    directory = artifact_dir_of(receipt.artifact_dir)
    written = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    assert written["state"] == "failed"
    assert written["error"]["category"] == "upstream_error"
    assert not (directory / "image.png").exists()


def test_a_write_failure_before_the_request_is_reported_as_failed(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """请求发出之前落盘就失败：一个字节都没发出去，所以是确定的失败，不是结果未知。"""
    assert fake_home.is_dir()
    real_create = client.create_artifact_dir
    locked: list[Path] = []

    def create_then_lock(output_dir: Path) -> Path:
        directory = real_create(output_dir)
        locked.append(directory)
        directory.chmod(0o500)
        return directory

    monkeypatch.setattr(client, "create_artifact_dir", create_then_lock)
    try:
        receipt = run_generate(
            gateway_config,
            GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir)),
        )
    finally:
        for directory in locked:
            directory.chmod(0o700)
    assert receipt.error is not None
    assert receipt.error.category is ErrorCategory.ARTIFACT_WRITE_FAILED
    assert receipt.state == "failed"
    assert fake_gateway.count == 0


def test_a_preview_failure_still_completes_with_the_image(
    gateway_config: Config,
    fake_gateway: FakeGateway,
    fake_home: Path,
    tmp_output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """图已经落盘了，预览再失败也不该把这次调用报成没拿到图。"""
    assert fake_home.is_dir()

    def failing_save(*args: object, **kwargs: object) -> None:
        raise OSError("预览写不出去")

    monkeypatch.setattr(Image.Image, "save", failing_save)
    receipt = run_generate(
        gateway_config, GenerateRequest(prompt=PROMPT, output_dir=str(tmp_output_dir))
    )
    assert receipt.state == "completed"
    assert receipt.output is not None
    assert Path(receipt.output.path).is_file()
    assert receipt.preview is None
    assert receipt.warnings == ["preview_omitted"]


def test_state_table_covers_every_error_category() -> None:
    assert set(STATE_BY_CATEGORY) == set(ErrorCategory)
