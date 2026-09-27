# pixelgate 决策日志

pixelgate 项目决策的 append-only 日志。

### 2026-09-27: 以 pixelgate 为名发布为独立仓库

**背景**：这个包原本以 gpt-image-tools 为名，放在一个私有仓库的子目录里开发，只在一台机器上使用；现在作为独立仓库公开。OpenAI 的品牌规定不允许在第三方产品名里使用「GPT」。

**选择**：仓库、Python 包与模块、命令、MCP 服务名统一为 `pixelgate`；上游的产品名 GPT Image 与型号名 `gpt-image-*` 是领域事实，照写不改。仓库从改名后的快照起步，不带原子目录的提交历史。安装方式改为按 git tag 安装 uv tool，不再以可编辑模式安装源码目录。ruff 配置改为写在本仓库 `pyproject.toml` 里、不继承外部文件，ruff 版本随 dev 依赖组由 `uv.lock` 钉住；类型检查从 pyright 换成 basedpyright 的 standard 档，并以基线文件区分存量诊断与新增诊断。

**理由**：独立仓库的使用者没有原仓库的上级配置文件，继承外部 ruff 配置会让规则在别人的检出里静默失效；按 tag 安装让日常运行的版本与源码检出的改动解耦。basedpyright 的基线机制让类型检查可以覆盖整个项目，而不被存量诊断拦住每一次提交。

**影响**：命令从 `gpt-image-mcp` 改为 `pixelgate`，MCP 工具名随之变为 `mcp__pixelgate__*`；配置目录约定改为 `~/.config/pixelgate/`；dev 依赖组为 pytest、ruff 与 basedpyright；新增 `.python-version`；测试新增服务名断言。

### 2026-09-27: 环境变量的防护口径以代码行为为准

**背景**：README 与架构文档把客户端对 `OPENAI_*` 与代理环境变量的处理写成「从进程环境移除」，而代码只在构造客户端期间临时摘掉这些变量、构造完成后原样放回。

**选择**：文档按代码行为写：出站防护的主力是 HTTP 客户端的 `trust_env=False` 加自带的目标守卫传输层，构造期间的临时摘除是第二道，防的是 SDK 在构造时读环境变量；进程环境本身不被清理。`client.py` 里对应的注释改为同一口径。

**影响**：README 第 9 节、`docs/architecture.md` 的核心设计原则一条与 `build_client` 的注释改写；代码行为不变，`v0.1.0` 的安装内容不受影响。
