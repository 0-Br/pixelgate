# pixelgate

开始任何工作前，先列出并逐字读取 `.claude/rules/` 下全部 `.md` 文件（Claude Code 已自动加载该目录，无需重复读取）。

pixelgate 是一个 stdio 传输的 MCP 服务，经本机回环网关调用 GPT Image 后端，向 Claude Code 提供生成与编辑图片两个工具。它是一个 Python 包，uv 项目，Python 3.14。本文件只记在本仓库工作需要的信息；README 面向使用者，写安装、配置与对外接口，本文件面向在本仓库做开发的 agent，两者不互相复述。默认模式：开发模式。

## 首次阅读

开始工作前按顺序逐字读：

1. `docs/architecture.md`：模块分工、出站约束、state 语义与技术栈。
2. `docs/iteration.md`：当前状态、已知问题与路线图。
3. `README.md` 第 4 节到第 9 节：配置、注册、工具、产物、错误类别与安全边界，这是本包的对外接口，改动前必须知道现状。

`docs/decisions.md` 是决策日志，不通读，按关键词检索后读命中的条目。

## 受管接口

| 受管状态 | 唯一写入通道 |
| --- | --- |
| 项目环境 `.venv/` 与锁文件 `uv.lock` | uv 命令；运行与测试一律带 `--locked`，加减依赖与改锁（`uv add`、`uv remove`、`uv lock`）由维护者执行 |
| 类型检查基线 `.basedpyright/baseline.json` | `uv run --locked basedpyright --writebaseline`，只在存量诊断清掉一批之后重写并单独提交；没有存量时基线是空形态 `{"files": {}}`，因为这时 `--writebaseline` 不生成文件，而基线缺失时收尾检查会跳过类型检查 |
| 用户配置 `~/.config/pixelgate/config.json` | 用户手工维护；本包只读它，代码与测试都不写 |
| 产物目录（`output_dir` 下的时间戳目录） | 只由工具调用本身创建；清理按时间戳目录整目录删除 |

本表是完整枚举，新增受管状态在此加一行。

## 开发与验证

改动与验证本项目时用到的命令与约定：

| 项 | 取值 |
| --- | --- |
| 测试命令 | 全量：`uv run --locked pytest`；收集：`uv run --locked pytest --collect-only -q`。首次运行前 `uv sync --locked --group dev --python 3.14` |
| CLI 前缀 | `uv run --locked pixelgate`（例如 `uv run --locked pixelgate check-config --config <配置路径>`） |
| 必读文档清单 | 见首次阅读 |
| 变更同步矩阵（改了什么就要同步什么） | 见下方「变更同步矩阵」一节 |
| 记账载体（决策与状态记在哪里） | 决策日志 `docs/decisions.md`；状态快照、已知问题与路线图 `docs/iteration.md` |
| 批次验证映射（改了哪些文件就跑哪些测试） | `src/pixelgate/client.py` → `tests/test_client.py`；`artifacts.py` → `tests/test_artifacts.py`；`server.py` → `tests/test_server.py`；`schemas.py`、`tests/conftest.py`、`pyproject.toml` 是共享底座，改了就跑全量。类型检查不收窄，每批都对整个项目跑 |
| lint 与 format 命令与政策 | `uv run --locked ruff check .` 与 `uv run --locked ruff format --check .`，零违规。ruff 配置写在 `pyproject.toml` 的 `[tool.ruff]`，自足、不继承别处的配置；ruff 版本由 `uv.lock` 钉住，所以只用项目环境里的 ruff，不用 PATH 上的 |
| 机械核查命令（lint 之外的检查） | `uv run --locked basedpyright`，standard 档，相对 `.basedpyright/baseline.json` 不新增 error |
| 严重度本域举例（三级严重度在本项目里长什么样） | critical：请求能发往回环网关之外的地址，client key 进了日志、回执或返回，产物覆盖了已有文件，`completed` 回执对应的图片没有写全。warning：错误类别与 README 第 8 节不一致，回执字段缺失值被填成请求值，测试依赖了真实网关。info：错误消息措辞，预览尺寸取舍，文档排版 |

## 变更同步矩阵

本表是完整枚举。

| 变更 | 必须同步 |
| --- | --- |
| 工具参数或返回形态 | `schemas.py` 的模型、README 第 6 节与第 7 节、`tests/test_schemas.py`、`tests/test_server.py` |
| 错误类别的增删 | `schemas.py` 的 `ErrorCategory`、README 第 8 节、测试里的枚举断言 |
| 凭据读取、传输守卫或 state 语义 | README 第 7 节的 state 说明与第 9 节、`docs/architecture.md` 的对应节、`tests/test_client.py`、`tests/test_artifacts.py` 里的安全用例 |
| 配置字段 | `schemas.py` 的 `Config`、`config.example.json`、README 第 4 节、`tests/test_schemas.py` |
| 运行依赖的版本 | `pyproject.toml` 的钉死版本；由维护者 `uv lock` 后重跑全部验证 |
| 发布新版本 | `src/pixelgate/__init__.py` 的 `__version__`、git tag、README 第 3 节安装命令里的 tag |
| 模块职责或设计原则 | `docs/architecture.md` |

## 测试约定

- 测试全部离线，不访问真实网关；网络行为用 `tests/conftest.py` 里的回环假网关、代理陷阱与诱饵网关构造。
- 测试用的 client key 由代码构造成一眼可见的占位值（如 `"ab" * 32`），不写真实 key；要可执行的 helper 时由 fixture 在 `tmp_path` 里写一个替身。路径用 `tmp_path` 或 `/tmp` 下的路径，不写本机家目录路径。
- 一个测试函数验证一个行为；新增错误类别时，同时补一条走到该类别的测试。
