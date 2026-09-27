"""artifacts 模块：输入快照与校验、产物目录、排他写、解码验真与预览。"""

import base64
import hashlib
import io
import json
import os
import random
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import MIB, png_bytes, png_bytes_of_size
from PIL import Image

from pixelgate import artifacts
from pixelgate.artifacts import (
    DIR_MODE,
    FILE_MODE,
    MAX_INPUT_BYTES,
    PREVIEW_BASE64_BUDGET,
    PREVIEW_QUALITY,
    check_mask,
    create_artifact_dir,
    decode_output,
    snapshot_inputs,
    write_bytes,
    write_preview,
    write_receipt,
)
from pixelgate.schemas import ErrorCategory, ImageRef, Receipt, ToolError

HEX64 = "a3f1" * 16


def noise_png(size: int, seed: int = 20260913) -> bytes:
    """高熵 PNG：像素取固定种子的伪随机字节，缩放后仍难压缩，用来压预览的体积预算。

    不用 os.urandom：分档判据贴着体积预算的边界，真随机数据会让同一份代码在不同轮次
    落到相邻档位，那种绿是不稳定的绿。
    """
    image = Image.frombytes(
        "RGB", (size, size), random.Random(seed).randbytes(size * size * 3)
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=0)
    return buffer.getvalue()


def receipt_fields() -> dict[str, object]:
    """一份 started 态 receipt 的必填字段。"""
    return {
        "request_id": "0f8c2b1a-3d4e-4f50-9a61-7b2c3d4e5f60",
        "operation": "generate",
        "state": "started",
        "requested_model": "gpt-image-2.5-sunburst",
        "route_model": "gpt-image-2.5-sunburst",
        "requested_size": "auto",
        "requested_quality": "auto",
        "requested_background": "auto",
        "started_at": "2026-09-13T12:00:00Z",
    }


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


# --- 输入快照 ---


def test_snapshot_inputs_records_bytes_and_geometry(
    make_png: Callable[..., ImageRef],
) -> None:
    ref = make_png(48, 32)
    [snapshot] = snapshot_inputs([ref], ["parent"])
    assert snapshot.name == "parent.png"
    assert snapshot.format == "png"
    assert (snapshot.width, snapshot.height) == (48, 32)
    assert snapshot.data == Path(ref.path).read_bytes()


def test_snapshot_inputs_names_references_in_order(
    make_png: Callable[..., ImageRef],
) -> None:
    refs = [make_png(), make_png()]
    snapshots = snapshot_inputs(refs, ["ref-1", "ref-2"])
    assert [item.name for item in snapshots] == ["ref-1.png", "ref-2.png"]


def test_snapshot_inputs_rejects_missing_file(tmp_path: Path) -> None:
    ref = ImageRef(path=str(tmp_path / "absent.png"), sha256=HEX64)
    with pytest.raises(ToolError) as exc_info:
        snapshot_inputs([ref], ["parent"])
    assert exc_info.value.category is ErrorCategory.INPUT_MISSING


def test_snapshot_inputs_rejects_directory(tmp_path: Path) -> None:
    ref = ImageRef(path=str(tmp_path), sha256=HEX64)
    with pytest.raises(ToolError) as exc_info:
        snapshot_inputs([ref], ["parent"])
    assert exc_info.value.category is ErrorCategory.INPUT_MISSING


def test_snapshot_inputs_rejects_hash_mismatch(
    make_png: Callable[..., ImageRef],
) -> None:
    ref = make_png()
    lying = ImageRef(path=ref.path, sha256=HEX64)
    with pytest.raises(ToolError) as exc_info:
        snapshot_inputs([lying], ["parent"])
    assert exc_info.value.category is ErrorCategory.INPUT_HASH_MISMATCH


def test_snapshot_inputs_rejects_broken_image(
    write_image: Callable[[bytes], ImageRef],
) -> None:
    ref = write_image(png_bytes()[:40] + os.urandom(200))
    with pytest.raises(ToolError) as exc_info:
        snapshot_inputs([ref], ["parent"])
    assert exc_info.value.category is ErrorCategory.INPUT_UNDECODABLE


def test_snapshot_inputs_rejects_non_image(
    write_image: Callable[[bytes], ImageRef],
) -> None:
    ref = write_image("这不是图片".encode())
    with pytest.raises(ToolError) as exc_info:
        snapshot_inputs([ref], ["parent"])
    assert exc_info.value.category is ErrorCategory.INPUT_UNDECODABLE


def test_snapshot_inputs_accepts_exactly_the_byte_limit(
    write_image: Callable[[bytes], ImageRef],
) -> None:
    ref = write_image(png_bytes_of_size(MAX_INPUT_BYTES))
    [snapshot] = snapshot_inputs([ref], ["parent"])
    assert len(snapshot.data) == 32 * MIB


def test_snapshot_inputs_rejects_one_byte_over_the_limit(
    write_image: Callable[[bytes], ImageRef],
) -> None:
    ref = write_image(png_bytes_of_size(MAX_INPUT_BYTES + 1))
    with pytest.raises(ToolError) as exc_info:
        snapshot_inputs([ref], ["parent"])
    assert exc_info.value.category is ErrorCategory.INPUT_TOO_LARGE


