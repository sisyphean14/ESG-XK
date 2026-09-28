# ESG-XK V1.0.0 — 需求冻结（Project Spec）

状态：FROZEN（2026-09-28）

## 1. 产品定位
Windows 本地 ESG 文档知识库管理系统。LOCAL FIRST，核心功能完全离线；AI 资产为可选增强。

## 2. 硬约束
| 编号 | 约束 |
|---|---|
| C1 | 源文件只读：不得修改/覆盖/删除/重命名/写入用户源文件（托管副本仅在用户选择时复制） |
| C2 | 禁止任何定时/计划/夜间/自动处理；所有解析、OCR、Embedding、索引、重处理均由用户点击触发 |
| C3 | 程序退出后不得在后台继续处理（Windows Job Object 保证子进程随主进程退出） |
| C4 | 数据库：SQLite WAL、foreign_keys=ON、事务、Migration Registry、启动/备份前/恢复后 integrity_check |
| C5 | 急停：立即终止 Worker 与全部 Helper 子进程，未提交 Stage 不进入正式库（事务原子提交） |
| C6 | 安全停止：当前最小原子 Stage 完成→提交→写 Checkpoint→释放资源→Worker 退出；可续跑 |
| C7 | RAG 回答必须有引用；无依据时显示“知识库中未找到足够依据” |
| C8 | 默认 RAG 仅使用“已批准 + 当前有效”文档 |
| C9 | Profile strict_no_fallback：数据目录失效时阻止写入，不得静默切换到新空库 |
| C10 | Data Root Lease 防止多实例写同一数据库；stale 检测（PID + heartbeat + OS 文件锁） |
| C11 | AI 资产只引用原路径：不移动、不复制、不删除、不改写模型文件 |
| C12 | LOW_MEMORY_16G 默认资源 Profile，重型 AI 并发 = 1，资源不足返回 RESOURCE_DENIED |
| C13 | 无 Telemetry、无云同步、无账号、不上传文件 |

## 3. 功能范围（V1.0.0）
首页 Dashboard、文档库、导入中心（单/多文件、文件夹、递归、拖拽、来源目录扫描/重新同步/增量扫描、重命名/删除/重复识别、SHA-256 去重、引用/托管两种模式、导入队列）、文档版本链、处理中心（两种 Pipeline：SEARCHABLE_CORPUS 可检索语料 / LOCAL_KNOWLEDGE_STANDARD 本地知识标准）、解析（PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/图片）、OCR（PP-OCR，中/英/印尼语）、结构化 Chunk + Locator、FTS5 全文检索、元数据检索、向量检索（FAISS）、Hybrid + Reranker、三语跨语言检索、本地 RAG + Citation、知识治理（草稿/待审核/已批准/已失效、有效期）、标签与自定义字段、Run 体系（暂停/继续/安全停止/急停/Checkpoint 续跑/重试失败）、资源管理、备份/恢复、日志中心、设置（数据目录、AI 资产中心、资源 Profile、检索策略）。

## 4. 明确不做（V1.0.0）
计划任务、定时器驱动的处理、后台常驻服务、联网能力、账号体系、云同步。

## 5. 验收
见 `TEST_PLAN.md` 第 5 节 50 项验收门槛；缺陷等级 P0–P3 定义同原始需求。
