"""配置、请求与 receipt 的数据模型，以及错误类别与图像参数合法域。

本模块是错误类别枚举、型号白名单与尺寸合法域的单点定义，client、artifacts、server
只引用不另立一份。`GenerateRequest` 与 `EditRequest` 的 JSON schema 就是两个 MCP 工具的
inputSchema，`ReceiptSummary` 的 JSON schema 就是 outputSchema，校验器与 schema 同源。

两类失败分工不同：模型自身的形态校验失败抛 pydantic 的 `ValidationError`，由调用边界
（server 收到 MCP 入参、client 读配置）统一映射成 `request_invalid` 与
`config_invalid`；型号、尺寸、quality 各有自己的错误类别，由三个校验函数直接抛
`ToolError`。
"""

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
)

DEFAULT_MODEL = "gpt-image-2.5-sunburst"
DEFAULT_SIZE = "auto"
DEFAULT_QUALITY = "auto"
DEFAULT_BACKGROUND = "auto"

#: 型号白名单：只有两个官方别名，不接受裸名与日期快照 id（它们会绕开 model_routes）。
ALLOWED_MODELS = (DEFAULT_MODEL, "gpt-image-2.5-flare")
ALLOWED_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")
#: 官方 `background` 枚举；透明背景要求输出为 png 或 webp，本包固定发 png，天然满足。
ALLOWED_BACKGROUNDS = ("auto", "opaque", "transparent")

PROMPT_MAX_CHARS = 32_000
#: 单次调用的输入图上限，首版自限（官方上限为 16）；编辑时 parent 占其中一张。
MAX_INPUT_IMAGES = 5

# 尺寸合法域取自官方图像生成指南中 2.5 两款的自定义尺寸约束。
SIZE_MULTIPLE = 16
SIZE_MAX_EDGE = 3840
SIZE_MAX_RATIO = 3
SIZE_MIN_PIXELS = 655_360
SIZE_MAX_PIXELS = 8_294_400
#: 2560x1440 的像素数：高于它的分辨率官方标为实验性，首版拒绝。
SIZE_EXPERIMENTAL_PIXELS = 3_686_400

SHA256_PATTERN = r"^[0-9a-f]{64}$"

GATEWAY_SCHEME = "http"
GATEWAY_HOST = "127.0.0.1"
GATEWAY_PATH = "/v1"
#: http 的 scheme 默认端口，配置层显式拒绝它，理由见 _parse_gateway_port。
HTTP_DEFAULT_PORT = 80

_SIZE_RE = re.compile(r"([0-9]+)x([0-9]+)")

type Operation = Literal["generate", "edit"]
type ReceiptState = Literal["started", "completed", "failed", "unknown"]


class ErrorCategory(StrEnum):
    """工具对外暴露的错误类别，封闭枚举；新增类别须同步 README 错误类别表与测试。"""

    CONFIG_INVALID = "config_invalid"
    AUTH_HELPER_FAILED = "auth_helper_failed"
    REQUEST_INVALID = "request_invalid"
    INPUT_MISSING = "input_missing"
    INPUT_HASH_MISMATCH = "input_hash_mismatch"
    INPUT_UNDECODABLE = "input_undecodable"
    INPUT_TOO_LARGE = "input_too_large"
    MASK_MISMATCH = "mask_mismatch"
    MODEL_NOT_ALLOWED = "model_not_allowed"
    SIZE_INVALID = "size_invalid"
    SIZE_EXPERIMENTAL = "size_experimental"
    QUALITY_INVALID = "quality_invalid"
    BACKGROUND_INVALID = "background_invalid"
    TARGET_DENIED = "target_denied"
    GATEWAY_UNREACHABLE = "gateway_unreachable"
    UPSTREAM_INTERRUPTED = "upstream_interrupted"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_NO_IMAGE = "upstream_no_image"
    UPSTREAM_BAD_IMAGE = "upstream_bad_image"
    RESPONSE_TOO_LARGE = "response_too_large"
    ARTIFACT_COLLISION = "artifact_collision"
    ARTIFACT_WRITE_FAILED = "artifact_write_failed"
    BUSY = "busy"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class WarningKind(StrEnum):
    """receipt 与返回摘要里 warnings 的封闭取值。"""

    SIZE_MISMATCH = "size_mismatch"
    PREVIEW_OMITTED = "preview_omitted"


