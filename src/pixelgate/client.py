"""配置读取、凭据 helper、目标守卫传输与两条单次调用的全流程。

出站路径上有三道互相独立的约束。一是配置层只接受回环 base_url（schemas 的
`_parse_gateway_port`）；二是传输层的 `GuardedTransport` 在建连前逐个核对 scheme、host、
port 与路径，别的目标一律 `target_denied`；三是构造客户端时清掉 `OPENAI_*` 与代理环境
变量，不让环境把请求引到别处。三道都过不去的请求发不出去。

单次调用只发一次 SDK 请求（`max_retries=0`），也不跟随重定向；网关内部是否重试不在本模块
的承诺范围内。
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import uuid
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import httpx2
import openai
from openai.types import ImagesResponse

from pixelgate.artifacts import (
    IMAGE_NAME,
    MIME_TYPES,
    PROMPT_NAME,
    REQUEST_NAME,
    InputSnapshot,
    check_mask,
    create_artifact_dir,
    decode_output,
    snapshot_inputs,
    write_bytes,
    write_inputs,
    write_preview,
    write_receipt,
    write_text,
)
from pixelgate.schemas import (
    BaseImageRequest,
    Config,
    EditRequest,
    ErrorCategory,
    ErrorInfo,
    GenerateRequest,
    ImageRef,
    Operation,
    OutputInfo,
    PreviewInfo,
    Receipt,
    ReceiptState,
    ToolError,
    UsageInfo,
    WarningKind,
    parse_config,
    resolve_model,
    validate_background,
    validate_quality,
    validate_size,
)

logger = logging.getLogger(__name__)

HELPER_TIMEOUT_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 600
RESPONSE_MAX_BYTES = 64 * 1024 * 1024

GENERATIONS_PATH = "/v1/images/generations"
EDITS_PATH = "/v1/images/edits"
IMAGE_PATHS = (GENERATIONS_PATH, EDITS_PATH)

#: 构造客户端时清掉的环境变量：前六个会被 SDK 直接读走（后三个会变成 OpenAI-Organization
#: 与 OpenAI-Project 请求头发给本机网关，与订阅无关），后面八个会让 httpx 走代理。
CLEARED_ENV_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_WEBHOOK_SECRET",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

#: 每个错误类别对应的 receipt state：上游明确失败与本地预检失败记 failed，
#: 传输中断、超时与落地前中断记 unknown，即「这一次的上游结果未知」。
#: `artifact_write_failed` 一项另有分支，见 `_state_for`：它的语义取决于请求发出没有。
STATE_BY_CATEGORY: dict[ErrorCategory, ReceiptState] = {
    ErrorCategory.CONFIG_INVALID: "failed",
    ErrorCategory.AUTH_HELPER_FAILED: "failed",
    ErrorCategory.REQUEST_INVALID: "failed",
    ErrorCategory.INPUT_MISSING: "failed",
    ErrorCategory.INPUT_HASH_MISMATCH: "failed",
    ErrorCategory.INPUT_UNDECODABLE: "failed",
    ErrorCategory.INPUT_TOO_LARGE: "failed",
    ErrorCategory.MASK_MISMATCH: "failed",
    ErrorCategory.MODEL_NOT_ALLOWED: "failed",
    ErrorCategory.SIZE_INVALID: "failed",
    ErrorCategory.SIZE_EXPERIMENTAL: "failed",
    ErrorCategory.QUALITY_INVALID: "failed",
    ErrorCategory.BACKGROUND_INVALID: "failed",
    ErrorCategory.TARGET_DENIED: "failed",
    # 连都没连上，请求没发出去，上游必定什么都没发生，所以是确定的失败。
    ErrorCategory.GATEWAY_UNREACHABLE: "failed",
    ErrorCategory.UPSTREAM_INTERRUPTED: "unknown",
    ErrorCategory.UPSTREAM_ERROR: "failed",
    ErrorCategory.UPSTREAM_NO_IMAGE: "failed",
    ErrorCategory.UPSTREAM_BAD_IMAGE: "failed",
    ErrorCategory.RESPONSE_TOO_LARGE: "unknown",
    ErrorCategory.ARTIFACT_COLLISION: "failed",
    ErrorCategory.ARTIFACT_WRITE_FAILED: "unknown",
    ErrorCategory.BUSY: "failed",
    ErrorCategory.TIMEOUT: "unknown",
    ErrorCategory.CANCELLED: "unknown",
}

type QualityLiteral = Literal["auto", "low", "medium", "high", "xhigh", "max"]
type BackgroundLiteral = Literal["auto", "opaque", "transparent"]

#: 建连阶段就失败的传输异常：请求一个字节都没发出去。读写与协议类异常不在此列，
#: 它们意味着请求已经送达，落到 `upstream_interrupted`。
CONNECT_FAILURES = (
    httpx2.ConnectError,
    httpx2.ProxyError,
    httpx2.UnsupportedProtocol,
)

_KEY_RE = re.compile(r"[0-9a-f]{64}")
_STDERR_KEEP = 200


def _now() -> str:
    """当前时刻的 ISO 8601 UTC 表示，秒精度。"""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_config(path: Path) -> Config:
    """读配置文件并校验；文件缺失、非法 JSON 与字段不合法都是 `config_invalid`。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ToolError(ErrorCategory.CONFIG_INVALID, detail=str(path)) from err
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ToolError(ErrorCategory.CONFIG_INVALID, detail=str(path)) from err
    return parse_config(data)


