# pixelgate

pixelgate is a stdio MCP server that gives Claude Code two image tools, `generate_image` and `edit_image`. Requests go only to a CLIProxyAPI gateway on the loopback interface, which forwards them to GPT Image models using your own ChatGPT subscription; pixelgate itself holds no upstream credentials. It is an unofficial personal project and is not affiliated with or endorsed by OpenAI or Anthropic. Routing a subscription through a gateway may conflict with the providers' terms of service, so use it at your own risk. The rest of this document is in Chinese.

## 1. 这是什么

pixelgate 是一个 stdio 传输的 MCP 服务，向 Claude Code 提供生成图片与编辑图片两个工具。它把请求发给本机回环地址上的 CLIProxyAPI 网关，由网关用 ChatGPT 订阅的凭据转发到 GPT Image 后端。pixelgate 不持有任何上游凭据，也不调用网关的管理接口；每次调用生成一张图，产物连同回执写进调用方指定的目录。

支持的客户端是 Claude Code，包括经 claudex 启动的 Claude Code 会话（claudex 把 Claude Code 的模型请求转到同一个本机网关）。Codex 自带原生的图像生成能力，不需要也不支持接入本服务。

## 2. 前置条件

- uv，以及 uv 托管的 Python 3.14（`uv python install --no-bin 3.14`）。
- 一个在本机运行的 CLIProxyAPI 网关：已用 ChatGPT 订阅登录，监听 `127.0.0.1` 上的某个端口，并配置了一个供客户端使用的 key。本包要求这个 key 是 64 位小写十六进制串，可以用 `openssl rand -hex 32` 生成。
- 一个读取这个 key 的可执行文件（下称 helper）：无参数运行，stdout 恰好输出一行 key。最简单的写法是把 key 存进一个权限 0600 的文件，helper 只 `cat` 它：

  ```sh
  #!/bin/sh
  exec cat "$HOME/.config/pixelgate/client.key"
  ```

## 3. 安装

```bash
uv tool install --python 3.14 "pixelgate @ git+https://github.com/0-Br/pixelgate@v0.1.0"
```

安装后命令 `pixelgate` 在 `~/.local/bin` 下。运行依赖在 `pyproject.toml` 里钉死版本，因为 `uv tool install` 不读 `uv.lock`，钉死才能与开发环境一致。升级时换 tag 重新执行同一条命令并加 `--reinstall`。

## 4. 配置

```bash
mkdir -m 700 ~/.config/pixelgate
cp config.example.json ~/.config/pixelgate/config.json   # 在仓库根目录执行
chmod 600 ~/.config/pixelgate/config.json
pixelgate check-config --config ~/.config/pixelgate/config.json
```

`check-config` 对合法配置打印 `ok`，否则退出码 2，并在 stderr 给出错误类别。配置文件有四个字段，出现别的字段即拒绝：

| 字段 | 取值 |
| --- | --- |
| `version` | 恒为 1 |
| `base_url` | 只接受 `http://127.0.0.1:<端口>/v1`，端口必须显式写出，不带用户名、密码、query 与 fragment |
| `client_key_helper` | helper 的绝对路径，必须可执行；每次请求前无参数调用一次，超时 5 秒，stdout 必须恰为一行 64 位小写十六进制 |
| `model_routes` | 两个键固定为 `gpt-image-2.5-sunburst` 与 `gpt-image-2.5-flare`，值是发往网关的请求名，缺任一键即拒绝 |

`model_routes` 的值必须是只归属订阅凭据的请求名。同一个模型名可能同时登记在网关里别的 provider（例如按量计费的 API key）下面，而网关不提供按 provider 选路，所以名字相同不等于走同一条路由。填写前经网关管理接口 `GET /v0/management/auth-files` 与 `/v0/management/auth-files/models?name=<文件名>` 核对：两个名字只出现在订阅登录的那份凭据上才能直接填；否则先在网关里给订阅凭据配一个唯一的别名（`oauth-model-alias`），再把别名填进来。核对时只看文件名、provider、账户类型与模型名，凭据文件的其余字段可能含真实的 key，不要打印。

配置文件缺失、JSON 非法、版本不符、有未知字段、helper 不存在或不可执行时，服务一启动就以退出码 2 结束。网关没有运行时，工具调用返回错误类别 `gateway_unreachable`；pixelgate 不负责拉起网关。

## 5. 在 Claude Code 中注册

```bash
claude mcp add --scope user pixelgate -- ~/.local/bin/pixelgate --config ~/.config/pixelgate/config.json
```

注册后工具名是 `mcp__pixelgate__generate_image` 与 `mcp__pixelgate__edit_image`。两个工具在工具清单里都带 `_meta["anthropic/requiresUserInteraction"]=true`，Claude Code 每次调用前都会请用户批准。

## 6. 工具