CATEGORY_MESSAGES: dict[ErrorCategory, str] = {
    ErrorCategory.CONFIG_INVALID: "配置不合法",
    ErrorCategory.AUTH_HELPER_FAILED: "凭据 helper 调用失败",
    ErrorCategory.REQUEST_INVALID: "请求参数不合法",
    ErrorCategory.INPUT_MISSING: "输入图片不存在",
    ErrorCategory.INPUT_HASH_MISMATCH: "输入图片的 sha256 与声明不符",
    ErrorCategory.INPUT_UNDECODABLE: "输入图片无法解码或格式不受支持",
    ErrorCategory.INPUT_TOO_LARGE: "输入图片超出体积或像素上限",
    ErrorCategory.MASK_MISMATCH: "mask 与父图的格式、尺寸或透明通道不匹配",
    ErrorCategory.MODEL_NOT_ALLOWED: "型号不在白名单内",
    ErrorCategory.SIZE_INVALID: "尺寸不在合法域内",
    ErrorCategory.SIZE_EXPERIMENTAL: "尺寸落在实验区间，首版不接受",
    ErrorCategory.QUALITY_INVALID: "quality 不是合法取值",
    ErrorCategory.BACKGROUND_INVALID: "background 不是合法取值",
    ErrorCategory.TARGET_DENIED: "请求目标不是配置中的回环网关地址",
    ErrorCategory.GATEWAY_UNREACHABLE: "连不上网关，请求没有发出",
    ErrorCategory.UPSTREAM_INTERRUPTED: "请求已发出，连接在读完响应前中断",
    ErrorCategory.UPSTREAM_ERROR: "上游返回错误",
    ErrorCategory.UPSTREAM_NO_IMAGE: "上游响应没有恰好一张图片",
    ErrorCategory.UPSTREAM_BAD_IMAGE: "上游返回的图片无法解码",
    ErrorCategory.RESPONSE_TOO_LARGE: "上游响应超出读取上限",
    ErrorCategory.ARTIFACT_COLLISION: "产物目标文件已存在",
    ErrorCategory.ARTIFACT_WRITE_FAILED: "产物落盘失败",
    ErrorCategory.BUSY: "同一进程已有调用在进行中",
    ErrorCategory.TIMEOUT: "调用超时，上游结果未知",
    ErrorCategory.CANCELLED: "调用被取消，上游结果未知",
}


class ToolError(Exception):
    """全包唯一的业务异常，携带错误类别与可对外的安全文案。

    `message_safe` 由类别的固定文案加可选的 `detail` 组成，会进入 receipt、MCP 返回与
    stderr。`detail` 只允许放三类内容：路径、本包自己的字段名、封闭枚举的取值；调用方
    给的 prompt、上游响应原文与凭据一律不得传入。
    """

    def __init__(
        self,
        category: ErrorCategory,
        *,
        http_status: int | None = None,
        detail: str | None = None,
    ) -> None:
        base = CATEGORY_MESSAGES[category]
        self.category = category
        self.http_status = http_status
        self.message_safe = base if detail is None else f"{base}：{detail}"
        super().__init__(self.message_safe)


def field_locations(error: ValidationError) -> str:
    """把校验失败的位置串成安全文案：只取字段名，不取字段值。"""
    locations = {
        ".".join(str(part) for part in item["loc"]) or "<root>"
        for item in error.errors()
    }
    return "字段 " + "、".join(sorted(locations))


