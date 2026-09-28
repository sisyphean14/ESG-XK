# ESG-XK 测试计划

## 1. 测试语料（D:\ESG-XK\ceshi）
由 `scripts/gen_testdata.py` 确定性生成：PDF 文字版（中/英/印尼）、PDF 扫描版（中/英/印尼）、DOCX（标题/表格）、XLSX（3 个 Sheet）、
PPTX（幻灯片/表格/备注）、TXT、MD、CSV、PNG/JPG/WEBP/BMP/多帧 TIFF、低质量 OCR 样本、重复文件（不同名相同内容）、同名不同内容、
损坏 PDF/DOCX/XLSX、空文件、不支持格式、超长文档（约 30 万字）、版本模板 v1/v2、运行控制语料（6 个 × 3 页扫描 PDF）。
`SOURCE_BASELINE.json` 记录全部源文件 size / mtime / SHA-256。

## 2. 自动化测试（pytest，tests/）
| 模块 | 覆盖 |
|---|---|
| test_unit_core | 错误码体系、语言识别、FTS 规范化/查询构建/高亮、Locator、结构化切分、知识结构化、资源 Profile |
| test_db_migration_profile | 新建迁移、WAL/外键、迁移失败回滚+快照、升级迁移、Schema 过新拒绝、事务回滚、Data Root 严格校验、Profile strict_no_fallback、数据目录失效不回退、租约阻止第二实例、stale 租约回收、数据目录切换 |
| test_import_parse | 递归导入、SHA-256 去重、同名不同内容、增量扫描、版本链、重命名识别、删除（缺失）识别、托管副本（源文件不变）、手动新版本、各格式解析与定位、损坏/空文件错误码、超长文档、SHA 校验 |
| test_pipeline_ai | 真实 Worker + 真实 PP-OCR + 真实 llama.cpp：单文档失败隔离、三语 OCR、低置信度、Locator、FTS 过滤、元数据/标签检索、跨语言向量检索、Hybrid+Reranker、RAG+Citation 持久化、无依据回答、治理范围（草稿/无效排除）、知识条目、完整性、无 AI 资产时的降级 |
| test_run_control | 暂停/继续、**真实 OCR 安全停止 + Checkpoint 续跑**、**真实 OCR 急停 + 重启 + 续跑**、Embedding 急停端口释放、Worker 崩溃→INTERRUPTED→续跑、第二个 Run 拒绝、运行中关闭无残留 |
| test_backup_resource_ui | 完整备份 Manifest/SHA、恢复往返、篡改备份拒绝、Schema 过新拒绝、恢复失败回滚、运行中禁止备份/恢复、资源门禁（槽位/内存）、GUI 与 Worker 互斥、AI 资产发现只读有界、**源码中无定时/计划任务实现**、UI Smoke |
| test_zz_source_integrity | 全部测试后源文件 size / mtime / SHA-256 与基线一致 |

## 3. EXE 真实验收
`ESG-XK.exe --acceptance report.json`（冻结 EXE 内驱动真实 GUI）：首页 → 导入 → 文档库 → 开始 → 暂停 → 继续 → 安全停止 →
Checkpoint 续跑 → 急停 → 急停后续跑至完成 → 全文/元数据搜索 → 知识检索 → AI 问答（有依据/无依据）→ 备份 → 恢复 → 日志 →
AI 资产中心 → 运行控制 → 关闭核对；每步截图。另以普通双击方式启动 EXE 并截图确认界面。

## 4. 数据库验收
`PRAGMA integrity_check = ok`，`PRAGMA foreign_key_check` 无结果（均使用新连接执行）。

## 5. 最终验收门槛
按需求第五十七节 50 项逐项记录于 `jiaofu/ESG-XK-1.0.0-portable/ACCEPTANCE_REPORT.md`；未实际运行的测试不得标记 PASS；
受本机资产限制的项目标记 PASS WITH SCOPE 并说明。
