# 周报图片采集工具箱：Windows x64 便携版构建

正式发布包由 `packaging/release.py` 从全新临时构建目录生成，完整包含 Windows x64 PyInstaller onedir 运行时与浏览器组件。发布压缩包为
`WeeklyImageToolbox-v1.0.0-windows-x64.zip`，解压后的可执行文件为
`WeeklyImageToolbox.exe`。用户无需安装 Python。发布页为
https://github.com/kasumi-ppp/galgame-news/releases/tag/v1.0.0，最新版本入口为
https://github.com/kasumi-ppp/galgame-news/releases/latest。

从干净的源码检出安装固定版本构建依赖，并运行正式发布脚本：

```powershell
python -m pip install -e ".[desktop,dev,build]"
python packaging/release.py
```

正式脚本在临时目录新建 PyInstaller dist/work 及发布组装目录，不复用或覆盖旧 bundle；验证完整后输出 ZIP。打包规格仅包含 `config/default.toml`，以及可选的仓库 `assets/` 目录。脚本会获取固定版本 FFmpeg 和 Playwright Chromium，并将运行时一并放入 ZIP。它不会收集用户已有任务输出、仓库内测试密钥或私测凭据。构建中用到的 FFmpeg 不会提交到仓库。

下面的 `packaging/build.py` 是开发用增量打包流程，不生成正式发布 ZIP。它在临时目录构建后更新 `dist/WeeklyImageToolbox/`，保留未知顶层文件和 `dist/.../output` 用户数据，并整体替换 `_internal` 运行时目录；若复制失败，会回滚本次文件变更。目标 EXE 正在使用时会在复制前失败。开发构建仅在维护者本地提供 `bin/ffmpeg.exe` 和 `bin/ffprobe.exe` 时复制这些文件，不会自行下载 FFmpeg，也不会把 FFmpeg 二进制提交到仓库。

正式发布脚本从全新临时目录构建、组装白名单文件并生成 ZIP，不执行上述 overlay。发布包不包含 SocialData 测试或私有凭据；SocialData 默认关闭，由用户自备密钥。`00_启动工具箱.lnk` 是开发构建生成的快捷方式，不属于发布 ZIP。

运行中的任务会保留完整任务目录，包括 `raw/`、索引、检查点、缓存、审核状态、日志和 `final/` 图片交付。历史页可重新打开审核或恢复可续跑任务。已压缩的旧任务仍为仅图片数据；不会从 `final/images` 重建缺失的内部数据。

规格文件将 PySide6 作为唯一 Qt 绑定，并排除 PyQt5、PyQt6、PySide2、tkinter、测试/Notebook/文档模块及 NumPy、Matplotlib 等未使用的科学计算栈。生产入口不会导入这些包；项目运行时依赖仍会打包。

桌面界面与 keyring 支持是可选依赖。基础安装仍需支持无界面 CLI，因此核心模块启动时不得导入 PySide6 或 keyring。

发布策略：从干净检出生成可复现包；每个版本固定 `pyproject.toml` 和 `requirements-build.txt` 中的依赖，并随发布说明审慎更新。程序没有自动更新器，通过正式发布流程分发新版。

## Windows smoke check

构建完成后，可在 PowerShell 中运行开发构建结果：

```powershell
& .\dist\WeeklyImageToolbox\WeeklyImageToolbox.exe
```

检查五个页面能否打开，创建包含三条新闻的离线测试任务，安全停止并续跑，进入审核页并导出审核结果。确认完成任务保留 `raw/`、检查点、缓存、审核状态及 `final/` 目录。若开发构建提供了 `bin/ffmpeg.exe` 和 `bin/ffprobe.exe`，还需检查设置页显示的工具路径。

Before publishing the portable ZIP, manually verify on a clean Windows
machine with no Python installation:

- the application starts from the extracted ZIP;
- image and video collection complete with the bundled tools;
- safe stop, restart, history, and review decisions survive an app restart;
- a single-news retry does not overwrite `raw/`;
- exported files retain the `x1.01.jpg`, `h1.01.jpg`, and `z1.01.jpg` naming;
- API credentials do not appear in JSON, logs, output, or crash reports;
- third-party notices and the pinned component versions ship with the bundle.

This repository checklist is documented but has not yet been executed on a
separate clean Windows machine.
