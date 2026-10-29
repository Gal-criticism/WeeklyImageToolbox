# 多来源补图实施说明

设计依据：2026-10-28对话确认的修订版计划。范围是规则驱动的图片补采、当前任务内已查链接去重和独立候选审核；模型、视频、OP截帧不在首版范围。

## 结构

- supplement/providers.py：每来源最多两个入口，官网优先；月幕只接受公开/co/picset/数字图集入口，主滑动容器内的图片，不抓头像、推荐内容与缩略图副本。
- supplement/rules.py：作品关系、版本风险和现有图片用途分类；元数据不能确认画面洁净。
- supplement/ledger.py：当前任务独立SQLite登记、URL规范化及页面候选缓存。
- supplement/service.py：正常抓取网络环境释放后进行补采；复用现有下载与持久化组件。
- desktop及review：候选采用、分类说明、任务参数和导出限制；审核状态保存独立采用名单。

任务选项默认use_image_supplement=False、supplement_target_images=5。旧任务关闭新能力；关闭时不改变既有配置哈希。任务存储中未采用候选以supplement_module标记，review_state.supplement_adopted保存显式采用决定。

## 验证边界

output/补图模块试验-20261028/ymgal-live-check.json是公开图集定点测试：只验证页面HTTP、主图集链接范围和数量，不下载图片或进行画面审核。样例本身属于AMBITIOUS MISSION后日谈1+2，不能当成本篇已补齐证据。

news-metadata.json由input中现有DOCX解析而来；rule-samples.json只记录代表新闻的元数据分类试验，不是人工画面标注集，也不提供分类准确率。上线后应通过实际采用/排除反馈继续调整规则。

运行测试：在项目根目录设置PYTHONPATH为src，QT_QPA_PLATFORM为offscreen，然后用E:/Anaconda3/python.exe -B -m pytest。真实网页与网络下载验证应分别报告，不能用冻结夹具代替实时可用性或画面合格性。

2026-10-28最终全量回归：977通过、4跳过，3条pytest-qt信号清理警告，无失败。独立试验版通过程序自检（浏览器启动、FFmpeg、审核导出和恢复），18个改动模块的打包代码与当前源码一致。核对记录见output/补图模块试验-20261028/交付核对.json；全量测试记录见output/supplement-final-tests.xml。
