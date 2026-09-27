# pixelgate 迭代状态

pixelgate 当前状态的快照与展望。

## 当前状态

- 运行环境：uv 项目，Python 3.14；开发环境 `uv sync --locked --group dev --python 3.14`；日常运行按 tag 安装为 uv tool，入口 `pixelgate --config <配置路径>`；运行时需要本机回环地址上已登录订阅的 CLIProxyAPI 网关，以及一个输出 client key 的 helper（README 第 2 节）。
- 工具：`generate_image` 与 `edit_image` 可用，参数与返回形态见 README 第 6 节。
- 型号：白名单为 `gpt-image-2.5-sunburst` 与 `gpt-image-2.5-flare`，经配置的 `model_routes` 映射到网关请求名。
- 验证：测试全部离线；类型检查以 `.basedpyright/baseline.json` 为基线，健康检查命令见 AGENTS.md「开发与验证」节。

## 已知问题

无。

## 路线图

- 取消通道：宿主取消调用时中止上游请求，并以 `cancelled` 写出终态回执。现在取消只切断宿主一侧，服务照常跑完。
- 恢复扫描：启动时找出停在 `started` 的回执，按上游请求 ID 或产物实况判定终态。现在停在 `started` 的回执由读取者解释为「上游结果未知」。
