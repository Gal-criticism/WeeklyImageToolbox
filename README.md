<p align="center">
  <img src="src/galgame_news/desktop/assets/character.png" width="112" height="112" alt="周报图片采集工具箱角色图">
</p>

<h1 align="center">周报图片采集工具箱</h1>

<p align="center">从一份 DOCX 开始，采集新闻配图、检查来源，再由你决定最终选图。</p>

<p align="center">
  <a href="https://github.com/Gal-criticism/WeeklyImageToolbox/releases/download/v1.1.0/WeeklyImageToolbox-v1.1.0-windows-x64.zip"><strong>下载 Windows x64 便携版 v1.1.0</strong></a>
  · <a href="https://github.com/Gal-criticism/WeeklyImageToolbox/releases/tag/v1.1.0">v1.1.0 发布说明</a>
</p>

从上方链接下载 v1.1.0 便携版；解压后运行 `WeeklyImageToolbox.exe`，无需另装 Python。

<p align="center">
  <img src="https://img.shields.io/badge/Windows-x64%20Portable-0078D6" alt="Windows x64 便携版">
  <img src="https://img.shields.io/badge/Python-Not%20required-3776AB?logo=python&logoColor=white" alt="便携版无需 Python">
</p>

<p align="center">
  <a href="#功能一览">功能介绍</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="docs/user/manual.md">使用手册</a> ·
  <a href="docs/user/faq.md">常见问题</a> ·
  <a href="https://github.com/Gal-criticism/WeeklyImageToolbox/issues">问题反馈</a>
</p>

适合需要整理新作、汉化和周报配图的编辑及测试用户。工具箱负责收集和整理候选，你可以逐条新闻检查已选图片与“未候选”备选，并导出确认后的结果。

> 以下截图来自离线演示数据，不代表真实抓取成果或筛选准确率。

## 界面预览

### 新建任务：选择栏目，明确本次抓取范围

![新建任务页面：DOCX、期号、输出目录与独立栏目选择](docs/assets/screenshots/new-task.png)

### 图片审核：查看备选、核对来源、决定是否入选

![图片审核页面：新闻列表、缩略图与大图详情](docs/assets/screenshots/review.png)

<details>
<summary>展开查看抓取进度、历史任务和设置</summary>

![抓取进度、历史任务和设置总览](docs/assets/screenshots/overview.png)

完整截图：[抓取进度](docs/assets/screenshots/progress.png) · [历史任务](docs/assets/screenshots/history.png) · [设置](docs/assets/screenshots/settings.png)

</details>

## 功能一览

| 功能 | 可以做什么 |
| --- | --- |
| 新闻栏目选择 | 新作、汉化、周边独立勾选，保留原文档顺序与 `xN` / `hN` / `zN` 编号 |
| 官网图片采集 | 发现相关作品页、图库与商品详情；动态图库可启用浏览器补充采集 |
| 周边商品采集 | 查找商品详情与补搜图片，提供商品级关联证据；Amazon 商品页过滤推荐商品 |
| 可选 X API | 配置自己的 SocialData 凭据后，按帖子提取媒体；每次任务默认关闭 |
| 汉化配图 | 识别 VNDB 发行记录对应作品及 Steam 本作品截图，优先获取高清原图 |
| 图片筛选 | 结合作品关联、图片级证据与原始质量筛选；确认同画面后在新闻内去重 |
| 人工审核 | 已选、未候选／待复核、已排除和视频分页；支持多选、快捷键、缩放及来源入口 |
| 图片交付 | PNG/JPG 审阅文件与新闻目录编号；合适的未候选备选随导出保留 |
| 任务恢复 | 暂停、继续、停止，历史任务重新审核及可恢复媒体续跑 |
| 任务存储 | SQLite 保存任务状态；旧任务可迁移，哈希资产复用并兼容 v1 任务 |
| 媒体恢复 | 恢复已有 X 与 YouTube 媒体记录，不自动启用 SocialData 查询 |
| 退出确认 | 有任务运行时关闭窗口先确认，确认后立即退出；空闲时直接关闭 |
| 中文桌面界面 | 浅色、深色和跟随系统；中文进度摘要与可展开的技术详情 |

