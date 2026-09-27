"""stdio MCP 服务：两个工具的定义、并发锁与命令行入口。

工具定义里的 `_meta["anthropic/requiresUserInteraction"]` 让宿主每次调用都提示真人批准，
这是本服务唯一的批准门，服务自身不做任何权限判断。

两个工具的实现是阻塞的（单次调用最长 600 秒），所以真正的调用跑在工作线程里，事件循环
留给协议本身；否则第二个调用连进不来，busy 也就无从谈起。同一进程同时只允许一次调用在
跑，锁拿不到就立刻回 busy，不排队。
"""

import argparse
import base64
import logging
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from pixelgate import __version__
from pixelgate.client import load_config, run_edit, run_generate
from pixelgate.schemas import (
    DEFAULT_MODEL,
    Config,
    EditRequest,
    ErrorCategory,
    ErrorInfo,
    GenerateRequest,
    Operation,
    PreviewInfo,
    Receipt,
    ReceiptSummary,
    ToolError,
    field_locations,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "pixelgate"
TOOL_GENERATE = "generate_image"
TOOL_EDIT = "edit_image"
REQUIRES_USER_INTERACTION = "anthropic/requiresUserInteraction"
PREVIEW_MIME = "image/jpeg"

GENERATE_DESCRIPTION = (
    "按提示词生成一张图片。产物写进 output_dir 下新建的目录（图片、预览、提示词、"
    "请求与回执各一份），返回实际路径与回执摘要。给了参考图就以它们为参照生成，"
    "但不声明父版本；要基于某张已有图片改，用 edit_image。"
)
EDIT_DESCRIPTION = (
    "基于指定的父图编辑出一张新图片。父图与可选的遮罩、参考图一起发给上游，原图不被"
    "改写；产物与返回形态同 generate_image，回执里记下父图的路径与摘要。"
)


def _tool_meta() -> dict[str, Any]:
    """每个工具一份独立的 `_meta`，值是 JSON 布尔真。"""
    return {REQUIRES_USER_INTERACTION: True}


def build_tools() -> list[types.Tool]:
    """两个工具的 tools/list 定义。

    入参与返回的 schema 直接取自 pydantic 模型，与运行时的校验器同源；工具描述是另写的
    一句话，写给挑工具的模型看，不复述 schema。
    """
    output_schema = ReceiptSummary.model_json_schema()
    return [
        types.Tool(
            name=TOOL_GENERATE,
            title="生成图片",
            description=GENERATE_DESCRIPTION,
            input_schema=GenerateRequest.model_json_schema(),
            output_schema=output_schema,
            _meta=_tool_meta(),
        ),
        types.Tool(
            name=TOOL_EDIT,
            title="编辑图片",
            description=EDIT_DESCRIPTION,
            input_schema=EditRequest.model_json_schema(),
            output_schema=output_schema,
            _meta=_tool_meta(),
        ),
    ]


def _preview_block(preview: PreviewInfo) -> types.ImageContent | None:
    """把预览读成 image 块；读不出来就退回只给路径，不让它拖垮整次调用。"""
    try:
        data = Path(preview.path).read_bytes()
    except OSError:
        logger.error("预览文件读取失败：%s", preview.path, exc_info=True)
        return None
    return types.ImageContent(
        data=base64.b64encode(data).decode("ascii"), mime_type=PREVIEW_MIME
    )


def _describe(receipt: Receipt) -> str:
    """给调用方的一段文本；不含提示词、图片内容与上游原文。"""
    lines = [f"状态：{receipt.state}", f"本地请求 id：{receipt.request_id}"]
    if receipt.output is not None:
        output = receipt.output
        lines.insert(0, "已生成图片。")
        lines.append(
            f"图片：{output.path}"
            f"（{output.width}x{output.height} {output.format}，{output.bytes} 字节）"
        )
    else:
        lines.insert(0, "调用未取得图片。")
    if receipt.preview is not None:
        lines.append(f"预览：{receipt.preview.path}（最长边 {receipt.preview.edge}）")
    if receipt.error is not None:
        lines.append(f"错误类别：{receipt.error.category.value}")
        lines.append(f"说明：{receipt.error.message_safe}")
        if receipt.error.http_status is not None:
            lines.append(f"HTTP 状态：{receipt.error.http_status}")
    if receipt.warnings:
        lines.append("提示：" + "、".join(item.value for item in receipt.warnings))
    if receipt.artifact_dir is not None:
        lines.append(f"产物目录：{receipt.artifact_dir}")
    return "\n".join(lines)


def _result_of(receipt: Receipt) -> types.CallToolResult:
    """把 receipt 变成工具返回：文本一段、预览一块、摘要一份。"""
    blocks: list[types.ContentBlock] = [types.TextContent(text=_describe(receipt))]
    if receipt.state == "completed" and receipt.preview is not None:
        block = _preview_block(receipt.preview)
        if block is not None:
            blocks.append(block)
    return types.CallToolResult(
        content=blocks,
        structured_content=receipt.summary(receipt.artifact_dir).model_dump(
            mode="json"
        ),
        is_error=receipt.state != "completed",
    )


def _refused_summary(
    operation: Operation, arguments: dict[str, Any], err: ToolError
) -> ReceiptSummary:
    """还没形成一次调用就被拒时的摘要；没有产物目录，也没有任何上游侧事实。"""
    requested = arguments.get("model")
    return ReceiptSummary(
        request_id=str(uuid.uuid4()),
        operation=operation,
        state="failed",
        requested_model=requested if isinstance(requested, str) else DEFAULT_MODEL,
        actual_model=None,
        output=None,
        preview=None,
        size_mismatch=False,
        usage=None,
        error=ErrorInfo(
            category=err.category,
            http_status=err.http_status,
            message_safe=err.message_safe,
        ),
        warnings=[],
        artifact_dir=None,
    )


def _refused(
    operation: Operation, arguments: dict[str, Any], err: ToolError
) -> types.CallToolResult:
    summary = _refused_summary(operation, arguments, err)
    text = "\n".join(
        [
            "调用未开始。",
            f"错误类别：{err.category.value}",
            f"说明：{err.message_safe}",
            f"本地请求 id：{summary.request_id}",
        ]
    )
    return types.CallToolResult(
        content=[types.TextContent(text=text)],
        structured_content=summary.model_dump(mode="json"),
        is_error=True,
    )


def _unknown_tool(name: str) -> types.CallToolResult:
    """未知工具名没有对应的回执，只回一段文本，不编造摘要。"""
    return types.CallToolResult(
        content=[types.TextContent(text=f"没有名为 {name} 的工具。")],
        is_error=True,
    )


def _run_tool(
    config: Config, name: str, arguments: dict[str, Any]
) -> types.CallToolResult:
    """在工作线程里跑完一次调用；入参形态不合法记 `request_invalid`。"""
    operation: Operation = "generate" if name == TOOL_GENERATE else "edit"
    try:
        if name == TOOL_GENERATE:
            receipt = run_generate(config, GenerateRequest.model_validate(arguments))
        else:
            receipt = run_edit(config, EditRequest.model_validate(arguments))
    except ValidationError as err:
        detail = field_locations(err)
        return _refused(
            operation,
            arguments,
            ToolError(ErrorCategory.REQUEST_INVALID, detail=detail),
        )
    return _result_of(receipt)


async def _call(
    config: Config, lock: threading.Lock, name: str, arguments: dict[str, Any]
) -> types.CallToolResult:
    if name not in (TOOL_GENERATE, TOOL_EDIT):
        return _unknown_tool(name)
    operation: Operation = "generate" if name == TOOL_GENERATE else "edit"
    if not lock.acquire(blocking=False):
        return _refused(operation, arguments, ToolError(ErrorCategory.BUSY))
    try:
        # 默认不放弃线程，所以取消时也要等它跑完，锁在 finally 里一定被放开。
        return await anyio.to_thread.run_sync(_run_tool, config, name, arguments)
    finally:
        lock.release()


def build_server(config: Config) -> Server[dict[str, Any]]:
    """装好两个处理器与并发锁的 MCP 服务。"""
    lock = threading.Lock()

    async def on_list_tools(
        ctx: ServerRequestContext[dict[str, Any]],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=build_tools())

    async def on_call_tool(
        ctx: ServerRequestContext[dict[str, Any]],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        return await _call(config, lock, params.name, params.arguments or {})

    return Server(
        SERVER_NAME,
        version=__version__,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def build_parser() -> argparse.ArgumentParser:
    """命令行解析器：`--config` 与 `check-config` 子命令。

    `--config` 在主命令与子命令上都认，所以 `pixelgate --config X` 与
    `pixelgate check-config --config X` 两种写法都成立；两处都没给时报错退出 2。
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        help="配置文件路径，必选",
    )
    parser = argparse.ArgumentParser(
        prog="pixelgate",
        parents=[common],
        description="经本机回环网关调用订阅图像后端的 stdio MCP 服务。",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "check-config",
        parents=[common],
        help="只校验配置：通过打印 ok，不通过退出码 2",
    )
    return parser


async def _serve(config: Config) -> None:
    server = build_server(config)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main() -> None:
    """命令行入口：`--config <path>` 必选，无子命令即起 stdio 服务。"""
    parser = build_parser()
    args = parser.parse_args()
    if args.config is None:
        parser.error("--config 是必选参数")
    try:
        config = load_config(args.config)
    except ToolError as err:
        sys.stderr.write(f"{err.category.value}: {err.message_safe}\n")
        raise SystemExit(2) from err
    if args.command == "check-config":
        sys.stdout.write("ok\n")
        return
    anyio.run(_serve, config)
