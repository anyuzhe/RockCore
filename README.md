# RockCore

RockCore 是面向本地项目的多 AI 智能工程工作台，由浙江岩创科技有限公司维护。

## Agent 能力层

RockCore 将运行时概念分为：Agent 决定谁来处理，Skill 提供任务 SOP，
Tool/MCP 提供本地或外部操作，Policy 负责权限边界。项目设置中的
“Skills”和“MCP”页用于配置这两层能力。

内置 Skills 位于 `skills/builtin/<skill-name>/SKILL.md`。运行时先读取
`name` 和 `description` 建立轻量目录，仅把当前任务选中的正文加入模型
上下文。项目可以在 `.ai/skills/` 放置同结构 Skill；同名项目 Skill 会
覆盖内置版本。项目 Skill 也需要在项目设置中保存本机批准指纹，内容
变化会自动撤销旧批准，避免仓库通过提示注入自行取得信任。任务计划和
运行检查点会保存实际选择的 Skill 名称。

MCP 使用项目 `.ai/agents.json` 中的 stdio 服务配置。外部工具会以
`mcp__服务名__工具名` 进入 ToolBroker，与本地工具共用任务权限检查；
只读任务看不到写入型 MCP 工具，单个 MCP 服务不可用也不会影响本地
文件、Git、Shell 和测试能力。密钥应通过环境变量引用，不要写入项目：

```json
{
  "mcp": {
    "enabled": true,
    "servers": [
      {
        "name": "example",
        "command": "npx",
        "args": ["-y", "your-mcp-server"],
        "env": {"ACCESS_TOKEN": "${EXAMPLE_ACCESS_TOKEN}"},
        "read_only": true,
        "allow_tools": ["*"]
      }
    ]
  }
}
```

MCP 进程不经过 shell，支持 UTF-8 JSON-RPC、Windows `.cmd/.bat` 启动器
和 GUI 事件循环。项目默认不启用任何外部 MCP 服务，必须在项目设置中
显式启用并保存；RockCore 会把配置指纹批准记录保存在用户数据目录，
因此仓库中的 `.ai/agents.json` 不能自行获得启动本地进程的权限。

## Windows 发布

GitHub Actions 工作流位于 `.github/workflows/windows-release.yml`。

- 推送到 `main`：生成 Windows x64 便携版 ZIP 和安装包，并上传到 Actions Artifacts。
- 推送版本标签（例如 `v1.0.0`）：额外自动创建 GitHub Release，并附带安装包、便携版和 SHA256 校验文件。
- 发布标签的版本必须与仓库 `VERSION` 完全一致，否则 Actions 会在打包前停止，避免客户端找不到对应安装包。

本地 Windows 构建（需要 Python 3.11+ 和 Inno Setup）：

```powershell
python -m pip install -r requirements-build.txt
./scripts/build_windows.ps1
```

输出目录为 `release/`：

```text
RockCore-Setup-1.0.3-x64.exe
RockCore-1.0.3-Windows-x64-portable.zip
SHA256SUMS.txt
```

正式版的配置、数据库和日志会保存到当前用户的应用数据目录，不会写入 `Program Files`。首次启动默认工作区为用户目录下的 `RockCore Projects`，也可以在设置中修改。

Windows 安装版启动后会在后台检查 GitHub 的最新稳定版 Release；也可以从
“帮助 → 检查更新”手动检查。发现新版本后由用户确认下载，RockCore 会同时
下载 `SHA256SUMS.txt` 并验证安装包 SHA-256，只有校验通过才会启动 Inno
Setup 覆盖升级。升级不会删除用户数据、项目或设置；启动检查可在“设置 →
通用”中关闭。

Python 项目的语法检查、`unittest` 和 `pytest` 验收使用 RockCore 安装包内置的
Python 运行时与 pytest，不要求用户另外安装 Python，也不依赖系统 `PATH`。
项目自身额外引入的第三方依赖仍需由项目提供；缺失时会明确报告为项目依赖问题，
不会误报成“找不到 Python”。

## 品牌资源

当前内置标识位于 `assets/branding/`，应用侧栏、窗口标题、关于页和 Windows 程序图标统一使用“RockCore · 岩创科技”。如果有公司的正式 Logo 原文件，可直接替换 `rockinnov_logo.png` 和 `rockcore.ico`；`rockinnov_logo.svg` 是当前内置标识的矢量源文件，`scripts/make_brand_assets.py` 用于重新生成默认衍生资源。
