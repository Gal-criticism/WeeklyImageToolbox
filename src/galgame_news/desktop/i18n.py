"""Presentation-only Chinese labels; serialized state codes remain unchanged."""
from __future__ import annotations


def code(value) -> str:
    return str(getattr(value, "value", value) or "unknown")


IMAGE_TYPES = {
    "game_cg": "游戏 CG", "background_art": "游戏背景美术", "decorative": "装饰素材",
    "gameplay_screenshot": "游戏截图", "key_visual": "主视觉",
    "character_art": "角色立绘", "cover": "封面", "announcement_art": "宣传插画",
    "goods": "商品图", "logo": "标志", "banner": "横幅", "ui": "界面素材",
    "photo": "照片", "unknown": "类型待确认", "icon": "图标", "favicon": "网站图标",
}
STATUS = {
    "idle": "等待开始", "running": "正在抓取", "paused": "已暂停", "completed": "已完成",
    "cancelled": "已停止", "failed": "执行失败", "selected": "已选", "unselected": "待复核",
    "invalid": "无效文件", "accepted": "已选", "pending": "待复核", "rejected": "已排除",
    "discovered": "已发现", "downloading": "正在下载", "downloaded": "已下载", "skipped": "已跳过",
}
REASONS = {
    "x_source": "来自 X 帖子", "age_gate": "页面需要年龄确认", "dynamic_page": "动态页面需要复核",
    "unknown_publish_time": "发布时间未知", "uncertain_match": "与本条新闻的关联待确认",
    "close_scores": "候选评分相近", "network_restricted": "网络访问受限",
    "fallback_old_material": "使用历史素材", "historical_duplicate": "与历史素材重复",
    "adult_or_unknown": "内容需人工复核", "image_type_review": "图片类型待确认",
    "image_quality_review": "清晰度需复核", "unknown": "判断依据待确认",
    "socialdata_x_photo_priority": "X 帖子照片优先入选",
    "socialdata_x_photo_source_overrides_entity_classifier": "使用原帖与新闻的直接关联证据",
    "source_not_linked_to_news": "来源与本条新闻未建立关联",
    "animated_source_requires_review": "动图代表帧需复核", "below_minimum_resolution": "原图尺寸未达到入选标准",
    "entity_conflict": "图片指向其他作品", "entity_unverified": "作品主体尚未确认",
    "duplicate_of_better_candidate": "同画面已有细节更好的版本",
    "goods_reference_requires_manual_review": "商品参考图或原型图，需人工确认同款及阶段",
    "goods_non_original_requires_manual_review": "仅取得商品预览图，未确认高清原图，保留待复核",
    "selected_by_rank_and_policy": "符合关联、类型及清晰度要求",
    "not_selected_by_policy_or_allocation": "未进入本条新闻的自动入选名额",
    "type_evidence_insufficient": "图片类型证据不足，保留待复核",
    "automatic_type_selection_disabled": "此类型需要人工选择",
    "background_art_source_unlinked": "背景美术缺少与本条新闻相连的官方来源证据",
    "decorative_asset_not_auto_selected": "装饰素材保留供查看，不自动入选",
    "news_selection_limit": "本条新闻已达到不同画面的入选上限",
    "type_limit_or_minimum_score": "达到类型数量上限或未达到入选评分",
    "review_image_conversion_failed": "审阅图片转换失败，保留原件",
    "download_failed": "图片下载失败", "conversion_failed": "审阅图片转换失败",
    "not_localization_section": "本项不属于汉化新闻分节",
    "localization_work_not_confirmed": "作品绑定尚未确认为当前作品",
    "localization_binding_uncertain": "作品绑定待确认",
    "localization_reference_only": "这是关联参考作品，仅供人工复核",
    "localization_binding_conflict": "作品绑定存在冲突",
    "localization_non_game_image_role": "标志或装饰素材不作为游戏画面",
    "localization_source_unlinked": "汉化图片来源未与本条新闻建立关联",
    "localization_image_not_screenshot": "缺少图片级游戏截图证据",
    "localization_image_not_native": "缺少原生图片证据",
    "localization_not_full_resolution": "当前为缩略图或非完整分辨率",
    "localization_image_not_downloaded_or_decoded": "图片未成功下载或解码",
    "localization_dimensions_missing": "缺少已解码的图片尺寸",
    "localization_expected_dimensions_mismatch": "实际图片尺寸与来源标注不符",
    "localization_below_native_resolution_floor": "图片未达到原生分辨率门槛",
    "localization_manual_review_required": "保留此图供人工复核，不自动入选",
    "localization_review_required": "汉化图片信息不足，需人工复核",
    "localization_full_native_screenshot": "已确认当前作品的原图游戏截图",
    "image_local_event_cg": "图片级证据标明为作品事件 CG",
    "localization_work_confirmed": "已确认属于当前作品",
    "source_linked": "来源已关联本条新闻",
    "decoded_image_valid": "图片已下载并完成解码校验",
}
STAGES = {"parse": "读取文档", "analyze": "分析新闻", "resolve": "查找来源", "collect": "采集媒体",
          "download": "下载媒体", "curate": "筛选图片", "output": "保存结果", "history": "更新历史",
          "news": "处理新闻", "task": "执行任务", "candidate": "处理图片", "video": "处理视频"}
