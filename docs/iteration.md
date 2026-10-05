# pixelgate 迭代状态

pixelgate 当前状态的快照与展望。

## 当前状态

- 运行环境：uv 项目，Python 3.14；开发环境 `uv sync --locked --group dev --python 3.14`；日常运行按 tag 安装为 uv tool，入口 `pixelgate --config <配置路径>`；运行时需要本机回环地址上已登录订阅的 CLIProxyAPI 网关，以及一个输出 client key 的 helper（README 第 2 节）。
- 工具：`generate_image` 与 `edit_image` 可用，参数与返回形态见 README 第 6 节。
- 型号：白名单为 `gpt-image-2.5-sunburst` 与 `gpt-image-2.5-flare`，经配置的 `model_routes` 映射到网关请求名。
- 验证：测试全部离线；lint、格式与类型检查都是零诊断，健康检查命令见 AGENTS.md「开发与验证」节。

## 已知问题

| 问题 | 影响 | 优先级 |
| --- | --- | --- |
| 注释与 docstring 引用仓库里没有的文档：`tests/test_schemas.py` 三处写「计划 §4」，`client.py` 与 `schemas.py` 各一处写「按计划丢弃」 | 读者找不到所指的规定；其中 `schemas.py` 里 `UsageInfo` 的 docstring 随输出 schema 发给调用方。改成直接写出这段代码遵守的约束 | 中 |
| 写给维护者的话进了对外的 schema：`UsageInfo` 的 docstring 写「保留 pydantic 默认的忽略行为」，`ErrorCategory` 的 docstring 写「新增类别须同步 README 错误类别表与测试」 | 这两句随输出 schema 发给每个调用方，对调用方没有用；现有测试只检查顶层的 description，查不到这一层。把维护说明移到注释里，并让测试覆盖嵌套模型的 description | 中 |
| 根指引 `AGENTS.md` 的第三行要求开工前先读项目规则目录下的全部文件，仓库里没有这个目录 | 照着做的读者会去找一个不存在的目录。删掉这一句；`.publish-allow` 第一行的放行要保留并改写理由，因为历史提交里含这句话，删掉放行后对全部历史的发布扫描会命中 | 低 |
| 几处文字与现状不符：`schemas.py` 三处用「首版」，而包已发过两个版本；`server.py` 的模块说明写「单次调用最长 600 秒」，README 与 `docs/architecture.md` 写的是可能超过 600 秒；`tests/test_client.py` 一处注释描述的是某台开发机的代理环境 | 文字不准，不影响功能。随下一次发版一并改 | 低 |

## 路线图

- 取消通道：宿主取消调用时中止上游请求，并以 `cancelled` 写出终态回执。现在取消只切断宿主一侧，服务照常跑完。
- 恢复扫描：启动时找出停在 `started` 的回执，按上游请求 ID 或产物实况判定终态。现在停在 `started` 的回执由读取者解释为「上游结果未知」。
