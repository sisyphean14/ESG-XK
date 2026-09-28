# ESG-XK V1.0.0 Release Notes

发布日期：2026-09-28　·　APP_VERSION 1.0.0　·　SCHEMA_VERSION 1　·　Git Tag `ESG-XK-V1.0.0`

## 首个正式版本
- Windows 便携版（无需安装、无需 Python / IDE），主程序 `ESG-XK.exe`。
- 12 个功能页：首页、文档库、导入中心、处理中心、全文搜索、知识检索、AI问答、知识管理、运行控制、备份恢复、日志中心、设置；
  简体中文界面，蓝绿渐变与半透明玻璃质感的企业级视觉，支持高 DPI / Windows 11。
- 文档导入：单/多文件、文件夹、递归、拖拽；来源目录增量扫描 / 完全重新同步；SHA-256 去重；版本、重命名、缺失识别；
  引用原始文件（只读）/ 托管副本；正式导入队列（暂停 / 继续 / 取消 / 重试）。
- 文档版本链：历史版本保留，记录生效/失效时间、SHA 变化与解析 / OCR / 索引历史。
- 解析：PDF（页码/段落/标题）、DOCX（标题层级/段落/表格）、XLSX（Sheet/行）、PPTX（Slide/表格/备注）、TXT、MD、CSV、
  PNG/JPG/JPEG/WEBP/BMP/TIFF（多帧）。所有文本单元保留 Locator。
- OCR：本机 PP-OCRv6（中文 / English / Bahasa Indonesia），页级原子提交，低置信度标记 LOW_CONFIDENCE，可重新 OCR。
- 结构化 Chunk（页 / Sheet / Slide / 标题 / 表格边界）、SQLite FTS5 全文检索（CJK 精确子串匹配）、元数据检索、
  FAISS 向量检索、Hybrid（RRF）+ Reranker、三语跨语言检索与可解释降级。
- 本地 RAG：回答必须带引用（文档 / 版本 / 页码·Sheet·Slide·段落 / 原文片段），无依据时显示“知识库中未找到足够依据”；
  默认仅使用“已批准 + 当前有效”文档。Citation 作为正式数据结构持久化。
- 知识治理：草稿 / 待审核 / 已批准 / 已失效、有效期、标签、业务元数据与自定义字段、知识条目（ESG 主题）。
- Run 体系：两种 Pipeline（可检索语料 / 本地知识标准），暂停、继续、安全停止、急停、Checkpoint 续跑、重试失败、崩溃恢复。
- 急停：立即终止 Worker 与 OCR / Embedding / LLM 子进程，校验进程、端口、租约、数据库完整性并生成报告。
- 资源管理：LOW_MEMORY_16G（默认，重型 AI 并发 1）/ STANDARD_32G，RESOURCE_DENIED 门禁。
- 数据安全：WAL、外键、事务、Migration Registry（失败回滚 + 快照）、启动/备份前/恢复后完整性检查、Data Root Lease、
  Profile strict_no_fallback、备份（Manifest + SHA-256）与恢复（安全备份 + 回滚）。
- 日志中心：运行 / 错误 / 技术 / 审计 / 处理 / OCR / RAG / Migration / Backup / 急停等分类，统一错误码。
- 明确不包含：计划任务、定时 / 夜间处理、后台常驻服务、联网、遥测、账号。

## 已知限制
见 `ACCEPTANCE_REPORT.md` 的“尚存限制”一节。
