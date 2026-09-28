# ESG-XK V1.0.0 最终测试报告

测试日期：2026-09-28　·　测试机：Windows 11 Home China 10.0.26200，31 GB 内存，RTX 5060 Laptop 8 GB，
测试语料：D:\ESG-XK\ceshi（39 个基线源文件 + 运行时工作区）

## 1. 自动化测试（pytest，最终回归）
命令：`pytest -q --junitxml=reports/junit_final.xml -s`（gongcheng）
结果：**65 个测试，65 PASS，0 FAIL，0 ERROR，0 SKIP**，耗时 829 s。全部使用真实组件：真实 Worker 子进程、真实 PP-OCRv6、
真实 llama.cpp（Qwen3-Embedding / Qwen3-Reranker / Qwen3.5-4B），无 mock。

| 模块 | 数量 | 结果 | 覆盖 |
|---|---:|---|---|
| test_unit_core | 8 | PASS | 错误码、语言识别、FTS 规范化、Locator、切分、知识结构化、资源 Profile |
| test_db_migration_profile | 10 | PASS | Migration（新建/升级/失败回滚/过新拒绝）、事务、外键、Data Root、Profile strict_no_fallback、Lease/stale、数据目录切换 |
| test_import_parse | 16 | PASS | 递归导入、SHA-256 去重、版本、重命名、缺失、托管副本、各格式解析、损坏/空文件、超长文档 |
| test_pipeline_ai | 14 | PASS | 真实 OCR 三语、低置信度、Locator、FTS、元数据、跨语言向量、Hybrid+Reranker、RAG+Citation、无依据、治理范围、降级 |
| test_run_control | 7 | PASS | 暂停/继续、真实安全停止+续跑、真实急停+重启+续跑、Embedding 急停端口释放、崩溃恢复、并发 Run 拒绝、运行中关闭 |
| test_backup_resource_ui | 9 | PASS | 备份 Manifest/SHA、恢复往返、篡改/过新拒绝、恢复回滚、运行中禁止、资源门禁、资产发现、无定时任务代码、UI Smoke |
| test_zz_source_integrity | 1 | PASS | 39 个源文件 size/mtime/SHA-256 零变化 |

## 2. 安全停止真实测试证据
Run RUN-20260928-180923-3997（6 个扫描 PDF × 3 页，真实 OCR）：运行中请求安全停止 → 状态 STOP_REQUESTED → 当前页 OCR 提交 →
Worker 退出（PID 不存在）→ Helper 0 → 处理租约释放 → Checkpoint `{"stage":"OCR","completed":true,...}` → 已完成 3/18 页 →
数据一致性检查 0 异常 → 继续 → resume_count=1 → 完成 18/18 页；`ocr_results` 恰好 18 行（已提交页未重复 OCR）。

## 3. 急停真实测试证据
Run RUN-20260928-181334-D899：OCR 进行中触发急停 → 167 ms 完成；终止 Worker 进程树 5 个进程（含 OCR Helper PID 68440）；
残留 Worker 0、Helper 0、端口 0；处理租约已释放；重型槽位 0；integrity_check = ok；foreign_key_check 0；状态 EMERGENCY_STOPPED；
Checkpoint 保留；无半写入（OCR 结果/Chunk/FTS 一致性 0 异常）→ 关闭并重新打开程序，启动完整性检查 ok → 继续 → 完成 18/18 页。
Embedding 阶段急停：llama-server 端口急停后不再监听；向量仅包含完整提交的批次。

## 4. EXE 真实验收（最终构建）
EXE：`D:\ESG-XK\jiaofu\ESG-XK-1.0.0-portable\ESG-XK.exe`（SHA-256 f6b21254157e0394f5312f93d6da53228b04ca10c687a5b90955d47823654bee）
- `ESG-XK.exe --acceptance`（冻结 EXE 内驱动真实界面，独立 Profile / Data Root，首次运行对话框由 EXE 自身处理并创建数据目录）：
  **22 步全部 PASS**，耗时约 290 s，每步截图保存于 `D:\ESG-XK\ceshi\work\exe_accept\screens`。
  覆盖：首页、导入（28 新文档 / 2 重复 / 1 跳过）、文档库、开始处理、暂停（暂停期间完成数不变）、继续、安全停止、Checkpoint 续跑、
  急停（419 ms，校验通过）、急停后续跑至完成（24 成功 / 4 损坏文件失败）、批量批准、全文搜索、元数据搜索、混合+Reranker 跨语言检索、
  AI 问答（带 4 条引用）、无依据问题、完整备份、恢复（前后 28/28 文档，integrity ok）、日志中心、AI 资产中心（6 项可用）、运行控制、退出。
  EXE 退出码 0；退出后残留子进程 0（按 create_time 校验）；last_shutdown：Worker 0 / Helper 0 / 端口 0 / 租约已释放。
- 普通方式启动（无参数，等同双击）：2.5 s 出现主窗口「ESG-XK 本地ESG文档知识库 V1.0.0」，空闲时无子进程；
  发送窗口关闭消息后正常退出（退出码 0），退出核对全部为 0。

## 5. 数据库验收
所有完整性校验均以新连接执行：`PRAGMA integrity_check = ok`，`PRAGMA foreign_key_check` 无错误（测试、急停、恢复、EXE 验收全部通过）。

## 6. 开发期发现并修复的缺陷
| 等级 | 缺陷 | 处理 |
|---|---|---|
| P1 | 恢复前自动安全备份与被恢复备份在同一秒创建时文件名相同，可能覆盖被恢复的备份 | 备份文件名改为毫秒 + 随机后缀且绝不覆盖；新增测试断言原备份 SHA 不变 |
| P2 | 急停校验在长生命周期连接上执行 PRAGMA integrity_check，Worker 写入 FTS5 后出现“malformed inverted index”误报（新连接与 FTS5 自检均为 ok） | 所有正式完整性校验改用新连接；Worker 禁用 WAL 自动 checkpoint，急停只可能中断未提交的 WAL 追加 |
| P2 | 急停报告未计入随 Worker 进程树一起终止的 Helper | 急停前快照已登记 Helper 并全部计入、标记与校验 |
| P3 | 新主题下单选按钮选中状态指示不可见 | 增加主题化 RadioButton / CheckBox 指示器 |
| P3 | 设置页 AI 测试在后台线程访问 GUI 数据库连接 | 改为在 GUI 线程读取状态 |

当前未关闭缺陷：P0 = 0，P1 = 0，P2 = 0，P3 = 0（尚存限制见验收报告）。
