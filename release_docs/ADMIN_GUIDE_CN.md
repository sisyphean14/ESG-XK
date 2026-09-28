# ESG-XK 管理员说明（V1.1.0）

## 1. 部署
便携版，无需安装：复制 `ESG-XK-1.1.0-portable` 目录到可写的目标位置即可。首次运行选择数据保存位置：程序文件夹内（便携模式，推荐）或自定义目录（见第 16 节）。
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
标准模式 `%LOCALAPPDATA%\ESG-XK\profiles\default.json`，便携模式 `<程序文件夹>\profile\default.json`：APP 路径、Data Root、AI 资产路径（llama-server、Embedding、Reranker、LLM、
PP-OCR Runtime 与模型、Model/Runtime/Tools Root）、资源 Profile、Profile 版本；`strict_no_fallback = true`。
数据目录失效时显示“数据目录不可用”并阻止写入，**不会**自动切换到新的空数据库；可选择重试、选择已有目录或明确创建新目录。
模板见 `config\profile.template.json`。

## 4. AI 资产中心（设置 → AI资产中心）
ESG-XK 不复制、不移动、不修改已有模型文件，只引用原路径；只有用户在“模型下载”页主动下载时才会在所选位置安装新模型（第 14 节）。首次运行在有限候选路径中快速探测；【扫描电脑…】由用户手动选择范围后全面查找（第 15 节）：
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


## 11. V1.1.0 升级
- 直接用 V1.1.0 便携版打开原数据目录：启动时自动执行 Schema 1 → 2 迁移（迁移前在 database\ 下生成快照
  pre-migration-v1-*.db；迁移在单事务中执行，失败自动回滚并保持 V1 数据库不变）。原有文档、索引、OCR、知识、Run、
  问答与备份全部保留；V1.0.0 的备份文件可以在 V1.1.0 中恢复（在临时副本上自动迁移）。
- V1.0.0 便携目录无需删除，可保留作为回退版本；但 Schema 2 数据库不能再被 V1.0.0 打开（会提示“数据库版本高于本程序”）。

## 12. 进程与超时机制（V1.1.0）
| 进程 | 作用 | 超时 / 回收 |
|---|---|---|
| ESG-XK.exe --worker | 处理 Worker | GUI 侧独立 Watchdog：心跳超时（默认 90 秒）或连续无有效进展（默认 900 秒）→ 终止进程树 → 当前文档计入卡死次数 → 自动从 Checkpoint 续跑；同一文档卡死超过自动重试上限后标记 TIMEOUT/STALLED 并跳过 |
| ESG-XK.exe --parse-helper | 解析与 Office 图片提取子进程 | 解析无进展超时（默认 120 秒）→ 终止并重启，重试 1 次 |
| PP-OCR Helper | OCR | 单张图片 / 单页无进展超时（默认 180 秒）→ 终止并重启，重试 1 次 |
| llama-server | Embedding / Reranker / LLM | 单批次 / 单次调用超时（180 / 180 / 300 秒） |
安全停止超过宽限时间（默认 300 秒）无响应时自动升级为受控强制终止（保留 Checkpoint）。所有参数见 设置 → 可靠性与Office OCR。

## 13. OCR 缓存
表 ocr_cache 以 图片 SHA-256 + 引擎键（引擎、模型文件指纹、语言、识别策略版本）缓存 Office 图片 OCR 结果；
更换 PP-OCR 模型或升级识别策略后缓存自动失效。Office 图片出现位置记录在 office_images 表；两者均随数据库备份与恢复。

## 14. 模型下载中心（V1.1.0）
- 内置清单 `_internal\esgxk\resources\models\catalog.json`：每个组件固定版本、文件大小、SHA-256 与多个下载源
  （llama.cpp b11228 CPU / CUDA 12.4；Qwen3-Embedding-0.6B Q8_0、Qwen3-Reranker-0.6B Q8_0、Qwen3-1.7B Q8_0、Qwen3-4B Q4_K_M；
  PP-OCRv5 mobile det / rec + PP-LCNet textline_ori；python-3.12.10-embed-amd64 + pip wheel + paddlepaddle==3.3.1 / paddleocr==3.7.0）。
- 下载写入 `<安装位置>\.downloads\*.part`（断点续传，Range 请求），校验 SHA-256 后才移动到最终位置；不一致即丢弃换源；
  单个下载源 60 秒无数据视为卡住并换源；pip 安装子进程以“输出行 / 目录增长”作为进展，15 分钟无进展即终止。
- 安装目录布局：`llama.cpp-cpu\` 或 `llama.cpp-cuda\`、`embedding\`、`reranker\`、`llm\`、`ppocr-models\`、`ppocr-runtime\`，
  以及安装记录 `ESGXK-MODELS.json`（扫描电脑会读取它）。
- PP-OCR 运行时为独立的嵌入式 Python（修改其 `python312._pth` 启用 site-packages），pip 缓存放在安装目录内并在完成后删除，
  不修改系统 Python、不写注册表、不设置环境变量。
- 下载在 GUI 进程的后台线程执行，急停与退出程序都会取消下载并结束 pip 子进程，不留后台任务。
- 许可证：各模型 / 运行时以其自身许可证为准（Qwen3：Apache-2.0；PaddleOCR / PaddlePaddle：Apache-2.0；llama.cpp：MIT；Python：PSF）。

## 15. 扫描电脑（V1.1.0）
- 仅由用户点击开始；扫描所选磁盘 / 文件夹，跳过 `Windows`、回收站、`System Volume Information`、`node_modules`、`.git`、
  `site-packages`、`%LOCALAPPDATA%\Temp` 等目录；GGUF 需以 `GGUF` 文件头开头且不小于 20 MB（排除 mmproj / vocab 文件）。
- 只读取目录项与文件头；PP-OCR 运行时通过其 `Lib\site-packages\paddleocr` 与 `paddle` 目录判断；模型目录通过 `inference.yml` 中的 model_name 识别。

## 16. 便携模式与数据目录迁移（V1.1.0）
- 程序文件夹内存在 `portable.flag` 即为便携模式：Profile 位于 `<程序文件夹>\profile\default.json`（优先于 %LOCALAPPDATA%），
  程序文件夹内的路径（数据目录、AI 资产）以**相对路径**保存，读取时相对程序文件夹解析——整个文件夹可移动。
  删除 `portable.flag` 即恢复标准模式（Profile 位于 `%LOCALAPPDATA%\ESG-XK\profiles`）。
- 迁移（设置 → 数据目录）：检查目标为空、不与源互相包含、磁盘空间充足 → 关闭数据库与租约 → 复制（跳过 temp\ 与租约锁文件）
  → 逐文件 SHA-256 比对 → 目标数据库 integrity_check → 切换 Profile 并打开新目录；任一步失败或取消：清理目标、重新打开原目录。
  源目录从不删除。
- `build_release.py` 保证交付包不包含 `portable.flag`、`profile\`、`ESG-XK-Data\`、`AI-Models\`。
