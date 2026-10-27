# 往期周报素材检索实施记录

## 已确认需求

- 新建任务增加默认关闭的“往期周报素材检索”；审核页第三标签为往期周报。
- 每条新闻查同作同版本、早于本期期号的最近两期，列出两期中匹配新闻的全部图片和视频，不为补视频翻第三期。no_videos覆盖历史模块。
- 本地历史任务和Nextcloud目录先建元数据索引；勾选后显示任务级“最近一年／全部周报”，默认365天查询，全部范围不设日期下界；日期存疑保留标记，手动更新索引，无批量原件下载。
- 历史候选独立于普通候选，未经采用不得被export_pending导出；采用后复用校验、去重、审核及导出。
- 凭据进现有CredentialStore；索引失败不影响原流程；离线仅使用本地和缓存。
- input下22份DOCX、当前解析500条新闻作完整本地回归；CI用冻结小夹具。仅定点联网验收，不重采集500条。

## 文件所有权与最小接口

- Luna A：archive/models.py、index.py、providers.py以及tests/archive/test_index.py、test_providers.py。ArchiveAsset数据类字段：asset_id、source_id、kind(image/video)、issue_number、path、name、game_names(tuple)、issue_date(ISO或None)、news_title、revision、size、source_kind(local/nextcloud)、local_path、original_url、original_source_url、preview_url、version；to_dict/from_dict。ArchiveIndex(path).replace_source(source_id, assets)、search(game_names, current_issue=..., no_videos=False, days=365, now=None)、list_assets(source_id=None)、last_updated、close。normalize_issue_number兼容226_副本。NextcloudArchiveProvider(server_url, username, password, root_path, transport=None, cache_dir=None).scan(days=365, now=None, cancelled=None)、fetch(asset, target, max_bytes=..., cancelled=None)、preview(asset,target,max_bytes=...,cancelled=None)、test_connection()。LocalArchiveProvider(roots,source_id='local')同接口。
- Luna B：desktop/new_task_page.py、review_page.py、settings_page.py、archive_panel.py、settings/model.py、credentials.py、settings/__init__.py、pipeline/contracts.py及对应新增UI/settings测试。AppSettings.archive:ArchiveSettings(server_url='', username='', root_path='', local_roots=[], range_days=365)。TaskRequest.use_archive_materials=False。CredentialStore支持archive_app_password。ArchivePanel接口set_assets(news_id, assets, message='')、set_status(message)、asset_activated(object)、adopt_requested(object)、preview_requested(object)、refresh_requested()；可有自定义signals，但先通知root。只UI，无网络或session合并。
- Luna C：archive/materialize.py、tests/archive/test_materialize.py、tests/archive/test_input_corpus.py；ArchiveMaterializer(cache_root, provider_lookup, config=None, media_probe=None).get_original(asset,cancelled=None)、get_preview(asset,cancelled=None)、prepare(asset,news_id=...,task_root=...,cancelled=None)返回ImageCandidate或VideoCandidate。prepare写task_root/retries/<news_id>/archive_<hash>/image_index.json和video_index.json，并在candidate.archive_provenance保留attempt_id。使用ImageDownloader.import_local、OutputManager真实转换、现有video probe。原件和预览独立缓存，缓存校验，独立取消及大小限制。A模型未就绪时先按约定写失败测试，不改A的文件。
- Root：domain.py中的可选archive_provenance与历史候选ID隔离、review/session.py接入、desktop/archive_controller.py、desktop/controller.py、pipeline/checkpoint.py、runner.py必要集成、文档、集成及最终验收。

所有worker在E:/project/galgame news工作；命令必须显式Set-Location并设置ErrorActionPreference=Stop。其他人同时编辑，不回滚他人修改，不提交推送。

## 执行记录

- 原分支main；已有用户output删除、debug.log和deliverables/docs未跟踪内容，保留。
- 已尝试原生工作区，返回D:/114514/Codex/archive-materials/galgame news，但沙箱拒绝进入；原生归档也因primary checkout拒绝，未删除。Ruling: 在原授权E盘工作目录按文件隔离实施，不提交、不改Git引用；不使用不可访问的D盘副本。
- 基线：tests/ingestion + new_task_page + review/session + settings：49 passed, 1 skipped。
- 开发顺序：接口约定 → 各自失败测试 → 实现 → root集成 → focused tests → full regression → 独立代码审查 → 修复 → 交付。