X_MEDIA_COUNTERS = {
    "x_queries": "X 查询数", "x_attachments": "附件数", "x_downloaded": "下载成功",
    "x_pending": "待恢复", "x_failed": "下载失败",
}


def image_type_label(value) -> str:
    return IMAGE_TYPES.get(code(value), "类型待确认")


def status_label(value) -> str:
    return STATUS.get(code(value).casefold(), "状态待确认")


def reason_label(value) -> str:
    text = code(value)
    if text in REASONS:
        return REASONS[text]
    if text.startswith("image_type_rejected:"):
        return "图片类型不适合本条新闻：" + image_type_label(text.partition(":")[2])
    if text.startswith("invalid:"):
        return "文件校验未通过，详见技术详情"
    return "需要人工复核，详见技术详情"


def event_summary(kind: str) -> str:
    exact = {"news_skipped": "已从检查点恢复本条新闻", "checkpoint_saved": "进度已保存",
             "source_found": "已发现媒体来源", "candidate_found": "已发现图片候选",
             "x_media_stats": "X 媒体进度已更新"}
    exact["task_workspace_ready"] = "任务目录已建立"
    exact["output_cleanup_failed"] = "图片已导出，内部文件整理未完成，请查看技术详情"
    if kind in exact:
        return exact[kind]
    stage, _, action = kind.rpartition("_")
    phrase = STAGES.get(stage, "任务处理")
    return phrase + {"started": "开始", "completed": "完成", "finished": "完成",
                     "failed": "失败，请查看技术详情", "cancelled": "已停止", "skipped": "已跳过"}.get(action, "状态已更新")


def error_summary(message: str) -> str:
    """Explain familiar failures without copying arbitrary raw diagnostics."""
    lower = message.casefold()
    for fragments, caption in (
        (("no news items", "no_news_items"), "未识别到新闻，请检查周报的分节与标题格式。"),
        (("configuration is missing", "config_missing"), "未找到配置文件，请检查工具箱的配置目录。"),
        (("input_missing", "no such file", "filenotfound"), "未找到输入文件，请重新选择周报文档。"),
        (("429", "rate limit"), "来源请求过于频繁，请稍后重试。"),
        (("timed out", "timeout"), "网络请求超时，可稍后重试本条新闻。"),
        (("connection", "network"), "网络连接失败，请检查网络或来源网站状态。"),
        (("permission", "access denied"), "没有写入权限，请检查输出目录。"),
    ):
        if any(fragment in lower for fragment in fragments):
            return caption
    return "任务执行失败，请展开技术详情查看原因。"