| 工具 | 必填 | 可选 | 行为 |
| --- | --- | --- | --- |
| `generate_image` | `prompt`（非空，不超过 32,000 字符）、`output_dir`（已存在目录的绝对路径） | `references`（图片引用数组，0 到 5 张）、`model`、`size`、`quality`、`background` | 没有 `references` 时走 generations 端点；有则走 edits 端点，参考图依次作为输入图 |
| `edit_image` | `prompt`、`parent`（图片引用）、`output_dir` | `references`（0 到 4 张，与 parent 合计不超过 5）、`mask`（图片引用）、`model`、`size`、`quality`、`background` | 走 edits 端点，parent 为第一张输入图，references 依次在后；回执记录 parent |

图片引用的形态是 `{"path": "<绝对路径>", "sha256": "<64 位小写十六进制>"}`。发出请求前逐张核对：文件存在；不超过 32 MiB；哈希与声明一致；Pillow 能识别（png、jpeg、webp）并完整解码；宽或高不超过 8192，总像素不超过 16,777,216。发送的是读取时形成的内存快照。mask 必须与 parent 格式相同、尺寸相同，并且带 alpha 通道。

| 参数 | 取值 |
| --- | --- |
| `model` | `gpt-image-2.5-sunburst`（默认）或 `gpt-image-2.5-flare`；裸名 `gpt-image-2.5`、带日期的快照名与其他型号一律拒绝 |
| `size` | `auto`（默认）或 `<W>x<H>`：W 与 H 为 16 的倍数，宽高比在 1:3 到 3:1 之间，单边不超过 3840，总像素在 655,360 到 3,686,400 之间。官方上限是 8,294,400，超过 3,686,400 的部分被官方标为实验性，以 `size_experimental` 拒绝；超过 8,294,400 以 `size_invalid` 拒绝 |
| `quality` | `auto`（默认）、`low`、`medium`、`high`、`xhigh`、`max` |
| `background` | `auto`（默认）、`opaque`、`transparent`；透明背景要求输出 png 或 webp，本包固定请求 png，产物带 alpha 通道；后端是否真的给出透明背景，以回执与解码结果为准 |

每次请求固定发送 `n=1`、`output_format="png"`、`stream=False`，客户端不重试。HTTP 超时为 600 秒，按单次读写操作计，所以上游持续慢速回传时，整次调用可能超过 600 秒。同一个服务进程里已有调用在跑时，第二个调用立即返回 `busy`，不排队；锁只在进程内生效，同时开着的两个 Claude Code 会话各连一个服务进程，彼此不互斥。

本版没有取消通道：客户端取消一次调用时，服务仍等工作线程跑完，那一次的产物与终态回执照常写盘（成功即 `completed`），只是客户端拿不到返回。要知道结果，读 `output_dir` 下最新产物目录里的 `receipt.json`。

成功时，`structuredContent` 是回执摘要，`content` 是一段文本（图片路径、预览路径、尺寸、格式、state）加一个 JPEG 预览图像块。预览质量 85，最长边从 768 像素起，按 100 KB 的 base64 预算依次降到 512、384；仍超预算就省略图像块，并在 `warnings` 里记 `preview_omitted`。失败时 `isError=true`，文本只含错误类别与不含敏感信息的字段，`structuredContent` 同样是回执摘要。

## 7. 产物

每次调用在 `output_dir` 下排他创建目录 `<UTC 时间戳>-<uuid4 前 8 位>/`（目录权限 0700，文件 0600），内容如下：

| 文件 | 内容 |
| --- | --- |
| `inputs/` | 输入图与 mask 的快照副本：`parent.<ext>`、`ref-<n>.<ext>`、`mask.<ext>`；没有输入图的纯生成调用不建这个目录 |
| `prompt.txt` | 发出的 prompt |
| `request.json` | 发出的参数，不含凭据与图片字节 |
| `receipt.json` | 回执，字段见下表 |
| `image.png` | 上游返回的原图，不转码 |
| `preview.jpg` | 独立的预览副本 |

输入预检失败时不创建目录；任何目标文件已经存在即报 `artifact_collision`，不覆盖。