def _parse_gateway_port(base_url: str) -> int:
    """校验 `base_url` 的形态并取出显式端口，不符合时抛 `ValueError`。

    只接受 `http://127.0.0.1:<port>/v1`：端口显式，没有用户名密码，没有 query 与
    fragment，路径恰为 `/v1`。这是「只连本机网关」在配置层的落点，传输层另有守卫复核。
    """
    parts = urlsplit(base_url)
    expected = f"{GATEWAY_SCHEME}://{GATEWAY_HOST}:<port>{GATEWAY_PATH}"
    if parts.scheme != GATEWAY_SCHEME or parts.hostname != GATEWAY_HOST:
        raise ValueError(f"base_url 只允许 {expected}，收到 {base_url!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"base_url 不得带用户名或密码，收到 {base_url!r}")
    if parts.query or parts.fragment:
        raise ValueError(f"base_url 不得带 query 或 fragment，收到 {base_url!r}")
    if parts.path != GATEWAY_PATH:
        raise ValueError(f"base_url 的路径须恰为 {GATEWAY_PATH}，收到 {base_url!r}")
    try:
        port = parts.port
    except ValueError as err:
        raise ValueError(f"base_url 的端口超出范围，收到 {base_url!r}") from err
    if port is None or not 1 <= port <= 65535:
        raise ValueError(f"base_url 须带显式端口，收到 {base_url!r}")
    if port == HTTP_DEFAULT_PORT:
        # httpx 按 WHATWG 规范把 scheme 的默认端口归一化成空，传输层守卫比较的就是那个
        # 空值，所以配置里写 :80 会让每一次调用都在守卫处被判否、退化成 target_denied。
        raise ValueError(
            f"base_url 不接受 http 的默认端口 {HTTP_DEFAULT_PORT}，"
            f"它在传输层会被归一化为空、与配置端口比不上，收到 {base_url!r}"
        )
    return port


class ImageRef(BaseModel):
    """调用方声明的图片引用；发送前按 sha256 核对文件内容。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="图片的绝对路径")
    sha256: str = Field(
        pattern=SHA256_PATTERN,
        description="文件内容的 sha256，64 位小写十六进制",
    )

    @field_validator("path")
    @classmethod
    def _check_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError(f"图片路径须为绝对路径，收到 {value!r}")
        return value


class Config(BaseModel):
    """`~/.config/pixelgate/config.json` 的内容，字段固定四个。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    base_url: str
    client_key_helper: str
    model_routes: dict[str, str]

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        _parse_gateway_port(value)
        return value

    @field_validator("client_key_helper")
    @classmethod
    def _check_helper(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"client_key_helper 须为绝对路径，收到 {value!r}")
        if not path.is_file():
            raise ValueError(f"client_key_helper 不是已存在的文件：{value}")
        if not os.access(path, os.X_OK):
            raise ValueError(f"client_key_helper 不可执行：{value}")
        return value

    @field_validator("model_routes")
    @classmethod
    def _check_routes(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(ALLOWED_MODELS):
            raise ValueError(
                f"model_routes 的键须恰为 {sorted(ALLOWED_MODELS)}，"
                f"收到 {sorted(value)}"
            )
        for alias, route in value.items():
            if not route.strip():
                raise ValueError(f"model_routes[{alias!r}] 的请求名不能为空")
        return value

    @property
    def port(self) -> int:
        """`base_url` 里的显式端口，供传输层的目标守卫比对。"""
        return _parse_gateway_port(self.base_url)


def parse_config(data: object) -> Config:
    """把已解析的 JSON 对象校验为 `Config`，任何失败都是 `config_invalid`。

    错误文案只带失败的字段名，不带字段值：配置内容会进 stderr 与 receipt，不宜原样回显。
    """
    try:
        return Config.model_validate(data)
    except ValidationError as err:
        raise ToolError(
            ErrorCategory.CONFIG_INVALID, detail=field_locations(err)
        ) from err


class BaseImageRequest(BaseModel):
    """两个工具入参的公共部分。

    `model`、`size`、`quality`、`background` 在这里只校验类型，取值合法性由
    `resolve_model`、`validate_size`、`validate_quality`、`validate_background` 在调用前
    逐项判，因为它们各有自己的错误类别。
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(description=f"绘图提示词，非空且不超过 {PROMPT_MAX_CHARS} 字符")
    output_dir: str = Field(description="已存在的输出目录，绝对路径")
    model: str = Field(
        default=DEFAULT_MODEL,
        description=f"型号别名，取 {ALLOWED_MODELS[0]} 或 {ALLOWED_MODELS[1]}",
    )
    size: str = Field(
        default=DEFAULT_SIZE,
        description="auto 或 <宽>x<高>；宽高须为 16 的倍数",
    )
    quality: str = Field(
        default=DEFAULT_QUALITY,
        description=f"取 {'、'.join(ALLOWED_QUALITIES)} 之一",
    )
    background: str = Field(
        default=DEFAULT_BACKGROUND,
        description=(
            f"取 {'、'.join(ALLOWED_BACKGROUNDS)} 之一；"
            "transparent 时产物 png 带 alpha 通道"
        ),
    )

    @field_validator("prompt")
    @classmethod
    def _check_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt 不能为空")
        if len(value) > PROMPT_MAX_CHARS:
            raise ValueError(
                f"prompt 最长 {PROMPT_MAX_CHARS} 字符，收到 {len(value)} 字符"
            )
        return value

    @field_validator("output_dir")
    @classmethod
    def _check_output_dir(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"output_dir 须为绝对路径，收到 {value!r}")
        if not path.is_dir():
            raise ValueError(f"output_dir 不是已存在的目录：{value}")
        return value


class GenerateRequest(BaseImageRequest):
    """`generate_image` 的入参：提示词与输出目录必填，参考图、型号、尺寸与质量可选。"""

    references: list[ImageRef] = Field(
        default_factory=list,
        max_length=MAX_INPUT_IMAGES,
        description="参考图，0 到 5 张；给了就走编辑端点，但不声明父版本",
    )


class EditRequest(BaseImageRequest):
    """`edit_image` 的入参：提示词、父图与输出目录必填，参考图、遮罩与三个参数可选。"""

    parent: ImageRef = Field(description="被编辑的父图，作为编辑端点的首图")
    references: list[ImageRef] = Field(
        default_factory=list,
        # parent 占输入图的一张，所以 references 比 generate 少一张。
        max_length=MAX_INPUT_IMAGES - 1,
        description="附加参考图，0 到 4 张，与 parent 合计不超过 5 张",
    )
    mask: ImageRef | None = Field(
        default=None,
        description="遮罩，须与 parent 同格式同尺寸并带 alpha 通道",
    )


def validate_size(size: str) -> None:
    """校验 `size`；不合法抛 `size_invalid`，落在实验区间抛 `size_experimental`。

    判定按官方指南的合法域，`auto` 与官方推荐尺寸都不走旁路。总像素超过上限记
    `size_invalid`，落在实验阈值与上限之间才记 `size_experimental`，两者不重叠。
    """
    if size == DEFAULT_SIZE:
        return
    match = _SIZE_RE.fullmatch(size)
    if match is None:
        raise ToolError(ErrorCategory.SIZE_INVALID)
    width, height = int(match.group(1)), int(match.group(2))
    if width < 1 or height < 1:
        raise ToolError(ErrorCategory.SIZE_INVALID)
    pixels = width * height
    off_grid = width % SIZE_MULTIPLE != 0 or height % SIZE_MULTIPLE != 0
    out_of_ratio = max(width, height) > SIZE_MAX_RATIO * min(width, height)
    over_edge = max(width, height) > SIZE_MAX_EDGE
    out_of_pixels = not SIZE_MIN_PIXELS <= pixels <= SIZE_MAX_PIXELS
    if off_grid or out_of_ratio or over_edge or out_of_pixels:
        raise ToolError(ErrorCategory.SIZE_INVALID)
    if pixels > SIZE_EXPERIMENTAL_PIXELS:
        raise ToolError(ErrorCategory.SIZE_EXPERIMENTAL)


def resolve_model(model: str, config: Config) -> str:
    """把白名单内的型号别名换成发往网关的请求名，否则抛 `model_not_allowed`。"""
    if model not in ALLOWED_MODELS:
        raise ToolError(ErrorCategory.MODEL_NOT_ALLOWED)
    return config.model_routes[model]


def validate_quality(quality: str) -> None:
    """校验 `quality`，非枚举值抛 `quality_invalid`。"""
    if quality not in ALLOWED_QUALITIES:
        raise ToolError(ErrorCategory.QUALITY_INVALID)


def validate_background(background: str) -> None:
    """校验 `background`，非枚举值抛 `background_invalid`。"""
    if background not in ALLOWED_BACKGROUNDS:
        raise ToolError(ErrorCategory.BACKGROUND_INVALID)


class OutputInfo(BaseModel):
    """落盘后的实际图片，字段取自解码结果而不是请求参数。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    format: str
    width: int
    height: int
    bytes: int


class PreviewInfo(BaseModel):
    """随返回一起给出的小体积预览。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    bytes: int
    edge: int


class UsageInfo(BaseModel):
    """上游 usage 的原样子集。

    这是全模块唯一容许多余键的模型：上游 usage 的其余键按计划丢弃，所以保留 pydantic
    默认的忽略行为；上游没返回 usage 时整个对象为 null，不补零。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ErrorInfo(BaseModel):
    """receipt 与返回摘要里的错误对象。"""

    model_config = ConfigDict(extra="forbid")

    category: ErrorCategory
    http_status: int | None = None
    message_safe: str


class ReceiptSummary(BaseModel):
    """一次调用的回执摘要：状态、实际型号、产物与预览、用量、错误与提示各一份。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    operation: Operation
    state: ReceiptState
    requested_model: str
    actual_model: str | None
    output: OutputInfo | None
    preview: PreviewInfo | None
    size_mismatch: bool
    usage: UsageInfo | None
    error: ErrorInfo | None
    warnings: list[WarningKind]
    #: 产物目录；预检阶段就失败、还没建目录的调用没有它，取 null。
    artifact_dir: str | None


class Receipt(BaseModel):
    """`receipt.json` 的内容。

    缺失值一律为 null，不填零也不拿请求值冒充返回值；`actual_model`、`usage`、
    `upstream_request_id`、`revised_prompt` 都只在上游确实返回时才有值。序列化用别名，
    `schema_version` 落到文件里是 `schema`。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[1] = Field(default=1, alias="schema")
    request_id: str
    operation: Operation
    state: ReceiptState
    requested_model: str
    route_model: str
    actual_model: str | None = None
    requested_size: str
    requested_quality: str
    requested_background: str
    parent: ImageRef | None = None
    references: list[ImageRef] = Field(default_factory=list)
    mask: ImageRef | None = None
    started_at: str
    finished_at: str | None = None
    output: OutputInfo | None = None
    size_mismatch: bool = False
    usage: UsageInfo | None = None
    upstream_request_id: str | None = None
    revised_prompt: str | None = None
    preview: PreviewInfo | None = None
    error: ErrorInfo | None = None
    warnings: list[WarningKind] = Field(default_factory=list)

    #: 本次调用的产物目录。它是进程内的传递位，不进 JSON、不进摘要的键集合，因为产物
    #: 目录是「这份 receipt 存在哪里」而不是 receipt 的内容；预检阶段就失败时没有目录。
    _artifact_dir: str | None = PrivateAttr(default=None)

    @property
    def artifact_dir(self) -> str | None:
        """本次调用的产物目录，没有建目录就失败时为 None。"""
        return self._artifact_dir

    def bind_artifact_dir(self, artifact_dir: str) -> None:
        """记下产物目录，供调用方构造 `summary` 时取用。"""
        self._artifact_dir = artifact_dir

    def summary(self, artifact_dir: str | None) -> ReceiptSummary:
        """取 MCP 返回用的摘要子集，加上产物目录；没有建目录时传 None。"""
        return ReceiptSummary(
            request_id=self.request_id,
            operation=self.operation,
            state=self.state,
            requested_model=self.requested_model,
            actual_model=self.actual_model,
            output=self.output,
            preview=self.preview,
            size_mismatch=self.size_mismatch,
            usage=self.usage,
            error=self.error,
            warnings=self.warnings,
            artifact_dir=artifact_dir,
        )
