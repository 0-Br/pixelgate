# pixelgate 决策日志

pixelgate 项目决策的 append-only 日志。

### 2026-09-27: 以 pixelgate 为名发布为独立仓库

**背景**：这个包原本以 gpt-image-tools 为名，放在一个私有仓库的子目录里开发，只在一台机器上使用；现在作为独立仓库公开。OpenAI 的品牌规定不允许在第三方产品名里使用「GPT」。

**选择**：仓库、Python 包与模块、命令、MCP 服务名统一为 `pixelgate`；上游的产品名 GPT Image 与型号名 `gpt-image-*` 是领域事实，照写不改。仓库从改名后的快照起步，不带原子目录的提交历史。安装方式改为按 git tag 安装 uv tool，不再以可编辑模式安装源码目录。ruff 配置改为写在本仓库 `pyproject.toml` 里、不继承外部文件，ruff 版本随 dev 依赖组由 `uv.lock` 钉住；类型检查从 pyright 换成 basedpyright 的 standard 档，并以基线文件区分存量诊断与新增诊断。

**理由**：独立仓库的使用者没有原仓库的上级配置文件，继承外部 ruff 配置会让规则在别人的检出里静默失效；按 tag 安装让日常运行的版本与源码检出的改动解耦。basedpyright 的基线机制让类型检查可以覆盖整个项目，而不被存量诊断拦住每一次提交。

**影响**：命令从 `gpt-image-mcp` 改为 `pixelgate`，MCP 工具名随之变为 `mcp__pixelgate__*`；配置目录约定改为 `~/.config/pixelgate/`；dev 依赖组为 pytest、ruff 与 basedpyright；新增 `.python-version`；测试新增服务名断言。