X API 照片保留最高来源优先级，仍遵循新闻内去重和数量限制。SocialData 默认关闭，使用时需自行配置密钥并逐任务启用；发行包不含私测凭据。其他来源的高清图片也需要作品关联和图片证据，不能仅凭来自 VNDB、Steam 或官网就自动入选。

### 一次任务的流程

```mermaid
flowchart LR
    A[选择 DOCX] --> B[选择栏目与抓取选项]
    B --> C[采集与下载]
    C --> D[按新闻审核图片]
    D --> E[导出已选与未候选备选]
```

## 快速开始

v1.1.0 Windows x64 便携版可从[最新版发布页](https://github.com/Gal-criticism/WeeklyImageToolbox/releases/latest)下载；查看[v1.1.0 发布说明](https://github.com/Gal-criticism/WeeklyImageToolbox/releases/tag/v1.1.0)。

首次使用建议保持“跳过视频”选中，并让 SocialData 保持关闭。SocialData 需要自备密钥、逐任务启用，发行包不含任何私测凭据。需要脚本处理的用户可参考[手册中的 CLI 入口](docs/user/manual.md#命令行)。

## 图片与任务输出

任务保留完整数据，以便后续审核、追溯来源和恢复。v1.1.0 新任务使用以下存储，`final/images` 的成品命名保持不变：

```text
output/<任务目录>/
├─ task_state.sqlite3     # 候选、进度、审核决定、批次及媒体缓存
├─ assets/
│  ├─ originals/          # 按哈希保存原件
│  └─ review/             # 按转换版本及内容哈希保存审阅件
├─ raw/                  # 发布时生成的兼容索引快照
└─ final/images/         # xN、hN、zN 已选及未候选备选
```

v1 旧任务保持可打开和可审核；需要使用 SQLite 时，可在历史任务页选择“迁移任务存储”。迁移不会删除旧文件。迁移、检查与整任务备份见[任务存储说明](docs/user/storage.md)。

尚未迁移的 v1 旧任务仍采用下列结构（其他文件按任务情况生成）：

```text
output/<任务目录>/
├─ raw/                    # 采集索引、原件与审阅文件
├─ media_state/            # 媒体下载状态
├─ x_media_cache/          # 可用时保留的 X 媒体查询缓存
├─ checkpoint.json         # 可恢复任务进度
├─ request_options.json    # 本次任务选项
├─ review_state.json       # 人工审核决定
└─ final/
   └─ images/
      ├─ x1/
      │  ├─ x1.01.png      # 已选图片（扩展名按实际转换结果）
      │  └─ 未候选/
      │     └─ x1.u01.jpg  # 保留供人工选择的备选
      ├─ h1/
      └─ z1/
```

交付图片主要查看 `final/images`。继续审核或恢复任务时，保留整个任务目录，不要只留下最终图片。

“未候选”不再展示已确认的同画面重复版本，且图片原始长边至少 **800**、短边至少 **600** 像素。尺寸未知的失败记录保留供诊断；被过滤的有效图片仍可保留在内部资产与索引中，内部资产不等同于备选交付。详见[审核与输出说明](docs/user/manual.md)。

## 使用边界

- 自动选图需要人工确认，尤其是作品关系、动态页面、商品素材及新闻对应关系；本项目不承诺整体筛选准确率。
- SocialData 是可选的付费外部服务，需自行配置凭据。图片下载与媒体恢复尽量复用记录，费用以服务商实际账单为准。
- 公开源码、截图与文档不包含你的 API 密钥；不要在 Issue、截图或日志反馈中粘贴凭据。
- 视频是现有辅助功能，使用时需检查相关下载工具；图片任务可以直接跳过视频。
- 本仓库尚未声明项目整体开源许可证。第三方组件的许可证独立适用，见[第三方声明](packaging/THIRD_PARTY_NOTICES.md)及 [VNDB 适配来源](src/galgame_news/localization/PROVENANCE.md)。

## 文档与反馈

- [快速开始](docs/user/quick-start.md)：安装、启动、浏览器与 CLI。
- [使用手册](docs/user/manual.md)：新建任务、审核、导出与历史恢复。
- [常见问题](docs/user/faq.md)：X 缺图、未候选、导出失败与目录问题。
- [构建说明](packaging/README.md)：面向维护者的 Windows 打包流程。
- [提交问题](https://github.com/Gal-criticism/WeeklyImageToolbox/issues)：请附操作步骤、版本、脱敏截图和必要诊断。
