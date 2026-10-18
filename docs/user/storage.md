# 任务存储与备份

每次新建任务都会在输出根目录下创建独立的任务文件夹，例如
`output/259-20261012T120000-a1b2c3d4/`。运行命令中的 `--output` 指定这个
输出根目录，不再直接作为该期的任务目录。省略 `--output` 时使用程序的
默认输出根目录。

v1.1.0 新任务以任务目录中的 `task_state.sqlite3` 为权威存储，保存检查点、下载候选
日志、补抓批次和审核决定。`raw/` 下的 JSON 文件仍作为可读的发布快照，
`final/` 保留最终图片交付。兼容用的 `Application.run` 包装接口仍沿用旧的
输出行为；正式命令行 `run` 使用独立任务目录和 SQLite 存储。

## 迁移旧任务

迁移不会自动启动。可在历史任务页选择 **迁移任务存储**，也可执行：

```text
python -m galgame_news migrate-storage PATH_TO_TASK
```

程序会先校验旧检查点和关键索引，再将记录写入 SQLite。原有 JSON 和最终图片
会保留。关键文件损坏时迁移会停止，并报告诊断信息；任务不会被标记为
SQLite 权威存储。

从历史任务导入过的结果，迁移时也会保留工具箱私有目录中的人工决定和补抓记录。
有效补抓资产会复制到源任务的哈希资产目录，原私有文件保留；坏批次单独报告。

## 检查与备份

校验任务数据库及其引用的资产：

```text
python -m galgame_news check-storage PATH_TO_TASK
```

将任务数据库和任务文件备份到另一个目录：

```text
python -m galgame_news backup-storage PATH_TO_TASK PATH_TO_BACKUP
```

备份目录必须为空，并位于任务目录及其父子目录层级之外。迁移或备份前后都可以运行
`check-storage` 检查数据库状态和图片文件。

## 移动任务

跨磁盘或设备移动任务时，复制完整任务文件夹，包括 `task_state.sqlite3`、
`assets/`、`raw/`、`retries/` 和 `final/`。任务数据库保存相对于任务根目录的
资产路径；完整目录搬迁后可继续解析这些路径。

## 开发验证

使用合成数据运行本地基准：

```text
python scripts/benchmark_sqlite_storage.py
```

迁移审计会先将旧任务复制到当前工作区内的临时目录，再对副本执行迁移：

```text
python scripts/benchmark_sqlite_storage.py --audit-task PATH_TO_LEGACY_TASK
```

审计会比较图片、视频、失败记录、审核决定和来源记录，并核对副本及源目录中
所有 JSON 和 `final/` 文件的哈希。报告仅记录环境、耗时、内存峰值和汇总数量，
不会写入个人目录或候选内容。

内存报告分别列出 Python 分配峰值与每 5 毫秒采样的 Windows 进程工作集。
合成样本的查询包含读取、栏目过滤与最多 100 条返回，不代表完整审核界面的耗时。

v1 任务可继续按原有 JSON 索引打开和审核；如需使用 SQLite 存储，可按上文步骤迁移。迁移会保留旧文件，并将资产整理到任务内的哈希路径。
