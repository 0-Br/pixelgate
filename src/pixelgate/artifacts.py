"""输入快照与校验、产物目录、排他写、上游图片的解码验真与预览。

两条贯穿本模块的纪律。一是不覆盖：每个产物文件都用 `O_EXCL` 创建，撞上已有文件即
`artifact_collision`，唯一的例外是 receipt 从 started 换成终态。二是权限由代码显式设定：
目录 0700、文件 0600，宽松 umask 下也不放宽，所以 mkdir 与 open 之后都再 chmod 一次。
"""

import base64
import binascii
import hashlib
import io
import json
import os
import uuid
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from pixelgate.schemas import (
    ErrorCategory,
    ImageRef,
    PreviewInfo,
    Receipt,
    ToolError,
)

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_INPUT_EDGE = 8192
MAX_INPUT_PIXELS = 16_777_216
ALLOWED_INPUT_FORMATS = ("png", "jpeg", "webp")

PREVIEW_EDGES = (768, 512, 384)
PREVIEW_QUALITY = 85
#: 预览的 base64 体积预算，超了就降一档边长，三档都超就不带预览。
PREVIEW_BASE64_BUDGET = 100 * 1024

DIR_MODE = 0o700
FILE_MODE = 0o600

INPUTS_DIR = "inputs"
IMAGE_NAME = "image.png"
PREVIEW_NAME = "preview.jpg"
PROMPT_NAME = "prompt.txt"
REQUEST_NAME = "request.json"
RECEIPT_NAME = "receipt.json"

EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


@dataclass(frozen=True)
class InputSnapshot:
    """一张输入图的内存快照。

    发往上游的就是这里的 `data`，落进 `inputs/` 的也是它，所以请求内容与留档内容同源，
    不会因为文件在调用期间被改动而对不上。
    """

    name: str
    data: bytes
    format: str
    width: int
    height: int
    mode: str


@dataclass(frozen=True)
class DecodedImage:
    """上游返回并验真后的图片。"""

    data: bytes
    format: str
    width: int
    height: int


