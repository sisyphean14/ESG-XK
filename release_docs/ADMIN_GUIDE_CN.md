# ESG-XK 管理员说明

## 1. 部署
便携版，无需安装：复制 `ESG-XK-1.0.0-portable` 目录到目标位置即可。首次运行选择数据目录（默认便携目录同级 `ESG-XK-Data`）。
应用程序与用户数据分离：升级时替换便携目录，数据目录保持不变（启动时自动执行 Schema Migration，迁移前自动快照，失败自动回滚）。

## 2. 数据目录（Data Root）
```
ESG-XK-Data\
  ESG-XK-DATA.json   数据目录标识（data_root_id）
  database\          esgxk.db（SQLite WAL）、迁移前快照
  documents\ managed_files\ ocr\ index\ vector\ cache\ runs\ logs\ backup\ export\ temp\
```
- `runs\app.lock / app.lease.json`：数据目录租约（防止两个实例同时写入）；`worker.lock`：处理租约；`heavy_slot_*.lock`：重型 AI 槽位。
- 租约使用操作系统文件锁 + PID / 心跳 / 时间戳；程序异常退出后锁由系统释放，残留的租约文件在下次启动时判定为 stale 并回收。
- 切换：设置 → 数据目录（需先安全停止任务）。旧目录保持原样。

## 3. 本机 Profile
`%LOCALAPPDATA%\ESG-XK\profiles\default.json`：APP 路径、Data Root、AI 资产路径（llama-server、Embedding、Reranker、LLM、
PP-OCR Runtime 与模型、Model/Runtime/Tools Root）、资源 Profile、Profile 版本；`strict_no_fallback = true`。
数据目录失效时显示“数据目录不可用”并阻止写入，**不会**自动切换到新的空数据库；可选择重试、选择已有目录或明确创建新目录。
模板见 `config\profile.template.json`。

## 4. AI 资产中心（设置 → AI资产中心）
ESG-XK 不下载、不复制、不移动、不修改模型文件，只引用原路径。首次运行在有限候选路径中探测（不做全盘扫描）：
- llama.cpp：`llama-server.exe`（Embedding / Reranker / LLM 均以 127.0.0.1 随机端口的本地 Helper 运行）；
- GGUF 模型：Embedding（如 Qwen3-Embedding）、Reranker（如 Qwen3-Reranker）、LLM（如 Qwen3.5-4B / Qwen3-VL-2B）；
- PP-OCR：含 paddleocr / paddle 的 Python 运行时 + PP-OCR 模型目录（detection / recognition / textline_orientation，需纯 ASCII 路径）。
可在下拉框中选择候选、浏览指定、重新探测，并用“测试 OCR / Embedding / Reranker / LLM”验证。更换 Embedding 模型后需对文档重新向量化。
未配置的能力显示“尚未配置AI资产”，文档管理 / 全文检索 / 元数据检索等照常使用。

## 5. 资源 Profile
- `LOW_MEMORY_16G`（默认）：重型 AI 并发 1，保留内存 2 GB，OCR 4 线程；OCR / Embedding / Reranker / LLM 由资源管理器统一调度，
  同一时刻只驻留一个重型模型；处理任务运行时交互式 AI（问答/语义检索）会被 `RESOURCE_DENIED` 拒绝并自动降级。
- `STANDARD_32G`：重型并发 2。

## 6. 进程模型与退出保证
GUI 与由其启动的 Worker / Helper 均加入 Windows Job Object（KILL_ON_JOB_CLOSE）：程序退出或崩溃时操作系统强制结束全部子进程。
ESG-XK 不创建计划任务、不注册服务、不设置开机自启。

## 7. 知识治理
默认 RAG 仅使用“已批准 + 当前有效（有效期内、未标记无效）+ 当前版本”。设置 → 检索与问答 可允许 RAG 使用草稿（管理员）。

## 8. 备份与恢复
备份文件：`backup\ESGXK-<类型>-<时间>.zip` + `.sha256`；ZIP 内 `MANIFEST.json` 含时间、版本、Schema、文件列表与 SHA-256。
恢复流程：校验 → 自动安全备份 → 临时目录解压与校验 → 替换 → integrity_check / foreign_key_check → 失败回滚。

## 9. 日志与排障
`logs\logs.db`（日志中心数据源）、`logs\esgxk-gui.log`、`esgxk-worker.log`、`*-error.log`、`helper-*.log`（OCR / llama Helper 输出）、
`runs\estop-*.json`（急停报告）、`runs\last_shutdown.json`（退出核对）。所有错误带错误码（IMPORT_* / PARSE_* / OCR_* / INDEX_* /
VECTOR_* / RAG_* / ASSET_* / RESOURCE_* / DB_* / BACKUP_* / RESTORE_* / PROFILE_* / RUN_* / ESTOP_*）。

## 10. 命令行
`ESG-XK.exe --version`：显示版本与 Schema。`--worker ...` 为内部参数（由程序自身启动 Worker 使用，请勿手动调用）。
`--acceptance <report.json>`：交付验收模式（自动驱动界面并生成验收报告，仅用于测试数据目录）。
