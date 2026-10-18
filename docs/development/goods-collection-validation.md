# GOODS collection validation

`scripts/validate_goods_collection.py` replays GOODS curation for the z-section news in a baseline task. It compares the baseline candidate/selected counts with a fresh formal pipeline run and writes a browsable evidence report. The baseline SQLite database is opened with `mode=ro`; the input DOCX is read only. Output must be a new path.

Run from the repository root:

```powershell
E:\Anaconda3\python.exe scripts\validate_goods_collection.py `
  --baseline-task "<plan265 task root>" `
  --input "<original plan265 DOCX>" `
  --output "<new validation root>" `
  --fetch
```

Without `--fetch`, the script makes no network requests. It still runs the replay through a closed-world transport, so sources and images without frozen fixtures are recorded as unavailable. With `--fetch`, it uses the shared safe HTTP client for public GETs only: HTML is limited to 5 MiB, images to 15 MiB, timeout is 10 seconds, and retries are disabled beyond the first attempt. Relevant product-link expansion is limited to depth 3 and 16 pages per news item. The formal pipeline replay then receives only frozen HTML and images. Browser rendering, video processing, LLM analysis, public APIs, and additional X lookups are disabled; X candidates and post-cache records are copied in memory from the baseline.

Use `--reuse-fixtures "<previous validation root>"` to copy previously frozen public resources into a new result directory. Without `--fetch`, all subsequent collection and downloads remain offline. Cached X bytes are served through the frozen image transport and downloaded by the formal pipeline into its own task asset library, rather than referencing files outside the task.

The script checks the DOCX SHA-256 against the baseline checkpoint before replay. It copies baseline X originals into the new root only after checking task-root containment, SHA-256, image decoding, and extension/format consistency. Any missing, unreadable, mismatched, or inaccessible source is written to `failed_sources.json` with a status and reason.

Output files:

- `report.html`: before/after counts by z-news item, GOODS evidence and roles, selected/manual-review status, and relative links to frozen review files.
- `summary.json`: issue and per-news counts, support-site sample status, API count, and the separate EEO sample status.
- `candidates.json`: candidate-level curation results and page-local GOODS evidence.
- `failed_sources.json`: fetch, baseline-asset, and closed-world replay failures.
- `fixture_manifest.json`: frozen page/image URLs, statuses, hashes, and relative file paths.
- `metrics.json`: runner metrics and explicit API-call count.
- `fixtures/`: fetched public HTML and images; `frozen/baseline_assets/`: verified baseline X originals.

The extra EEO sample for **あえて無視するキミとの未来** is not treated as actual news. The approved seed is `https://eeo.today/store/101/title/8161`; the report records whether it could be fetched and parsed separately from the plan 265 news. Getchu, Fantia, and DLsite are reported as unverified until matching product candidates are reached through real public links. The script does not manufacture product URLs.

## 2026-10-18 验收记录

最终结果：`output/265_goods_fix_20261018/report.html`。原任务以只读方式打开。

| 新闻 | 原候选／已选 | 新候选／已选 | 说明 |
| --- | --- | --- | --- |
| Gamers 快闪店 | 43／3 | 3／3 | 保留 3 张缓存 X 图片；网页连接失败留档 |
| FANZA 抽奖 | 129／1 | 1／1 | 保留 X 图片；年龄确认页不生成商品候选 |
| Frontwing 周边 | 1／1 | 1／1 | 保留 X 图片；Amazon 503 留档 |
| 联动咖啡厅 | 1／1 | 1／1 | 保留 X 图片 |
| 氷見山玲手办 | 214／1 | 14／1 | 1 张 X 已选，13 张正文商品参考图待复核 |

独立 eeo 样例经过列表第二页发现商品详情，共 3 张候选，2 张下载并入选，1 张原图下载失败且保留链接。Getchu、Fantia、DLsite 的真实详情页未验证，仅有离线解析覆盖；不据此宣称全站可用。

本次无付费 API、LLM 或视频调用。有效原件哈希、审阅图解码与扩展名，以及最终任务内路径已核对。
全量回归一次运行结果为 840 通过、4 跳过，另有 1 项原有 Windows 进程关闭时限测试在并行验证期间超时；该模块单独复跑 7 项通过。最后定向回归 59 通过、1 跳过。
