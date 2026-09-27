"""schemas 模块的校验规则，以及错误类别、receipt 键名、工具 schema 的契约锁定。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pixelgate.schemas import (
    CATEGORY_MESSAGES,
    EditRequest,
    ErrorCategory,
    GenerateRequest,
    ImageRef,
    Receipt,
    ReceiptSummary,
    ToolError,
    WarningKind,
    parse_config,
    resolve_model,
    validate_background,
    validate_quality,
    validate_size,
)

# 计划 §4 的错误类别完整枚举，逐字列出用于比对，不从被测模块反推。
ERROR_CATEGORIES = [
    "config_invalid",
    "auth_helper_failed",
    "request_invalid",
    "input_missing",
    "input_hash_mismatch",
    "input_undecodable",
    "input_too_large",
    "mask_mismatch",
    "model_not_allowed",
    "size_invalid",
    "size_experimental",
    "quality_invalid",
    "background_invalid",
    "target_denied",
    "gateway_unreachable",
    "upstream_interrupted",
    "upstream_error",
    "upstream_no_image",
    "upstream_bad_image",
    "response_too_large",
    "artifact_collision",
    "artifact_write_failed",
    "busy",
    "timeout",
    "cancelled",
]

# 计划 §4 的 receipt 键名，逐字列出。
RECEIPT_KEYS = {
    "schema",
    "request_id",
    "operation",
    "state",
    "requested_model",
    "route_model",
    "actual_model",
    "requested_size",
    "requested_quality",
    "requested_background",
    "parent",
    "references",
    "mask",
    "started_at",
    "finished_at",
    "output",
    "size_mismatch",
    "usage",
    "upstream_request_id",
    "revised_prompt",
    "preview",
    "error",
    "warnings",
}

# 计划 §4 的摘要子集，即 MCP structuredContent 的字段。
SUMMARY_KEYS = {
    "request_id",
    "operation",
    "state",
    "requested_model",
    "actual_model",
    "output",
    "preview",
    "size_mismatch",
    "usage",
    "error",
    "warnings",
    "artifact_dir",
}

# 不得出现在 receipt、返回摘要与工具 schema 里的键：prompt 与凭据是不外泄的内容，
# 其余是不暴露给调用方的上游参数。
RETIRED_KEYS = {
    "prompt",
    "api_key",
    "authorization",
    "b64_json",
    "url",
    "image_url",
    "n",
    "background",
    "output_compression",
    "partial_images",
    "moderation",
    "input_fidelity",
    "stream",
}

SUNBURST = "gpt-image-2.5-sunburst"
FLARE = "gpt-image-2.5-flare"
HEX64 = "a3f1" * 16


def receipt_fields() -> dict[str, Any]:
    """一份 started 态 receipt 的必填字段。"""
    return {
        "request_id": "0f8c2b1a-3d4e-4f50-9a61-7b2c3d4e5f60",
        "operation": "generate",
        "state": "started",
        "requested_model": SUNBURST,
        "route_model": SUNBURST,
        "requested_size": "auto",
        "requested_quality": "auto",
        "requested_background": "auto",
        "started_at": "2026-09-13T12:00:00Z",
    }


# --- 错误类别与 warning 枚举 ---


def test_error_category_members_match_the_plan() -> None:
    assert sorted(category.value for category in ErrorCategory) == sorted(
        ERROR_CATEGORIES
    )


def test_every_error_category_has_a_fixed_message() -> None:
    assert set(CATEGORY_MESSAGES) == set(ErrorCategory)


def test_warning_kind_members_are_closed() -> None:
    assert sorted(kind.value for kind in WarningKind) == [
        "preview_omitted",
        "size_mismatch",
    ]


def test_tool_error_carries_the_fixed_category_message() -> None:
    error = ToolError(ErrorCategory.SIZE_INVALID)
    assert error.category is ErrorCategory.SIZE_INVALID
    assert error.http_status is None
    assert error.message_safe == CATEGORY_MESSAGES[ErrorCategory.SIZE_INVALID]


def test_tool_error_appends_the_detail() -> None:
    error = ToolError(
        ErrorCategory.UPSTREAM_ERROR, http_status=503, detail="/tmp/a.png"
    )
    assert error.http_status == 503
    assert error.message_safe.startswith(
        CATEGORY_MESSAGES[ErrorCategory.UPSTREAM_ERROR]
    )
    assert error.message_safe.endswith("/tmp/a.png")


# --- 配置 ---


def test_parse_config_accepts_the_example(config_data: dict[str, Any]) -> None:
    config = parse_config(config_data)
    assert config.version == 1
    assert config.port == 8317
    assert config.model_routes[SUNBURST] == SUNBURST
    assert config.model_routes[FLARE] == FLARE


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8317/v1",
        "https://127.0.0.1:8317/v1",
        "http://127.0.0.1/v1",
        "http://user@127.0.0.1:8317/v1",
        "http://user:secret@127.0.0.1:8317/v1",
        # http 的默认端口会被传输层归一化成空，配置层放行它等于让每次调用都撞守卫。
        "http://127.0.0.1:80/v1",
        "http://127.0.0.1:8317/v1?debug=1",
        "http://127.0.0.1:8317/v1#frag",
        "http://127.0.0.2:8317/v1",
        "http://[::1]:8317/v1",
        "http://127.0.0.1:8317",
        "http://127.0.0.1:8317/",
        "http://127.0.0.1:8317/v1/",
        "http://127.0.0.1:8317/v2",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:99999/v1",
        "127.0.0.1:8317/v1",
        "",
    ],
)
def test_parse_config_rejects_base_url_outside_the_allowed_form(
    config_data: dict[str, Any], base_url: str
) -> None:
    config_data["base_url"] = base_url
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_parse_config_rejects_unknown_field(config_data: dict[str, Any]) -> None:
    config_data["api_key"] = "test-client-key"
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


@pytest.mark.parametrize("version", [0, 2, None, "two"])
def test_parse_config_rejects_other_versions(
    config_data: dict[str, Any], version: object
) -> None:
    config_data["version"] = version
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


@pytest.mark.parametrize("missing", [SUNBURST, FLARE])
def test_parse_config_rejects_missing_route(
    config_data: dict[str, Any], missing: str
) -> None:
    del config_data["model_routes"][missing]
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_parse_config_rejects_extra_route(config_data: dict[str, Any]) -> None:
    config_data["model_routes"]["gpt-image-2"] = "gpt-image-2"
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


@pytest.mark.parametrize("route", ["", "   "])
def test_parse_config_rejects_blank_route_value(
    config_data: dict[str, Any], route: str
) -> None:
    config_data["model_routes"][FLARE] = route
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_parse_config_rejects_missing_helper(
    config_data: dict[str, Any], tmp_path: Path
) -> None:
    config_data["client_key_helper"] = str(tmp_path / "absent.sh")
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_parse_config_rejects_non_executable_helper(
    config_data: dict[str, Any], tmp_path: Path
) -> None:
    helper = tmp_path / "helper.sh"
    helper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    helper.chmod(0o644)
    config_data["client_key_helper"] = str(helper)
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_parse_config_rejects_relative_helper(config_data: dict[str, Any]) -> None:
    config_data["client_key_helper"] = "lib/read_client_key.sh"
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_parse_config_rejects_directory_as_helper(
    config_data: dict[str, Any], tmp_path: Path
) -> None:
    config_data["client_key_helper"] = str(tmp_path)
    with pytest.raises(ToolError) as exc_info:
        parse_config(config_data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


def test_parse_config_accepts_another_executable_helper(
    config_data: dict[str, Any], tmp_path: Path
) -> None:
    helper = tmp_path / "helper.sh"
    helper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    helper.chmod(0o755)
    config_data["client_key_helper"] = str(helper)
    assert parse_config(config_data).client_key_helper == str(helper)


@pytest.mark.parametrize("data", [None, [], "config", 3])
def test_parse_config_rejects_non_object(data: object) -> None:
    with pytest.raises(ToolError) as exc_info:
        parse_config(data)
    assert exc_info.value.category is ErrorCategory.CONFIG_INVALID


@pytest.mark.parametrize("port", [8317, 1, 65535])
def test_config_port_comes_from_the_base_url(
    config_data: dict[str, Any], port: int
) -> None:
    config_data["base_url"] = f"http://127.0.0.1:{port}/v1"
    assert parse_config(config_data).port == port


# --- 型号白名单 ---


@pytest.mark.parametrize("alias", [SUNBURST, FLARE])
def test_resolve_model_accepts_the_two_aliases(
    config_data: dict[str, Any], alias: str
) -> None:
    config = parse_config(config_data)
    assert resolve_model(alias, config) == config.model_routes[alias]


def test_resolve_model_returns_the_configured_route_name(
    config_data: dict[str, Any],
) -> None:
    config_data["model_routes"][SUNBURST] = "gateway-sunburst"
    config = parse_config(config_data)
    assert resolve_model(SUNBURST, config) == "gateway-sunburst"


@pytest.mark.parametrize(
    "model",
    [
        "gpt-image-2.5",
        "gpt-image-2.5-sunburst-2026-08-01",
        "gpt-image-2.5-sunburst-2026-09-08",
        "gpt-image-2.5-flare-2026-09-08",
        "gpt-image-2",
        "gpt-image-1.5",
        "chatgpt-image-latest",
        "GPT-Image-2.5-Sunburst",
        "",
    ],
)
def test_resolve_model_rejects_everything_outside_the_whitelist(
    config_data: dict[str, Any], model: str
) -> None:
    config = parse_config(config_data)
    with pytest.raises(ToolError) as exc_info:
        resolve_model(model, config)
    assert exc_info.value.category is ErrorCategory.MODEL_NOT_ALLOWED


# --- 尺寸合法域 ---


@pytest.mark.parametrize(
    "size",
    [
        "auto",
        "1024x1024",
        "1536x1024",
        "1024x1536",
        "2560x1440",
        "1440x2560",
        "640x1024",
        "1024x640",
    ],
)
def test_validate_size_accepts_the_legal_domain(size: str) -> None:
    assert validate_size(size) is None


@pytest.mark.parametrize("size", ["2576x1440", "1440x2576", "3840x2160", "2160x3840"])
def test_validate_size_rejects_the_experimental_band(size: str) -> None:
    with pytest.raises(ToolError) as exc_info:
        validate_size(size)
    assert exc_info.value.category is ErrorCategory.SIZE_EXPERIMENTAL


@pytest.mark.parametrize(
    "size",
    [
        "1000x1000",
        "1024x3088",
        "3088x1024",
        "3856x1296",
        "624x1024",
        "3840x2176",
        "0x0",
        "abc",
        "1024x1024x1",
        "1024X1024",
        " 1024x1024",
        "1024x1024 ",
        "1024*1024",
        "-1024x1024",
        "",
        "AUTO",
    ],
)
def test_validate_size_rejects_illegal_sizes(size: str) -> None:
    with pytest.raises(ToolError) as exc_info:
        validate_size(size)
    assert exc_info.value.category is ErrorCategory.SIZE_INVALID


# --- quality ---


@pytest.mark.parametrize("quality", ["auto", "low", "medium", "high", "xhigh", "max"])
def test_validate_quality_accepts_the_six_values(quality: str) -> None:
    assert validate_quality(quality) is None


@pytest.mark.parametrize("quality", ["ultra", "", "standard", "hd", "HIGH", "Auto"])
def test_validate_quality_rejects_other_values(quality: str) -> None:
    with pytest.raises(ToolError) as exc_info:
        validate_quality(quality)
    assert exc_info.value.category is ErrorCategory.QUALITY_INVALID


# --- background ---


@pytest.mark.parametrize("background", ["auto", "opaque", "transparent"])
def test_validate_background_accepts_the_three_values(background: str) -> None:
    assert validate_background(background) is None


@pytest.mark.parametrize(
    "background", ["checkerboard", "", "Transparent", "TRANSPARENT", "none"]
)
def test_validate_background_rejects_other_values(background: str) -> None:
    with pytest.raises(ToolError) as exc_info:
        validate_background(background)
    assert exc_info.value.category is ErrorCategory.BACKGROUND_INVALID


# --- 图片引用 ---


def test_image_ref_accepts_absolute_path_and_lowercase_hex() -> None:
    ref = ImageRef(path="/tmp/a.png", sha256=HEX64)
    assert ref.path == "/tmp/a.png"
    assert ref.sha256 == HEX64


def test_make_png_fixture_returns_a_valid_ref(
    make_png: Callable[..., ImageRef],
) -> None:
    ref = make_png(32, 48, "RGBA")
    assert Path(ref.path).is_file()
    assert len(ref.sha256) == 64


@pytest.mark.parametrize(
    "sha256",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "", " " + "a" * 63, HEX64.upper()],
    ids=["upper", "short", "long", "non_hex", "empty", "leading_space", "hex_upper"],
)
def test_image_ref_rejects_bad_sha256(sha256: str) -> None:
    with pytest.raises(ValidationError):
        ImageRef(path="/tmp/a.png", sha256=sha256)


@pytest.mark.parametrize("path", ["a.png", "./a.png", "~/a.png", ""])
def test_image_ref_rejects_non_absolute_path(path: str) -> None:
    with pytest.raises(ValidationError):
        ImageRef(path=path, sha256=HEX64)


def test_image_ref_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ImageRef.model_validate({"path": "/tmp/a.png", "sha256": HEX64, "role": "ref"})


# --- 两个工具的入参 ---


def test_generate_request_defaults(tmp_output_dir: Path) -> None:
    request = GenerateRequest(prompt="一张示意图", output_dir=str(tmp_output_dir))
    assert request.model == SUNBURST
    assert request.size == "auto"
    assert request.quality == "auto"
    assert request.background == "auto"
    assert request.references == []


@pytest.mark.parametrize(
    "prompt",
    ["", "   ", "x" * 32001],
    ids=["empty", "blank", "over_limit"],
)
def test_generate_request_rejects_bad_prompt(prompt: str, tmp_output_dir: Path) -> None:
    with pytest.raises(ValidationError):
        GenerateRequest(prompt=prompt, output_dir=str(tmp_output_dir))


def test_generate_request_accepts_prompt_at_the_limit(tmp_output_dir: Path) -> None:
    request = GenerateRequest(prompt="x" * 32000, output_dir=str(tmp_output_dir))
    assert len(request.prompt) == 32000


def test_generate_request_rejects_relative_output_dir() -> None:
    with pytest.raises(ValidationError):
        GenerateRequest(prompt="一张示意图", output_dir="output")


def test_generate_request_rejects_absent_output_dir(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        GenerateRequest(prompt="一张示意图", output_dir=str(tmp_path / "absent"))


def test_generate_request_rejects_file_as_output_dir(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError):
        GenerateRequest(prompt="一张示意图", output_dir=str(target))


def test_generate_request_accepts_five_references(
    tmp_output_dir: Path, make_png: Callable[..., ImageRef]
) -> None:
    references = [make_png() for _ in range(5)]
    request = GenerateRequest(
        prompt="一张示意图",
        output_dir=str(tmp_output_dir),
        references=references,
    )
    assert len(request.references) == 5


def test_generate_request_rejects_six_references(
    tmp_output_dir: Path, make_png: Callable[..., ImageRef]
) -> None:
    references = [make_png() for _ in range(6)]
    with pytest.raises(ValidationError):
        GenerateRequest(
            prompt="一张示意图",
            output_dir=str(tmp_output_dir),
            references=references,
        )


def test_generate_request_rejects_unknown_field(tmp_output_dir: Path) -> None:
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(
            {
                "prompt": "一张示意图",
                "output_dir": str(tmp_output_dir),
                "n": 2,
            }
        )


def test_generate_request_input_schema_lists_the_tool_parameters() -> None:
    schema = GenerateRequest.model_json_schema()
    assert set(schema["properties"]) == {
        "prompt",
        "output_dir",
        "references",
        "model",
        "size",
        "quality",
        "background",
    }
    assert set(schema["required"]) == {"prompt", "output_dir"}


def test_edit_request_defaults(
    tmp_output_dir: Path, make_png: Callable[..., ImageRef]
) -> None:
    request = EditRequest(
        prompt="把背景换成夜色",
        parent=make_png(),
        output_dir=str(tmp_output_dir),
    )
    assert request.model == SUNBURST
    assert request.mask is None
    assert request.references == []


def test_edit_request_requires_parent(tmp_output_dir: Path) -> None:
    with pytest.raises(ValidationError):
        EditRequest.model_validate(
            {"prompt": "把背景换成夜色", "output_dir": str(tmp_output_dir)}
        )


def test_edit_request_accepts_four_references_and_a_mask(
    tmp_output_dir: Path, make_png: Callable[..., ImageRef]
) -> None:
    request = EditRequest(
        prompt="把背景换成夜色",
        parent=make_png(),
        output_dir=str(tmp_output_dir),
        references=[make_png() for _ in range(4)],
        mask=make_png(mode="RGBA"),
    )
    assert len(request.references) == 4
    assert request.mask is not None


def test_edit_request_rejects_five_references(
    tmp_output_dir: Path, make_png: Callable[..., ImageRef]
) -> None:
    with pytest.raises(ValidationError):
        EditRequest(
            prompt="把背景换成夜色",
            parent=make_png(),
            output_dir=str(tmp_output_dir),
            references=[make_png() for _ in range(5)],
        )


def test_edit_request_rejects_unknown_field(
    tmp_output_dir: Path, make_png: Callable[..., ImageRef]
) -> None:
    with pytest.raises(ValidationError):
        EditRequest.model_validate(
            {
                "prompt": "把背景换成夜色",
                "parent": make_png().model_dump(),
                "output_dir": str(tmp_output_dir),
                "input_fidelity": "high",
            }
        )


def test_edit_request_input_schema_lists_the_tool_parameters() -> None:
    schema = EditRequest.model_json_schema()
    assert set(schema["properties"]) == {
        "prompt",
        "parent",
        "output_dir",
        "references",
        "mask",
        "model",
        "size",
        "quality",
        "background",
    }
    assert set(schema["required"]) == {"prompt", "parent", "output_dir"}


# --- receipt 与返回摘要 ---


def test_receipt_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError):
        Receipt.model_validate({})


@pytest.mark.parametrize("field", list(receipt_fields()))
def test_receipt_rejects_each_missing_required_field(field: str) -> None:
    fields = receipt_fields()
    del fields[field]
    with pytest.raises(ValidationError):
        Receipt.model_validate(fields)


def test_receipt_keys_match_the_plan() -> None:
    receipt = Receipt.model_validate(receipt_fields())
    assert set(receipt.model_dump(by_alias=True)) == RECEIPT_KEYS


def test_receipt_schema_key_is_one() -> None:
    receipt = Receipt.model_validate(receipt_fields())
    assert receipt.model_dump(by_alias=True)["schema"] == 1


def test_receipt_round_trips_through_its_json_form() -> None:
    receipt = Receipt.model_validate(receipt_fields())
    assert Receipt.model_validate(receipt.model_dump(by_alias=True)) == receipt


def test_receipt_carries_no_retired_keys() -> None:
    receipt = Receipt.model_validate(receipt_fields())
    assert RETIRED_KEYS.isdisjoint(receipt.model_dump(by_alias=True))


def test_receipt_defaults_are_empty_not_fabricated() -> None:
    receipt = Receipt.model_validate(receipt_fields())
    assert receipt.actual_model is None
    assert receipt.finished_at is None
    assert receipt.output is None
    assert receipt.usage is None
    assert receipt.error is None
    assert receipt.references == []
    assert receipt.warnings == []


@pytest.mark.parametrize("state", ["done", "ok", "", "STARTED"])
def test_receipt_rejects_unknown_state(state: str) -> None:
    with pytest.raises(ValidationError):
        Receipt.model_validate({**receipt_fields(), "state": state})


@pytest.mark.parametrize("operation", ["create", "generate_image", ""])
def test_receipt_rejects_unknown_operation(operation: str) -> None:
    with pytest.raises(ValidationError):
        Receipt.model_validate({**receipt_fields(), "operation": operation})


def test_receipt_rejects_unknown_warning() -> None:
    with pytest.raises(ValidationError):
        Receipt.model_validate({**receipt_fields(), "warnings": ["slow"]})


def test_receipt_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        Receipt.model_validate({**receipt_fields(), "b64_json": "AAAA"})


def test_summary_is_the_documented_subset() -> None:
    receipt = Receipt.model_validate(receipt_fields())
    summary = receipt.summary("/tmp/pictures/20260913-abcd1234")
    assert set(summary.model_dump()) == SUMMARY_KEYS
    assert summary.artifact_dir == "/tmp/pictures/20260913-abcd1234"
    assert summary.request_id == receipt.request_id
    assert summary.state == "started"


def test_summary_carries_the_completed_payload() -> None:
    fields = receipt_fields()
    fields["state"] = "completed"
    fields["actual_model"] = SUNBURST
    fields["size_mismatch"] = True
    fields["warnings"] = ["size_mismatch"]
    fields["output"] = {
        "path": "/tmp/pictures/run/image.png",
        "sha256": HEX64,
        "format": "png",
        "width": 1024,
        "height": 1024,
        "bytes": 12345,
    }
    receipt = Receipt.model_validate(fields)
    summary = receipt.summary("/tmp/pictures/run")
    assert summary.output is not None
    assert summary.output.width == 1024
    assert summary.size_mismatch is True
    assert summary.warnings == [WarningKind.SIZE_MISMATCH]


def test_summary_output_schema_carries_no_retired_keys() -> None:
    properties = ReceiptSummary.model_json_schema()["properties"]
    assert set(properties) == SUMMARY_KEYS
    assert RETIRED_KEYS.isdisjoint(properties)