def test_snapshot_inputs_rejects_over_wide_image(
    write_image: Callable[[bytes], ImageRef],
) -> None:
    ref = write_image(png_bytes(8208, 16))
    with pytest.raises(ToolError) as exc_info:
        snapshot_inputs([ref], ["parent"])
    assert exc_info.value.category is ErrorCategory.INPUT_TOO_LARGE


def test_snapshot_inputs_rejects_over_total_pixels(
    write_image: Callable[[bytes], ImageRef],
) -> None:
    ref = write_image(png_bytes(4096, 4097, "L"))
    with pytest.raises(ToolError) as exc_info:
        snapshot_inputs([ref], ["parent"])
    assert exc_info.value.category is ErrorCategory.INPUT_TOO_LARGE


# --- mask 与父图的匹配 ---


def test_check_mask_accepts_same_size_with_alpha(
    make_png: Callable[..., ImageRef],
) -> None:
    [parent] = snapshot_inputs([make_png(64, 64)], ["parent"])
    [mask] = snapshot_inputs([make_png(64, 64, "RGBA")], ["mask"])
    assert check_mask(mask, parent) is None


def test_check_mask_rejects_different_size(make_png: Callable[..., ImageRef]) -> None:
    [parent] = snapshot_inputs([make_png(64, 64)], ["parent"])
    [mask] = snapshot_inputs([make_png(32, 64, "RGBA")], ["mask"])
    with pytest.raises(ToolError) as exc_info:
        check_mask(mask, parent)
    assert exc_info.value.category is ErrorCategory.MASK_MISMATCH


def test_check_mask_rejects_missing_alpha(make_png: Callable[..., ImageRef]) -> None:
    [parent] = snapshot_inputs([make_png(64, 64)], ["parent"])
    [mask] = snapshot_inputs([make_png(64, 64)], ["mask"])
    with pytest.raises(ToolError) as exc_info:
        check_mask(mask, parent)
    assert exc_info.value.category is ErrorCategory.MASK_MISMATCH


def test_check_mask_rejects_different_format(
    make_png: Callable[..., ImageRef], write_image: Callable[[bytes], ImageRef]
) -> None:
    [parent] = snapshot_inputs([make_png(64, 64)], ["parent"])
    buffer = io.BytesIO()
    Image.new("RGBA", (64, 64)).save(buffer, format="WEBP")
    [mask] = snapshot_inputs([write_image(buffer.getvalue())], ["mask"])
    with pytest.raises(ToolError) as exc_info:
        check_mask(mask, parent)
    assert exc_info.value.category is ErrorCategory.MASK_MISMATCH


# --- 产物目录与排他写 ---


def test_create_artifact_dir_is_private_under_loose_umask(
    tmp_output_dir: Path, loose_umask: None
) -> None:
    directory = create_artifact_dir(tmp_output_dir)
    assert directory.parent == tmp_output_dir
    assert mode_of(directory) == DIR_MODE


def test_create_artifact_dir_names_are_unique(tmp_output_dir: Path) -> None:
    first = create_artifact_dir(tmp_output_dir)
    second = create_artifact_dir(tmp_output_dir)
    assert first != second


def test_create_artifact_dir_reports_collision(
    tmp_output_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts, "_artifact_dir_name", lambda: "fixed-name")
    (tmp_output_dir / "fixed-name").mkdir()
    with pytest.raises(ToolError) as exc_info:
        create_artifact_dir(tmp_output_dir)
    assert exc_info.value.category is ErrorCategory.ARTIFACT_COLLISION


def test_write_bytes_is_private_under_loose_umask(
    tmp_path: Path, loose_umask: None
) -> None:
    target = tmp_path / "image.png"
    write_bytes(target, b"payload")
    assert target.read_bytes() == b"payload"
    assert mode_of(target) == FILE_MODE


def test_write_bytes_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "image.png"
    target.write_bytes("原有内容".encode())
    with pytest.raises(ToolError) as exc_info:
        write_bytes(target, "新内容".encode())
    assert exc_info.value.category is ErrorCategory.ARTIFACT_COLLISION
    assert target.read_bytes() == "原有内容".encode()


# --- receipt 落盘 ---


def test_write_receipt_uses_the_json_schema_key(tmp_path: Path) -> None:
    receipt = Receipt.model_validate(receipt_fields())
    write_receipt(tmp_path, receipt)
    written = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert written["schema"] == 1
    assert "schema_version" not in written
    assert written["state"] == "started"


def test_write_receipt_file_is_private_under_loose_umask(
    tmp_path: Path, loose_umask: None
) -> None:
    write_receipt(tmp_path, Receipt.model_validate(receipt_fields()))
    assert mode_of(tmp_path / "receipt.json") == FILE_MODE


def test_write_receipt_replaces_its_own_started_file(tmp_path: Path) -> None:
    started = Receipt.model_validate(receipt_fields())
    write_receipt(tmp_path, started)
    final = started.model_copy(update={"state": "completed"})
    write_receipt(tmp_path, final)
    written = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert written["state"] == "completed"