回执字段（缺失的值写 null，不拿请求值冒充返回值）：

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `schema` | int | 恒为 1 |
| `request_id` | str | 本地生成的 uuid4 |
| `operation` | str | `generate` 或 `edit` |
| `state` | str | `started`、`completed`、`failed`、`unknown` |
| `requested_model`、`route_model` | str | 调用方给的型号，与发往网关的请求名 |
| `actual_model` | str 或 null | 上游响应里的 model 字段 |
| `requested_size`、`requested_quality`、`requested_background` | str | 发出的值 |
| `parent`、`references`、`mask` | 引用对象、数组、引用对象或 null | 输入图引用 |
| `started_at` | str | ISO 8601 UTC，在请求发出前写入 |
| `finished_at` | str 或 null | 到达终态的时间 |
| `output` | 对象或 null | `{path, sha256, format, width, height, bytes}` |
| `size_mismatch` | bool | 请求的尺寸不是 auto、实际尺寸又不同时为 true |
| `usage` | 对象或 null | 上游 usage 中的 `input_tokens`、`output_tokens`、`total_tokens` |
| `upstream_request_id` | str 或 null | 响应头 `x-request-id` |
| `revised_prompt` | str 或 null | 上游返回的改写后 prompt，原样记录 |
| `preview` | 对象或 null | `{path, bytes, edge}` |
| `error` | 对象或 null | `{category, http_status, message_safe}` |
| `warnings` | str 数组 | `size_mismatch`、`preview_omitted` |

state 的含义：`started` 在请求发出前写入；`completed` 只在图片解码成功、全部文件写盘之后写入；请求发出后连接中断、超时或写盘前中断，写 `unknown`；本地预检失败、建连失败（请求没有发出）与上游返回明确错误，写 `failed`。持久回执停在 `started` 的，是进程被杀或机器重启留下的，读取者应理解为「上游结果未知」；本版不做恢复扫描。MCP 返回的摘要包含 `request_id`、`operation`、`state`、`requested_model`、`actual_model`、`output`、`preview`、`size_mismatch`、`usage`、`error`、`warnings`、`artifact_dir`。

`output_dir` 由调用方给出，本包不设默认值。建议固定用一个用户级的生成库，例如 `~/.local/share/pixelgate/`，放在任何 git 仓库之外：每个时间戳目录是一次调用的过程产物，用于溯源与核对额度，数量会随着试构图和改稿不断增长。选中的成品由使用者复制进项目自己的资产目录，随项目做版本管理。清理时按时间戳目录整个删除，删之前看一眼 `receipt.json`，确认没有别处还在引用它；本包不做自动清理。

## 8. 错误类别

下表是完整枚举：

| 类别 | 含义 |
| --- | --- |
| `config_invalid` | 配置文件缺失、非法或不符合第 4 节 |
| `auth_helper_failed` | helper 非零退出、超时或输出格式不符 |
| `request_invalid` | 参数形态错误：类型不符、超长、`output_dir` 不是已存在目录的绝对路径 |
| `input_missing`、`input_hash_mismatch`、`input_undecodable`、`input_too_large` | 输入图不存在、哈希不符、不能完整解码、超过字节或像素上限 |
| `mask_mismatch` | mask 与 parent 的格式或尺寸不同，或没有 alpha 通道 |
| `model_not_allowed`、`size_invalid`、`size_experimental`、`quality_invalid`、`background_invalid` | 型号不在白名单、尺寸不合法、尺寸落在实验区间、质量取值非法、背景取值非法 |
| `target_denied` | 传输层拒绝了发往非回环地址或非 Images 路径的请求；正常调用不会触发，只出现在测试与日志里 |
| `gateway_unreachable` | 连不上配置里的网关，请求没有发出；`state` 为 `failed`，不消耗额度 |
| `upstream_interrupted` | 请求已发出，连接在响应读完之前中断；`state` 为 `unknown`，上游可能已经生成并消耗了额度 |
| `upstream_error` | 上游返回明确的错误状态 |
| `upstream_no_image`、`upstream_bad_image` | 响应里的图片数量不为 1，或 base64 非法、解码失败、超过像素上限 |
| `response_too_large` | 响应累计读取超过 64 MiB，中止 |
| `artifact_collision`、`artifact_write_failed` | 产物目标已存在，或写盘失败 |
| `busy` | 已有调用在跑 |
| `timeout` | 单次读写操作超过 600 秒 |
| `cancelled` | 调用被取消；本版没有取消通道，不会写出这个类别（见第 6 节） |

## 9. 安全边界

- 构造客户端前，把 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_CUSTOM_HEADERS` 与各个代理环境变量从进程环境里移除，不读 `.env`。
- 传输层只放行 `http://127.0.0.1:<配置端口>` 下的 `/v1/images/generations` 与 `/v1/images/edits`，不跟随重定向。
- client key 只在进程内经 helper 取得，不进参数、日志、回执与返回。
- stdout 上只有 MCP 协议帧；诊断信息写 stderr，而且不含 prompt、图片内容、上游原文与凭据。

## 10. 开发

```bash
uv sync --locked --group dev --python 3.14
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked basedpyright
```

测试全部离线：`tests/conftest.py` 提供合成图片、假 HOME 下的合成 key、回环上的假网关、代理陷阱与诱饵网关，不访问真实网关。类型检查相对仓库里的基线 `.basedpyright/baseline.json` 不新增 error。

## 11. 许可证

MIT，见 [LICENSE](LICENSE)。