def read_client_key(config: Config) -> str:
    """调 helper 在进程内取 client key。

    调用契约：不传任何参数，超时 `HELPER_TIMEOUT_SECONDS` 秒，stdout 须恰为一行 64 位
    小写十六进制。每次请求前现取，不在启动时缓存，这样轮换 key 不必重启服务。取到的值只
    留在返回值里，不进日志、异常消息与产物；helper 的 stderr 只截前 200 字节记日志，并且
    先把形似 key 的片段抹掉再记。
    """
    helper = config.client_key_helper
    try:
        # 可执行文件的路径来自已校验的配置，参数表为空，没有 shell 参与。
        completed = subprocess.run(
            [helper],
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        logger.error("凭据 helper 无法执行：%s（%s）", helper, type(err).__name__)
        raise ToolError(ErrorCategory.AUTH_HELPER_FAILED, detail=helper) from err
    if completed.returncode != 0:
        _log_helper_failure(helper, completed.returncode, completed.stderr)
        raise ToolError(ErrorCategory.AUTH_HELPER_FAILED, detail=helper)
    lines = completed.stdout.splitlines()
    if len(lines) != 1 or _KEY_RE.fullmatch(lines[0]) is None:
        _log_helper_failure(helper, completed.returncode, completed.stderr)
        raise ToolError(ErrorCategory.AUTH_HELPER_FAILED, detail=helper)
    return lines[0]


def _log_helper_failure(helper: str, returncode: int, stderr: str) -> None:
    """记 helper 的失败摘要；形似 key 的片段先抹掉，免得诊断信息反而泄露凭据。"""
    # 顺序要紧：先在全文上脱敏再截断。反过来的话，跨过截断点的那枚 key 在切片里
    # 不足 64 位、匹配不上正则，前缀就原样进了日志。
    summary = _KEY_RE.sub("<redacted>", stderr)[:_STDERR_KEEP]
    logger.error(
        "凭据 helper 失败：%s 退出码 %s，stderr：%s", helper, returncode, summary
    )


class _CountingStream(httpx2.SyncByteStream):
    """边转发响应体边累计字节数，超预算立刻中止，不等 SDK 把整个响应读完。"""

    def __init__(
        self, stream: httpx2.SyncByteStream, transport: "GuardedTransport"
    ) -> None:
        self._stream = stream
        self._transport = transport

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._stream:
            self._transport.bytes_read += len(chunk)
            if self._transport.bytes_read > self._transport.max_bytes:
                raise ToolError(ErrorCategory.RESPONSE_TOO_LARGE)
            yield chunk

    def close(self) -> None:
        self._stream.close()


class GuardedTransport(httpx2.BaseTransport):
    """只放行配置里那个回环地址的两个图像端点，并盯住响应体积。

    校验发生在建连之前，所以被拒的目标连 TCP 连接都不会建立。它同时记下最近一次响应的
    `x-request-id`，供 receipt 引用上游的请求编号。
    """

    def __init__(
        self,
        port: int,
        *,
        inner: httpx2.BaseTransport | None = None,
        max_bytes: int = RESPONSE_MAX_BYTES,
    ) -> None:
        self.port = port
        self.max_bytes = max_bytes
        self.last_request_id: str | None = None
        self.bytes_read = 0
        self._inner = (
            inner
            if inner is not None
            else httpx2.HTTPTransport(trust_env=False, retries=0)
        )

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """校验目标后转发请求，返回的响应体带字节计数。"""
        self._check_target(request.url)
        response = self._inner.handle_request(request)
        self.last_request_id = response.headers.get("x-request-id")
        # 同步传输返回的一定是同步字节流，这里只是把静态类型收窄。
        stream = cast("httpx2.SyncByteStream", response.stream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_CountingStream(stream, self),
            request=request,
            extensions=response.extensions,
        )

    def close(self) -> None:
        """关闭内层传输。"""
        self._inner.close()

    def _check_target(self, url: httpx2.URL) -> None:
        allowed = (
            url.scheme == "http"
            and url.host == "127.0.0.1"
            and url.port == self.port
            and url.path in IMAGE_PATHS
        )
        if not allowed:
            raise ToolError(ErrorCategory.TARGET_DENIED, detail=url.path)


@contextlib.contextmanager
def _without_env(names: tuple[str, ...]) -> Generator[None]:
    """临时摘掉指定环境变量，退出时原样放回。"""
    saved = {name: os.environ[name] for name in names if name in os.environ}
    for name in saved:
        del os.environ[name]
    try:
        yield
    finally:
        os.environ.update(saved)


def build_client(
    config: Config, api_key: str
) -> tuple[openai.OpenAI, GuardedTransport]:
    """构造只连本机网关、不重试、不跟随重定向、不读环境的 SDK 客户端。

    返回客户端与它用的传输对象，后者带着 `x-request-id` 与累计读取字节数，调用结束后仍可
    读取。环境变量只在构造期间被摘掉，构造完成即原样放回。
    """
    transport = GuardedTransport(config.port)
    # 两个客户端都在摘除窗口内构造：代理变量只影响 httpx 客户端，在窗口外构造它，摘除
    # 就不起作用。起主要防护作用的是 trust_env=False 与自带的 transport，窗口是第二道。
    with _without_env(CLEARED_ENV_VARS):
        http_client = httpx2.Client(
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        api = openai.OpenAI(
            base_url=config.base_url,
            api_key=api_key,
            max_retries=0,
            timeout=REQUEST_TIMEOUT_SECONDS,
            http_client=http_client,
        )
    return api, transport


@dataclass(frozen=True)
class _CallResult:
    """一次上游调用返回的、进 receipt 的那部分事实。"""

    b64: str
    actual_model: str | None
    revised_prompt: str | None
    usage: UsageInfo | None


def _as_file(snapshot: InputSnapshot) -> tuple[str, bytes, str]:
    return (snapshot.name, snapshot.data, MIME_TYPES[snapshot.format])


def _usage_of(response: ImagesResponse) -> UsageInfo | None:
    """取 usage 的三个字段，其余键按计划丢弃；上游没返回就是 None。"""
    usage = response.usage
    if usage is None:
        return None
    return UsageInfo(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


def _actual_model_of(response: ImagesResponse) -> str | None:
    """上游响应里的 model 字段；SDK 把它当扩展字段收着，没有就是 None。"""
    value = getattr(response, "model", None)
    return value if isinstance(value, str) else None


def _read_response(response: ImagesResponse) -> _CallResult:
    images = response.data or []
    if len(images) != 1:
        raise ToolError(ErrorCategory.UPSTREAM_NO_IMAGE)
    b64 = images[0].b64_json
    if not b64:
        raise ToolError(ErrorCategory.UPSTREAM_NO_IMAGE)
    return _CallResult(
        b64=b64,
        actual_model=_actual_model_of(response),
        revised_prompt=images[0].revised_prompt,
        usage=_usage_of(response),
    )


def _as_tool_error(err: openai.APIConnectionError) -> ToolError:
    """把连接层异常还原成本包的错误类别。

    目标守卫与体积上限都在传输层抛 `ToolError`，而 SDK 会把传输层的异常统一包成
    `APIConnectionError`，所以先顺着异常链找回原来的类别。找不到时按「请求到底发出去
    没有」分两类：建连阶段失败记 `gateway_unreachable`，上游确定什么都没发生；请求发出
    之后的读写或协议中断记 `upstream_interrupted`，上游可能已经生成并扣量。分不清的取
    后者，因为把「可能已扣量」误报成「没发出去」的代价更大。
    """
    cause: BaseException | None = err.__cause__
    while cause is not None:
        if isinstance(cause, ToolError):
            return cause
        if isinstance(cause, CONNECT_FAILURES):
            return ToolError(ErrorCategory.GATEWAY_UNREACHABLE)
        cause = cause.__cause__
    return ToolError(ErrorCategory.UPSTREAM_INTERRUPTED)


def _send(
    api: openai.OpenAI,
    *,
    use_edit: bool,
    prompt: str,
    route_model: str,
    size: str,
    quality: str,
    background: str,
    images: list[InputSnapshot],
    mask: InputSnapshot | None,
) -> _CallResult:
    """发出唯一的一次请求并读回结果；一切失败都翻译成 `ToolError`。"""
    checked_quality = cast("QualityLiteral", quality)
    checked_background = cast("BackgroundLiteral", background)
    try:
        if use_edit:
            response = api.images.edit(
                image=[_as_file(item) for item in images],
                mask=_as_file(mask) if mask is not None else openai.omit,
                prompt=prompt,
                model=route_model,
                n=1,
                output_format="png",
                size=size,
                quality=checked_quality,
                background=checked_background,
                stream=False,
            )
        else:
            response = api.images.generate(
                prompt=prompt,
                model=route_model,
                n=1,
                output_format="png",
                size=size,
                quality=checked_quality,
                background=checked_background,
                stream=False,
            )
    except openai.APITimeoutError as err:
        raise ToolError(ErrorCategory.TIMEOUT) from err
    except openai.APIStatusError as err:
        raise ToolError(
            ErrorCategory.UPSTREAM_ERROR, http_status=err.status_code
        ) from err
    except openai.APIConnectionError as err:
        raise _as_tool_error(err) from err
    except openai.APIError as err:
        raise ToolError(ErrorCategory.UPSTREAM_ERROR) from err
    return _read_response(response)


def _snapshot_all(
    parent: ImageRef | None, references: list[ImageRef], mask: ImageRef | None
) -> tuple[list[InputSnapshot], InputSnapshot | None]:
    """按发送顺序取输入图快照：父图在前，参考图其后，mask 单独一份。"""
    refs = ([parent] if parent is not None else []) + references
    names = ([] if parent is None else ["parent"]) + [
        f"ref-{index}" for index in range(1, len(references) + 1)
    ]
    images = snapshot_inputs(refs, names)
    if mask is None:
        return images, None
    [mask_snapshot] = snapshot_inputs([mask], ["mask"])
    if parent is not None:
        check_mask(mask_snapshot, images[0])
    return images, mask_snapshot


def _write_call_record(
    directory: Path,
    *,
    prompt: str,
    endpoint: str,
    route_model: str,
    size: str,
    quality: str,
    background: str,
    images: list[InputSnapshot],
    mask: InputSnapshot | None,
) -> None:
    """落下 prompt.txt、request.json 与输入快照。

    request.json 只记发出的参数与输入图的文件名，不含 key 也不含 prompt 正文：prompt
    已经单独存在 prompt.txt 里，两处各存一份只会多出一个对不齐的风险。
    """
    write_text(directory / PROMPT_NAME, prompt)
    record = {
        "endpoint": endpoint,
        "model": route_model,
        "n": 1,
        "output_format": "png",
        "stream": False,
        "size": size,
        "quality": quality,
        "background": background,
        "images": [item.name for item in images],
        "mask": mask.name if mask is not None else None,
        "prompt_file": PROMPT_NAME,
    }
    write_text(
        directory / REQUEST_NAME,
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
    )
    write_inputs(directory, images + ([mask] if mask is not None else []))


def _preview_or_none(image_path: Path, directory: Path) -> PreviewInfo | None:
    """生成预览，失败就当没有预览。

    图片这时已经落盘并验真过了，为了一个附属的预览把整次调用报成「没拿到图」，会让回执
    与现场对不上；记一条日志、在回执里留 `preview_omitted`，比推翻结果诚实。
    """
    try:
        return write_preview(image_path, directory)
    except ToolError as err:
        logger.error("预览生成失败：%s（%s）", image_path, err.category.value)
        return None


def _complete(
    directory: Path, receipt: Receipt, result: _CallResult, requested_size: str
) -> Receipt:
    """解码、落盘、生成预览，写终态 receipt。"""
    decoded = decode_output(result.b64)
    image_path = directory / IMAGE_NAME
    write_bytes(image_path, decoded.data)
    preview = _preview_or_none(image_path, directory)
    actual_size = f"{decoded.width}x{decoded.height}"
    mismatch = requested_size != "auto" and requested_size != actual_size
    warnings: list[WarningKind] = []
    if mismatch:
        warnings.append(WarningKind.SIZE_MISMATCH)
    if preview is None:
        warnings.append(WarningKind.PREVIEW_OMITTED)
    final = receipt.model_copy(
        update={
            "state": "completed",
            "finished_at": _now(),
            "output": OutputInfo(
                path=str(image_path),
                sha256=hashlib.sha256(decoded.data).hexdigest(),
                format=decoded.format,
                width=decoded.width,
                height=decoded.height,
                bytes=len(decoded.data),
            ),
            "preview": preview,
            "size_mismatch": mismatch,
            "warnings": warnings,
        }
    )
    write_receipt(directory, final)
    return final


def _state_for(category: ErrorCategory, request_sent: bool) -> ReceiptState:
    """取这次失败的 state。

    只有 `artifact_write_failed` 分两种：请求发出之前落盘就失败（目录不可写、磁盘满），
    上游什么都没发生，记 `failed`；发出之后才失败的，上游可能已经生成，记 `unknown`。
    别的类别不看这个标志。
    """
    if category is ErrorCategory.ARTIFACT_WRITE_FAILED and not request_sent:
        return "failed"
    return STATE_BY_CATEGORY[category]


def _fail(
    directory: Path | None,
    receipt: Receipt,
    err: ToolError,
    upstream_request_id: str | None,
    *,
    request_sent: bool,
) -> Receipt:
    """把失败落成终态 receipt；已经建了目录的，尽力把它写下去。"""
    failed = receipt.model_copy(
        update={
            "state": _state_for(err.category, request_sent),
            "finished_at": _now(),
            "upstream_request_id": upstream_request_id or receipt.upstream_request_id,
            "error": ErrorInfo(
                category=err.category,
                http_status=err.http_status,
                message_safe=err.message_safe,
            ),
        }
    )
    if directory is not None:
        try:
            write_receipt(directory, failed)
        except ToolError as write_err:
            logger.error(
                "终态 receipt 写入失败：%s（%s）", directory, write_err.category.value
            )
    return failed


def _execute(
    config: Config,
    request: BaseImageRequest,
    *,
    operation: Operation,
    parent: ImageRef | None,
    references: list[ImageRef],
    mask: ImageRef | None,
) -> Receipt:
    """一次受控调用的全流程，成功与失败都返回 receipt，不往外抛业务异常。"""
    receipt = Receipt(
        request_id=str(uuid.uuid4()),
        operation=operation,
        state="started",
        requested_model=request.model,
        route_model="",
        requested_size=request.size,
        requested_quality=request.quality,
        requested_background=request.background,
        parent=parent,
        references=references,
        mask=mask,
        started_at=_now(),
    )
    directory: Path | None = None
    transport: GuardedTransport | None = None
    request_sent = False
    try:
        route_model = resolve_model(request.model, config)
        receipt = receipt.model_copy(update={"route_model": route_model})
        validate_size(request.size)
        validate_quality(request.quality)
        validate_background(request.background)
        images, mask_snapshot = _snapshot_all(parent, references, mask)
        use_edit = operation == "edit" or bool(images)
        endpoint = EDITS_PATH if use_edit else GENERATIONS_PATH
        api_key = read_client_key(config)
        directory = create_artifact_dir(Path(request.output_dir))
        _write_call_record(
            directory,
            prompt=request.prompt,
            endpoint=endpoint,
            route_model=route_model,
            size=request.size,
            quality=request.quality,
            background=request.background,
            images=images,
            mask=mask_snapshot,
        )
        write_receipt(directory, receipt)
        api, transport = build_client(config, api_key)
        with api:
            request_sent = True
            result = _send(
                api,
                use_edit=use_edit,
                prompt=request.prompt,
                route_model=route_model,
                size=request.size,
                quality=request.quality,
                background=request.background,
                images=images,
                mask=mask_snapshot,
            )
        receipt = receipt.model_copy(
            update={
                "actual_model": result.actual_model,
                "revised_prompt": result.revised_prompt,
                "usage": result.usage,
                "upstream_request_id": transport.last_request_id,
            }
        )
        receipt = _complete(directory, receipt, result, request.size)
    except ToolError as err:
        upstream_id = transport.last_request_id if transport is not None else None
        receipt = _fail(directory, receipt, err, upstream_id, request_sent=request_sent)
    if directory is not None:
        receipt.bind_artifact_dir(str(directory))
    return receipt


def run_generate(config: Config, req: GenerateRequest) -> Receipt:
    """跑完一次生成调用。

    没给参考图走 generations，给了就走 edits；后者不声明父版本，receipt 的 `parent` 为
    null，因为参考图只是风格与内容的参照，不构成版本关系。
    """
    return _execute(
        config,
        req,
        operation="generate",
        parent=None,
        references=list(req.references),
        mask=None,
    )


def run_edit(config: Config, req: EditRequest) -> Receipt:
    """跑完一次编辑调用；父图作为首张输入图，receipt 记下它的路径与摘要。"""
    return _execute(
        config,
        req,
        operation="edit",
        parent=req.parent,
        references=list(req.references),
        mask=req.mask,
    )