def test_write_receipt_refuses_a_foreign_receipt(tmp_path: Path) -> None:
    write_receipt(tmp_path, Receipt.model_validate(receipt_fields()))
    other = Receipt.model_validate({**receipt_fields(), "request_id": "另一次调用"})
    with pytest.raises(ToolError) as exc_info:
        write_receipt(tmp_path, other)
    assert exc_info.value.category is ErrorCategory.ARTIFACT_COLLISION


def test_write_receipt_refuses_a_stale_temporary_file(tmp_path: Path) -> None:
    """临时文件同样排他创建：撞上已存在的就报碰撞，不无条件删掉别人的文件。"""
    (tmp_path / ".receipt.json.tmp").write_text("别人的半份内容", encoding="utf-8")
    with pytest.raises(ToolError) as exc_info:
        write_receipt(tmp_path, Receipt.model_validate(receipt_fields()))
    assert exc_info.value.category is ErrorCategory.ARTIFACT_COLLISION
    assert not (tmp_path / "receipt.json").exists()


def test_write_receipt_leaves_no_temporary_file(tmp_path: Path) -> None:
    write_receipt(tmp_path, Receipt.model_validate(receipt_fields()))
    assert [item.name for item in tmp_path.iterdir()] == ["receipt.json"]


# --- 上游图片的解码验真 ---


def test_decode_output_returns_geometry() -> None:
    decoded = decode_output(base64.b64encode(png_bytes(40, 24)).decode("ascii"))
    assert (decoded.width, decoded.height) == (40, 24)
    assert decoded.format == "png"


def test_decode_output_rejects_invalid_base64() -> None:
    with pytest.raises(ToolError) as exc_info:
        decode_output("!!!! 这不是 base64 !!!!")
    assert exc_info.value.category is ErrorCategory.UPSTREAM_BAD_IMAGE


def test_decode_output_rejects_non_image() -> None:
    with pytest.raises(ToolError) as exc_info:
        decode_output(base64.b64encode("这不是图片".encode()).decode("ascii"))
    assert exc_info.value.category is ErrorCategory.UPSTREAM_BAD_IMAGE


def test_decode_output_rejects_non_png() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buffer, format="JPEG")
    with pytest.raises(ToolError) as exc_info:
        decode_output(base64.b64encode(buffer.getvalue()).decode("ascii"))
    assert exc_info.value.category is ErrorCategory.UPSTREAM_BAD_IMAGE


def test_decode_output_rejects_over_the_pixel_limit() -> None:
    encoded = base64.b64encode(png_bytes(4096, 4097, "L")).decode("ascii")
    with pytest.raises(ToolError) as exc_info:
        decode_output(encoded)
    assert exc_info.value.category is ErrorCategory.UPSTREAM_BAD_IMAGE


# --- 预览 ---


def test_write_preview_keeps_a_small_image_at_the_top_tier(
    tmp_path: Path, loose_umask: None
) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(png_bytes(64, 64))
    preview = write_preview(image_path, tmp_path)
    assert preview is not None
    assert preview.edge == 64
    assert Path(preview.path) == tmp_path / "preview.jpg"
    assert preview.bytes == (tmp_path / "preview.jpg").stat().st_size
    assert mode_of(tmp_path / "preview.jpg") == FILE_MODE


def test_write_preview_is_a_jpeg_within_the_budget(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(noise_png(3000))
    preview = write_preview(image_path, tmp_path)
    assert preview is not None
    data = (tmp_path / "preview.jpg").read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
    assert len(base64.b64encode(data)) <= PREVIEW_BASE64_BUDGET


def test_write_preview_steps_down_one_tier(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(noise_png(3000))
    preview = write_preview(image_path, tmp_path)
    assert preview is not None
    assert preview.edge == 512


def test_write_preview_steps_down_two_tiers(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(noise_png(800))
    preview = write_preview(image_path, tmp_path)
    assert preview is not None
    assert preview.edge == 384


def test_write_preview_gives_up_when_even_the_smallest_tier_is_too_big(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(noise_png(512))
    assert write_preview(image_path, tmp_path) is None
    assert not (tmp_path / "preview.jpg").exists()


def test_write_preview_maps_a_save_failure_to_a_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编码失败要变成本包的错误类别，不能让 OSError 穿透到调用链外面去。"""
    image_path = tmp_path / "image.png"
    image_path.write_bytes(png_bytes(64, 64))

    def failing_save(*args: object, **kwargs: object) -> None:
        raise OSError("写不出去")

    monkeypatch.setattr(Image.Image, "save", failing_save)
    with pytest.raises(ToolError) as exc_info:
        write_preview(image_path, tmp_path)
    assert exc_info.value.category is ErrorCategory.ARTIFACT_WRITE_FAILED


def test_preview_quality_is_the_documented_value() -> None:
    assert PREVIEW_QUALITY == 85
    assert PREVIEW_BASE64_BUDGET == 100 * 1024


def test_write_preview_does_not_touch_the_original(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    original = png_bytes(64, 64)
    image_path.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    write_preview(image_path, tmp_path)
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() == digest
