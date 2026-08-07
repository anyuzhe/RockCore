# RockCore

RockCore 是面向本地项目的多 AI 智能工程工作台，由浙江岩创科技有限公司维护。

## Windows 发布

GitHub Actions 工作流位于 `.github/workflows/windows-release.yml`。

- 推送到 `main`：生成 Windows x64 便携版 ZIP 和安装包，并上传到 Actions Artifacts。
- 推送版本标签（例如 `v1.0.0`）：额外自动创建 GitHub Release，并附带安装包、便携版和 SHA256 校验文件。

本地 Windows 构建（需要 Python 3.11+ 和 Inno Setup）：

```powershell
python -m pip install -r requirements-build.txt
./scripts/build_windows.ps1
```

输出目录为 `release/`：

```text
RockCore-Setup-1.0.0-x64.exe
RockCore-1.0.0-Windows-x64-portable.zip
SHA256SUMS.txt
```

正式版的配置、数据库和日志会保存到当前用户的应用数据目录，不会写入 `Program Files`。首次启动默认工作区为用户目录下的 `RockCore Projects`，也可以在设置中修改。

## 品牌资源

当前内置标识位于 `assets/branding/`，应用侧栏、窗口标题、关于页和 Windows 程序图标统一使用“RockCore · 岩创科技”。如果有公司的正式 Logo 原文件，可直接替换 `rockinnov_logo.png` 和 `rockcore.ico`；`rockinnov_logo.svg` 是当前内置标识的矢量源文件，`scripts/make_brand_assets.py` 用于重新生成默认衍生资源。