def _artifact_dir_name() -> str:
    """产物目录名：UTC 时间戳加 uuid4 前 8 位。"""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _decode_image(
    data: bytes, *, bad: ErrorCategory, oversize: ErrorCategory
) -> tuple[str, int, int, str]:
    """完整解码一张图片，返回格式、宽、高与 Pillow mode。

    先读几何尺寸再解码，超上限的图不会被真的解出来；Pillow 的 decompression bomb 告警在
    这里转成错误，不让它以告警的形式溜过去。
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "").lower()
                width, height = image.size
                mode = image.mode
                if max(width, height) > MAX_INPUT_EDGE:
                    raise ToolError(oversize)
                if width * height > MAX_INPUT_PIXELS:
                    raise ToolError(oversize)
                image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as err:
        raise ToolError(oversize) from err
    except (OSError, ValueError, SyntaxError) as err:
        raise ToolError(bad) from err
    return image_format, width, height, mode


def _snapshot_one(ref: ImageRef, name: str) -> InputSnapshot:
    path = Path(ref.path)
    if not path.is_file():
        raise ToolError(ErrorCategory.INPUT_MISSING, detail=ref.path)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ToolError(ErrorCategory.INPUT_TOO_LARGE, detail=ref.path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.sha256:
        raise ToolError(ErrorCategory.INPUT_HASH_MISMATCH, detail=ref.path)
    image_format, width, height, mode = _decode_image(
        data,
        bad=ErrorCategory.INPUT_UNDECODABLE,
        oversize=ErrorCategory.INPUT_TOO_LARGE,
    )
    if image_format not in ALLOWED_INPUT_FORMATS:
        raise ToolError(ErrorCategory.INPUT_UNDECODABLE, detail=ref.path)
    return InputSnapshot(
        name=f"{name}{EXTENSIONS[image_format]}",
        data=data,
        format=image_format,
        width=width,
        height=height,
        mode=mode,
    )


def snapshot_inputs(refs: list[ImageRef], names: list[str]) -> list[InputSnapshot]:
    """逐张核对输入图并读成内存快照。

    参数
    ----------
    refs : list[ImageRef]
        调用方声明的图片引用。
    names : list[str]
        每张图的目标文件名主干（`parent`、`ref-1`、`mask`），扩展名按识别出的格式补。

    返回
    ----------
    list[InputSnapshot]
        与 `refs` 等长、同序。

    异常
    ----------
    ToolError
        文件不存在、体积或像素超限、摘要不符、格式不受支持或无法完整解码。
    """
    if len(refs) != len(names):
        raise ValueError(
            f"refs 与 names 须等长，收到 {len(refs)} 个引用与 {len(names)} 个名字"
        )
    return [_snapshot_one(ref, name) for ref, name in zip(refs, names, strict=True)]


def check_mask(mask: InputSnapshot, parent: InputSnapshot) -> None:
    """核对 mask 与父图的格式、尺寸与透明通道，不符即 `mask_mismatch`。"""
    if mask.format != parent.format:
        raise ToolError(ErrorCategory.MASK_MISMATCH, detail=mask.name)
    if (mask.width, mask.height) != (parent.width, parent.height):
        raise ToolError(ErrorCategory.MASK_MISMATCH, detail=mask.name)
    if "A" not in mask.mode:
        raise ToolError(ErrorCategory.MASK_MISMATCH, detail=mask.name)


def _make_dir(path: Path) -> None:
    try:
        path.mkdir(mode=DIR_MODE)
    except FileExistsError as err:
        raise ToolError(ErrorCategory.ARTIFACT_COLLISION, detail=str(path)) from err
    except OSError as err:
        raise ToolError(ErrorCategory.ARTIFACT_WRITE_FAILED, detail=str(path)) from err
    path.chmod(DIR_MODE)


def create_artifact_dir(output_dir: Path) -> Path:
    """在 `output_dir` 下排他创建本次调用的产物目录并返回它。"""
    directory = output_dir / _artifact_dir_name()
    _make_dir(directory)
    return directory


def write_bytes(path: Path, data: bytes) -> None:
    """排他写一个 0600 的文件；目标已存在即 `artifact_collision`。"""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    except FileExistsError as err:
        raise ToolError(ErrorCategory.ARTIFACT_COLLISION, detail=str(path)) from err
    except OSError as err:
        raise ToolError(ErrorCategory.ARTIFACT_WRITE_FAILED, detail=str(path)) from err
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except OSError as err:
        raise ToolError(ErrorCategory.ARTIFACT_WRITE_FAILED, detail=str(path)) from err
    os.chmod(path, FILE_MODE)


def write_text(path: Path, text: str) -> None:
    """排他写一个 0600 的 UTF-8 文本文件。"""
    write_bytes(path, text.encode("utf-8"))


def write_inputs(directory: Path, snapshots: list[InputSnapshot]) -> None:
    """把输入快照落到产物目录的 `inputs/` 下；没有输入图就不建这个目录。"""
    if not snapshots:
        return
    inputs = directory / INPUTS_DIR
    _make_dir(inputs)
    for snapshot in snapshots:
        write_bytes(inputs / snapshot.name, snapshot.data)


def _check_replaceable(target: Path, receipt: Receipt) -> None:
    """只有本次调用自己写下的 started receipt 可以被替换，别的一律算碰撞。"""
    try:
        existing: object = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ToolError(ErrorCategory.ARTIFACT_COLLISION, detail=str(target)) from err
    if not isinstance(existing, dict):
        raise ToolError(ErrorCategory.ARTIFACT_COLLISION, detail=str(target))
    same_call = existing.get("request_id") == receipt.request_id
    if not same_call or existing.get("state") != "started":
        raise ToolError(ErrorCategory.ARTIFACT_COLLISION, detail=str(target))


def write_receipt(directory: Path, receipt: Receipt) -> None:
    """原子写 `receipt.json`：先写临时文件再 `os.replace`，读取方看不到半份内容。

    序列化走别名，落到文件里的键是 `schema` 而不是 `schema_version`。
    """
    target = directory / RECEIPT_NAME
    if target.exists():
        _check_replaceable(target, receipt)
    payload = json.dumps(
        receipt.model_dump(by_alias=True), ensure_ascii=False, indent=2
    )
    # 临时文件也走排他创建：产物目录里没有任何一条路径可以无条件删掉已存在的文件，
    # 撞上残留就报碰撞，交给人看一眼那份残留是什么。
    temporary = directory / f".{RECEIPT_NAME}.tmp"
    write_bytes(temporary, f"{payload}\n".encode())
    try:
        os.replace(temporary, target)
        os.chmod(target, FILE_MODE)
    except OSError as err:
        raise ToolError(
            ErrorCategory.ARTIFACT_WRITE_FAILED, detail=str(target)
        ) from err


def decode_output(b64: str) -> DecodedImage:
    """解码上游返回的图片并验真。

    只接受 PNG：请求里固定发了 `output_format=png`，别的格式说明上游没有兑现参数，按
    `upstream_bad_image` 处置而不是默默改名保存。像素上限与输入图同一套。
    """
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ToolError(ErrorCategory.UPSTREAM_BAD_IMAGE) from err
    image_format, width, height, _ = _decode_image(
        data,
        bad=ErrorCategory.UPSTREAM_BAD_IMAGE,
        oversize=ErrorCategory.UPSTREAM_BAD_IMAGE,
    )
    if image_format != "png":
        raise ToolError(ErrorCategory.UPSTREAM_BAD_IMAGE)
    return DecodedImage(data=data, format=image_format, width=width, height=height)


def write_preview(image_path: Path, directory: Path) -> PreviewInfo | None:
    """按边长三档生成 JPEG 预览，返回落盘结果；三档都超预算就返回 None。

    预览是独立副本，原图既不转码也不改动。`edge` 记的是实际最长边，小图不会被放大，
    所以它可能小于当档的目标边长。
    """
    try:
        with Image.open(image_path) as image:
            image.load()
            source = image.convert("RGB")
    except OSError as err:
        raise ToolError(
            ErrorCategory.ARTIFACT_WRITE_FAILED, detail=str(image_path)
        ) from err
    for edge in PREVIEW_EDGES:
        thumbnail = source.copy()
        thumbnail.thumbnail((edge, edge))
        buffer = io.BytesIO()
        try:
            thumbnail.save(buffer, format="JPEG", quality=PREVIEW_QUALITY)
        except OSError as err:
            raise ToolError(
                ErrorCategory.ARTIFACT_WRITE_FAILED, detail=str(image_path)
            ) from err
        data = buffer.getvalue()
        if len(base64.b64encode(data)) <= PREVIEW_BASE64_BUDGET:
            target = directory / PREVIEW_NAME
            write_bytes(target, data)
            return PreviewInfo(
                path=str(target), bytes=len(data), edge=max(thumbnail.size)
            )
    return None