## 状态

- A：完成。索引/本地JSON与SQLite/Nextcloud元数据与按需传输已接入；补齐中文路径、邮箱canonical ID、真实preview端点、304、自然文件顺序和期号日期回归。
- B：完成。新增抓取开关、第三标签、ArchivePanel、设置和凭据库，来源信息与右侧预览已接入。
- C：完成。原件/缩略图隔离缓存、采用准备、视频probe容器校验、22DOCX/500新闻本地回归。
- Root集成：完成。新增异步ArchiveController、采用批次持久化/重开、来源导出、旧SQLite列兼容、旧TaskRequest位置参数兼容；不自动采用历史素材。

## 复验与交付

- 独立GPT-6-Luna审查的4项中风险均已修：范围变化使快照失效、HTTPS保护、重复采用保留旧决定、不支持的视频格式拒绝而非改后缀。
- 最终全套日志：output/archive-module-validation/pytest-final.log，944 passed, 4 skipped, 3 pytest-qt disconnect warnings，187.72s。
- 全套后补充了新flag放在TaskRequest末尾的旧位置参数兼容修复；其相关domain/archive UI/controller组26 passed。
- 新增模块/脚本/测试Ruff与git diff --check通过。宽范围旧文件原有风格告警未作无关清理。
- 只读UI证据：output/archive-module-validation/ui/01-抓取选项.png、02-往期周报标签.png、03-索引设置.png。Luna视觉审查确认关键控件可见；为离线合成示例，不代表真实网盘认证成功。
- 实际网盘认证未验收：先前临时凭据图片已不存在，新模块未配置有效凭据。没有从历史日志恢复或硬编码口令。用户在设置中配置后需定点验证。
- Ruling: 保留仅当前新闻最多两张本地卡片缩略图的只读加载；不涉及远端下载或全量原件复制。
- Ruling: 取消在当前同步请求结束后生效，30s请求超时上限沿用，未为此扩展整个网络架构。
- 用户原有素材删除/未跟踪deliverables/debug.log保持原状；未提交、未推送、未发布或重打包exe。

## 2026-10-27 两期与任务范围更新

- 检索改为当前期号之前最近两个匹配期次，返回两期的全部匹配媒体；去重以期次隔离，无视频模式不继续翻第三期。
- 新建任务勾选后显示“最近一年／全部周报”，默认最近一年；TaskRequest.archive_range 位于字段末尾，任务开始、SQLite持久化和恢复已接通。旧任务缺字段按year处理。
- 全部范围传入days=None，不设置日期下界；历史查询快照版本为3，并记录任务范围，旧快照或范围变化均重查。索引更新仍只处理元数据，选择全部不会触发全量原图下载。
- 本次定向合并验证：116 passed；结果见output/archive-two-issues-range-validation/pytest-results.xml。覆盖索引、真实DOCX解析回归、UI、任务保存恢复、缓存切换、checkpoint身份与既有材料化集成；未重新运行全套。
- UI三种状态由GPT-6-Luna检查，截图在output/archive-two-issues-range-validation/ui/。源码已更新，本次未重打包便携EXE。

## 2026-10-27 专用账号接入验收

- 新账号的可用共享挂载为`/图片组文件`，与旧账号主目录不同；登录、目录读取、默认transport和实际原图GET均已验证。两个目标图片已全部材料化并保留来源及哈希，未扩大为全盘原件下载。
- 实际桌面用户的SettingsStore与Windows Credential Manager已配置；保存后从CredentialStore重新读取并连接成功。密码未写入源码、普通设置或验收记录；测试进程本身不另存密码。沙箱凭据库与桌面用户凭据库隔离，最终配置验证在实际桌面用户上下文执行。
- Provider补充图片组栏目式期次、点分隔日期和混合共享根的期次分支过滤；不修改全局期号规范化规则。抓取、索引、集成及打包定向测试共68 passed，见output/archive-account-check-20261027/pytest-results.xml。
- 打包规格排除仅供可选本地CLIP分析使用的open_clip/torch栈，避免Anaconda环境将这些可选依赖纳入基础桌面程序；实际构建与启动结果以同目录build报告为准。
- 验收证据、系统配置验证和媒体位于output/archive-account-check-20261027；未自动建立全档案索引，首次使用需在设置中手动更新元数据索引。
