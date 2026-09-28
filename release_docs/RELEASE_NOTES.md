# ESG-XK V1.1.0 Release Notes

发布日期：2026-09-28　·　APP_VERSION 1.1.0　·　SCHEMA_VERSION 2　·　Git Tag `ESG-XK-V1.1.0`
基线：V1.0.0（`ESG-XK-V1.0.0`，交付目录 `ESG-XK-1.0.0-portable` 保持冻结，未作任何修改）

## 新增：Office 内嵌图片 OCR
- **DOCX**：DrawingML 内嵌图片（inline）、浮动图片（anchor）、VML `v:imagedata`、表格单元格中的图片；
  定位 = 章节 + 段落附近 + 表格行 + 图片序号 + 前后文 + 关系 ID + docPr 形状 ID（DOCX 无稳定页码，不伪造页码）。
- **PPTX**：幻灯片图片（含组合形状中的图片、图片占位符）；保存 Slide 序号、Shape ID / 名称、图片序号、
  Bounding Box（x / y / width / height，EMU）、形状旋转角度。
- **XLSX**：工作表 Drawing 图片（twoCellAnchor / oneCellAnchor / absoluteAnchor）；保存工作表、锚点单元格、图片序号。
- 格式：PNG / JPEG / BMP / TIFF / WEBP / GIF；EMF / WMF / SVG 等矢量格式记录为“不支持 OCR 的格式”并跳过。
- 过滤：1×1 / 极小图、过小图片、小图标 / Logo（按像素与显示尺寸）、透明装饰图、纯色空白图、外部链接图片。
- OCR 缓存：图片 SHA-256 + 引擎键（引擎 / 模型文件指纹 / 语言 / 策略版本）；同一图片只 OCR 一次，
  每个出现位置保留独立记录、独立 Chunk、可被分别引用；模型或策略变化后自动失效。
- 三语 OCR（中文 / English / Bahasa Indonesia）复用本机 PP-OCRv6；启用文本行方向分类，
  结果较差时自动尝试 90° / 270° / 180° 旋转重试；小图自动放大后识别。
- 低置信度：标记 LOW_CONFIDENCE，Chunk 保存置信度；检索排序降权，RAG 上下文标注“OCR低置信度，仅供参考”。
- 数据模型：`office_images`（出现位置）、`ocr_cache`；`chunks.source_type`（BODY / TABLE / NOTES / SCANNED_OCR /
  IMAGE_OCR / OFFICE_IMAGE_OCR）与 `chunks.confidence`；`qa_citations.source_type`；复用既有 source_units / ocr_results。
- 全文搜索、向量检索、RAG 引用均显示“命中来源：内嵌图片OCR”及位置（如《…docx》章节「2 专项检查」· 第4段附近 · 内嵌图片 4）。
- 文档详情：Office OCR 摘要（正文提取 / 内嵌图片 / OCR成功 / 低置信度 / 失败 / 过滤 / 缓存复用）与“内嵌图片”页签；
  子处理：重新解析正文 / 重新OCR扫描页 / 重新OCR内嵌图片 / 重新Chunk / 重新索引 / 重新Embedding。
- 版本更新：新版本中未变化的图片复用缓存，变化的图片重新 OCR，历史版本 OCR 结果保留。

## 新增：处理可靠性（超时 / Watchdog / 有限重试 / 自动跳过）
- 解析与 Office 图片提取在独立子进程（`ESG-XK.exe --parse-helper`）中执行，带“无进展超时”。
- OCR（单页 / 单图）、Embedding（单批）、Reranker、LLM 调用均有无进展超时；超时即终止并重启对应 Helper，
  自动重试 1 次（可配置），仍失败则记录 FAILED / TIMEOUT 并跳过该单元，文档成为“部分成功”，队列继续。
- 预检：缺失 / 无权限 / 0 字节 / 损坏 Office（ZIP）/ 缺失 PDF 文件头在开始阶段快速识别，不进入长时间处理；
  加密 / 损坏 / 空文件 / 无权限 / 不支持格式属于不可恢复错误，不重试。
- GUI 侧独立 Watchdog 线程：监控 Worker 进程、心跳与“有效进展”（页 / 图片 / 批次 / 阶段变化），
  判定 STALLED / CRASHED 后终止进程树、回收 Helper、清理临时文件、事务回滚，当前文档计入卡死次数并自动从 Checkpoint 续跑；
  同一文档超过自动重试上限后标记 TIMEOUT（STALLED）并跳过——不会无限循环，重启程序后也不会重新卡在同一文件。
- 安全停止超过宽限时间无响应 → 升级为受控强制终止（保留 Checkpoint）。
- 资源不足：最多等待宽限时间（默认 60 秒），仍不满足则记录 RESOURCE_DENIED 并跳过，禁止无限“等待资源”。
- 状态体系：Run 新增 COMPLETED_WITH_ERRORS；文档项新增 PARTIAL（部分成功）、TIMEOUT；失败分类
  TIMEOUT / STALLED / UNSUPPORTED / CORRUPTED / ENCRYPTED / ACCESS_DENIED / RESOURCE_DENIED / OCR_FAILED / PARSE_FAILED /
  EMBEDDING_FAILED / INDEX_FAILED / PARTIAL_SUCCESS / SKIPPED。
- Run 结束自动生成失败清单（JSON + CSV）；处理中心可“导出失败清单”与“重试失败项”（全部 / 仅超时 / 仅 OCR 失败 /
  仅部分成功 / 选中文件，均为人工触发）。
- 所有超时、重试、Watchdog 参数可在 设置 → 可靠性与Office OCR 中调整。

## 数据库
- Schema 1 → 2 迁移：新增表与字段；`runs` / `run_items` 按 SQLite 官方重建流程扩展状态约束（foreign_keys=OFF 下执行，
  提交前 foreign_key_check，失败整体回滚）；迁移前自动快照。已在 V1.0.0 正式 EXE 生成的真实数据目录上验证升级与回滚。
- V1.0.0 备份可在 V1.1.0 中恢复；V1.1.0 新增数据随备份 / 恢复完整保留。

## 修复与改进
- 冻结 EXE 子进程协议强制 UTF-8（避免系统 ANSI 代码页导致中文路径错误）。
- 急停 / Watchdog / 恢复均清理 OCR 与解析临时文件；恢复前停止 Watchdog 释放数据库连接。
- 启动恢复清除中断任务的过期阶段信息。

## 行为变化（相对 V1.0.0）
- 队列完成但含失败项时，Run 状态由 COMPLETED 改为 COMPLETED_WITH_ERRORS。
- Worker 崩溃时由 Watchdog 自动续跑（V1.0.0 为标记“已中断”等待用户继续）；Watchdog 不可用时仍保留原有兜底行为。
- 未新增任何计划任务 / 定时 / 自动扫描 / 自动 OCR：所有处理仍需用户手动开始。
