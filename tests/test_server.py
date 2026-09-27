"""server 模块：tools/list 定义、两个工具的调用、并发锁与命令行入口。

MCP 侧的用例都经真实的 stdio 传输起一个子进程，跑的是 `[project.scripts]` 装出来的
`pixelgate`；子进程的 HOME 显式指向假 HOME，配置指向回环上的假网关，所以它取的是合成
key、连的是假服务。
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
import pytest
from conftest import CLIENT_KEY, FakeGateway
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import LATEST_PROTOCOL_VERSION

from pixelgate.schemas import (
    EditRequest,
    GenerateRequest,
    ImageRef,
    ReceiptSummary,
    parse_config,
)
from pixelgate.server import (
    REQUIRES_USER_INTERACTION,
    SERVER_NAME,
    TOOL_EDIT,
    TOOL_GENERATE,
    build_server,
    build_tools,
)

#: 装在本环境里的入口脚本，等同于 `uv run --locked --group dev pixelgate`，
#: 只是省掉每次派发时 uv 重新解析环境的开销。
SCRIPT = Path(sys.executable).parent / "pixelgate"
PROMPT = "一张示意图"
EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.json"


@pytest.fixture
def server_config(
    tmp_path: Path, config_data: dict[str, Any], fake_gateway: FakeGateway
) -> Path:
    """指向假网关的配置文件，供子进程用。"""
    config_data["base_url"] = f"http://127.0.0.1:{fake_gateway.port}/v1"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    return path


@pytest.fixture
def server_params(server_config: Path, fake_home: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=str(SCRIPT),
        args=["--config", str(server_config)],
        env={"HOME": str(fake_home)},
    )


async def _list_tools(params: StdioServerParameters) -> types.ListToolsResult:
    async with Client(params) as client:
        return await client.list_tools()


async def _call_tool(
    params: StdioServerParameters, name: str, arguments: dict[str, Any]
) -> types.CallToolResult:
    async with Client(params) as client:
        return await client.call_tool(name, arguments)


async def _call_twice(
    params: StdioServerParameters,
    name: str,
    arguments: dict[str, Any],
    delay: float,
) -> dict[int, types.CallToolResult]:
    """同一会话里并发发两次调用，第二次在第一次还没返回时进去。"""
    results: dict[int, types.CallToolResult] = {}
    async with Client(params) as client:

        async def one(index: int) -> None:
            results[index] = await client.call_tool(name, arguments)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(one, 0)
            await anyio.sleep(delay)
            task_group.start_soon(one, 1)
    return results


def run_cli(args: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "HOME": str(home)},
    )


def text_blocks(result: types.CallToolResult) -> list[types.TextContent]:
    return [item for item in result.content if isinstance(item, types.TextContent)]


def image_blocks(result: types.CallToolResult) -> list[types.ImageContent]:
    return [item for item in result.content if isinstance(item, types.ImageContent)]


def summary_of(result: types.CallToolResult) -> ReceiptSummary:
    return ReceiptSummary.model_validate(result.structured_content)


# --- 服务名 ---


def test_build_server_names_the_service_pixelgate(
    config_data: dict[str, Any],
) -> None:
    """不起子进程，直接看构建出的服务在 initialize 里报的名字。"""
    server = build_server(parse_config(config_data))
    assert SERVER_NAME == "pixelgate"
    assert server.create_initialization_options().server_name == "pixelgate"


# --- tools/list ---


def test_tools_list_has_exactly_the_two_tools(
    server_params: StdioServerParameters,
) -> None:
    listed = anyio.run(_list_tools, server_params)
    assert [tool.name for tool in listed.tools] == [TOOL_GENERATE, TOOL_EDIT]


def test_every_tool_requires_user_interaction(
    server_params: StdioServerParameters,
) -> None:
    listed = anyio.run(_list_tools, server_params)
    for tool in listed.tools:
        assert tool.meta is not None
        # 值必须是 JSON 布尔真，别的取值宿主会忽略。
        assert tool.meta[REQUIRES_USER_INTERACTION] is True


def test_tool_schemas_come_from_the_models(
    server_params: StdioServerParameters,
) -> None:
    listed = anyio.run(_list_tools, server_params)
    schemas = {tool.name: tool for tool in listed.tools}
    assert schemas[TOOL_GENERATE].input_schema == GenerateRequest.model_json_schema()
    assert schemas[TOOL_EDIT].input_schema == EditRequest.model_json_schema()
    for tool in listed.tools:
        assert tool.output_schema == ReceiptSummary.model_json_schema()


def test_tool_input_schemas_keep_their_local_refs_resolvable() -> None:
    """嵌套模型在 schema 里是 `$defs` 加本地 `$ref`，核对每个引用都能在本文档内解析。"""
    for tool in build_tools():
        definitions = tool.input_schema.get("$defs", {})
        assert "ImageRef" in definitions
        for ref in _collect_refs(tool.input_schema):
            assert ref.startswith("#/$defs/")
            assert ref.removeprefix("#/$defs/") in definitions


def _collect_refs(node: object) -> list[str]:
    if isinstance(node, dict):
        found: list[str] = []
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_refs(value))
        return found
    if isinstance(node, list):
        return [ref for item in node for ref in _collect_refs(item)]
    return []


def test_tool_descriptions_are_written_for_the_caller() -> None:
    """工具描述是自己写的一句话，不是模型 docstring 顺带渗进来的那句。"""
    for tool in build_tools():
        assert tool.description is not None
        assert "inputSchema" not in tool.description
        assert "JSON schema" not in tool.description


def test_schema_descriptions_do_not_talk_about_the_schema() -> None:
    """pydantic 把模型 docstring 放进 schema 的 description，它会随 schema 送到调用方。

    所以那句话也得是写给调用方的：不复述「本模型的 schema 就是 inputSchema」这类维护者
    才关心的事实。
    """
    for tool in build_tools():
        for schema in (tool.input_schema, tool.output_schema or {}):
            description = schema.get("description", "")
            assert "inputSchema" not in description
            assert "outputSchema" not in description


# --- 两个工具的成功调用 ---


def test_generate_image_returns_summary_text_and_preview(
    server_params: StdioServerParameters,
    fake_gateway: FakeGateway,
    tmp_output_dir: Path,
) -> None:
    arguments = {"prompt": PROMPT, "output_dir": str(tmp_output_dir)}
    result = anyio.run(_call_tool, server_params, TOOL_GENERATE, arguments)
    assert result.is_error is False
    summary = summary_of(result)
    assert summary.state == "completed"
    assert summary.operation == "generate"
    assert summary.output is not None
    assert len(text_blocks(result)) == 1
    [image] = image_blocks(result)
    assert image.mime_type == "image/jpeg"
    assert fake_gateway.count == 1


def test_generate_image_leaves_the_artifact_set_on_disk(
    server_params: StdioServerParameters, tmp_output_dir: Path
) -> None:
    arguments = {"prompt": PROMPT, "output_dir": str(tmp_output_dir)}
    result = anyio.run(_call_tool, server_params, TOOL_GENERATE, arguments)
    summary = summary_of(result)
    assert summary.artifact_dir is not None
    directory = Path(summary.artifact_dir)
    assert {item.name for item in directory.iterdir()} == {
        "prompt.txt",
        "request.json",
        "receipt.json",
        "image.png",
        "preview.jpg",
    }


def test_edit_image_returns_the_same_shape(
    server_params: StdioServerParameters,
    tmp_output_dir: Path,
    make_png: Callable[..., ImageRef],
) -> None:
    parent = make_png(64, 64)
    arguments = {
        "prompt": PROMPT,
        "parent": parent.model_dump(),
        "output_dir": str(tmp_output_dir),
        "mask": make_png(64, 64, "RGBA").model_dump(),
    }
    result = anyio.run(_call_tool, server_params, TOOL_EDIT, arguments)
    assert result.is_error is False
    summary = summary_of(result)
    assert summary.state == "completed"
    assert summary.operation == "edit"
    assert summary.artifact_dir is not None
    inputs = Path(summary.artifact_dir) / "inputs"
    assert {item.name for item in inputs.iterdir()} == {"parent.png", "mask.png"}
    assert len(image_blocks(result)) == 1


def test_the_returned_payload_carries_no_credential(
    server_params: StdioServerParameters, tmp_output_dir: Path
) -> None:
    """凭据不进返回：文本块与 structuredContent 的序列化全文都扫一遍合成 key。"""
    arguments = {"prompt": PROMPT, "output_dir": str(tmp_output_dir)}
    result = anyio.run(_call_tool, server_params, TOOL_GENERATE, arguments)
    assert result.is_error is False
    payload = json.dumps(
        result.model_dump(by_alias=True, mode="json"), ensure_ascii=False
    )
    assert CLIENT_KEY not in payload
    assert "Bearer" not in payload
    for text in text_blocks(result):
        assert CLIENT_KEY not in text.text


def test_successful_text_block_names_the_artifacts(
    server_params: StdioServerParameters, tmp_output_dir: Path
) -> None:
    arguments = {"prompt": PROMPT, "output_dir": str(tmp_output_dir)}
    result = anyio.run(_call_tool, server_params, TOOL_GENERATE, arguments)
    summary = summary_of(result)
    assert summary.output is not None
    [text] = text_blocks(result)
    assert summary.output.path in text.text
    assert "completed" in text.text
    assert PROMPT not in text.text


# --- 失败路径 ---


def test_upstream_failure_is_reported_as_a_tool_error(
    server_params: StdioServerParameters,
    fake_gateway: FakeGateway,
    tmp_output_dir: Path,
) -> None:
    fake_gateway.mode = "status_5xx"
    arguments = {"prompt": PROMPT, "output_dir": str(tmp_output_dir)}
    result = anyio.run(_call_tool, server_params, TOOL_GENERATE, arguments)
    assert result.is_error is True
    summary = summary_of(result)
    assert summary.state == "failed"
    assert summary.error is not None
    assert summary.error.category == "upstream_error"
    assert summary.error.http_status == 503
    [text] = text_blocks(result)
    assert "upstream_error" in text.text
    assert PROMPT not in text.text
    # 上游响应体的原文不进返回。
    assert "上游暂时不可用" not in text.text
    assert image_blocks(result) == []


def test_invalid_arguments_are_reported_without_reaching_the_gateway(
    server_params: StdioServerParameters,
    fake_gateway: FakeGateway,
    tmp_output_dir: Path,
) -> None:
    arguments = {"prompt": "", "output_dir": str(tmp_output_dir)}
    result = anyio.run(_call_tool, server_params, TOOL_GENERATE, arguments)
    assert result.is_error is True
    summary = summary_of(result)
    assert summary.error is not None
    assert summary.error.category == "request_invalid"
    assert summary.artifact_dir is None
    assert fake_gateway.count == 0


def test_an_unknown_tool_name_is_reported_without_a_receipt(
    server_params: StdioServerParameters, fake_gateway: FakeGateway
) -> None:
    """未知工具没有 operation、也没有回执可言，所以只回文本，不编造一份摘要。"""
    result = anyio.run(_call_tool, server_params, "paint_image", {})
    assert result.is_error is True
    assert result.structured_content is None
    [text] = text_blocks(result)
    assert "paint_image" in text.text
    assert fake_gateway.count == 0


# --- 并发 ---


def test_a_second_concurrent_call_is_refused_as_busy(
    server_params: StdioServerParameters,
    fake_gateway: FakeGateway,
    tmp_output_dir: Path,
) -> None:
    fake_gateway.mode = "sleep"
    fake_gateway.sleep_seconds = 3.0
    arguments = {"prompt": PROMPT, "output_dir": str(tmp_output_dir)}
    results = anyio.run(_call_twice, server_params, TOOL_GENERATE, arguments, 0.8)
    first, second = results[0], results[1]
    assert first.is_error is False
    assert summary_of(first).state == "completed"
    assert second.is_error is True
    refused = summary_of(second)
    assert refused.error is not None
    assert refused.error.category == "busy"
    assert refused.artifact_dir is None
    # 被拒的那一次一个请求都不发，网关只收到第一次的。
    assert fake_gateway.count == 1


# --- 命令行入口 ---


def test_help_exits_zero(fake_home: Path) -> None:
    completed = run_cli(["--help"], fake_home)
    assert completed.returncode == 0
    assert "check-config" in completed.stdout


def test_missing_config_option_exits_two(fake_home: Path) -> None:
    completed = run_cli([], fake_home)
    assert completed.returncode == 2


def test_check_config_prints_ok(server_config: Path, fake_home: Path) -> None:
    completed = run_cli(["check-config", "--config", str(server_config)], fake_home)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"


def test_check_config_accepts_the_shipped_example(
    tmp_path: Path, key_helper: Path, fake_home: Path
) -> None:
    """样例里只有 helper 路径是占位值；换成替身后，其余字段原样通过校验。"""
    data: dict[str, Any] = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["client_key_helper"] = str(key_helper)
    path = tmp_path / "example.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    completed = run_cli(["check-config", "--config", str(path)], fake_home)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"


def test_check_config_rejects_an_unknown_field(
    tmp_path: Path, config_data: dict[str, Any], fake_home: Path
) -> None:
    config_data["api_key"] = "test-client-key"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    completed = run_cli(["check-config", "--config", str(path)], fake_home)
    assert completed.returncode == 2
    assert "config_invalid" in completed.stderr
    assert completed.stdout.strip() == ""


def test_serving_with_an_invalid_config_exits_two(
    tmp_path: Path, config_data: dict[str, Any], fake_home: Path
) -> None:
    config_data["api_key"] = "test-client-key"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(config_data), encoding="utf-8")
    completed = run_cli(["--config", str(path)], fake_home)
    assert completed.returncode == 2
    assert "config_invalid" in completed.stderr


def test_missing_config_file_exits_two(tmp_path: Path, fake_home: Path) -> None:
    completed = run_cli(["--config", str(tmp_path / "absent.json")], fake_home)
    assert completed.returncode == 2
    assert "config_invalid" in completed.stderr


def test_stdout_carries_only_json_rpc_frames(
    server_config: Path, fake_home: Path
) -> None:
    """服务模式下 stdout 逐行都是可解析的 JSON，诊断信息只能走 stderr。"""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "stdout-probe", "version": "0"},
        },
    }
    completed = subprocess.run(
        [str(SCRIPT), "--config", str(server_config)],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "HOME": str(fake_home)},
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, "服务模式下 stdout 应至少有一帧 initialize 响应"
    for line in lines:
        assert isinstance(json.loads(line), dict)
