# ESG-XK 架构

## 1. 分层
```
UI Layer (PySide6, esgxk/ui)
  ↓ 仅调用
Application Services (esgxk/services: AppContext, DocumentService, SearchService, RagService, RunController, BackupService)
  ↓
Processing Pipeline (esgxk/processing: pipeline stages, worker process)
  ↓
Document Registry (esgxk/ingest: importer, scanner, versioning, managed copy)
  ↓
Parser / OCR (esgxk/parsers, esgxk/ocr)
  ↓
Chunk / Knowledge (esgxk/chunking, esgxk/knowledge)
  ↓
FTS / Metadata / Vector Index (esgxk/index)
  ↓
Hybrid Retrieval → Reranker → RAG → Citation (esgxk/retrieval, esgxk/ai)
```
独立横切模块：Run Control（processing/run_control）、Checkpoint（run_items + checkpoints 表）、Resource Manager（ai/resources）、Audit/Logs（core/logs）、Backup/Restore（backup/）、Profile（core/profile）、Migration（db/migrations）、Emergency Stop（processing/run_control + core/procutil）。

## 2. 进程模型
| 进程 | 说明 | 生命周期 |
|---|---|---|
| ESG-XK.exe（GUI） | 持有 Data Root Lease；读写元数据；启动 Worker | 用户启动/关闭 |
| ESG-XK.exe --worker（Processing Worker） | 执行某个 Run 的 Pipeline；持有 worker.lock | 用户点击开始/继续时启动；安全停止/完成/急停时退出 |
| PP-OCR Helper（外部 runtime python + esgxk_ocr_worker.py） | JSON Lines 协议 OCR | 由 Worker 按需启动，Stage 结束/停止时关闭 |
| llama-server Helper（embedding / reranker / llm） | 本地 127.0.0.1 随机端口 | 按需启动，资源门禁控制，急停立即终止 |

所有子进程均加入 GUI 进程创建的 Windows Job Object（JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE），GUI 退出/崩溃时操作系统强制回收全部子进程 —— 保证“程序退出后不在后台继续处理”。

## 3. Run 控制协议
- GUI 与 Worker 通过数据库 `runs.control_request`（NONE/PAUSE/STOP）通信；Worker 在每个原子 Stage 边界检查。
- 每个 (文档版本, Stage) 的输出与 `run_items.last_completed_stage`、`checkpoints` 在 **同一个 SQLite 事务** 中提交 → Checkpoint 与正式数据永远一致。
- 急停：GUI 直接终止 Worker 进程树 + 数据库中登记的全部 Helper（校验 pid + create_time 防误杀），未提交事务由 SQLite WAL 自动丢弃；随后写 EMERGENCY_STOPPED、释放 worker.lock、校验端口、执行 integrity_check。
- Worker 监视父进程（GUI）存活；父进程消失即退出。

## 4. 数据安全
- 源文件：仅以只读方式打开（`open(path,'rb')`），导入前后比较 size/mtime；托管副本 copy→sha 校验→原子 rename。
- 向量索引（FAISS）是可重建派生物：来自 `chunk_embeddings` 正式表，写 temp → 校验 → os.replace。
- Migration：迁移前自动复制数据库快照，单事务执行；失败回滚并恢复快照。
- Backup：sqlite backup API 生成一致快照 → zip + MANIFEST（sha256）。Restore：校验 Manifest/SHA/Schema → 先做安全备份 → temp 恢复 → integrity_check → 原子替换，失败回滚。

## 5. 检索
- FTS5（unicode61）+ CJK 单字切分：索引与查询均将中日韩字符按字切分，中文查询转为短语查询 → 任意长度中文子串精确匹配；拉丁语系按词（去重音）。
- 向量：Qwen3-Embedding（多语，1024 维）→ FAISS IndexFlatIP（精确内积）。
- Hybrid：FTS 召回 + 向量召回 → RRF 融合 → Qwen3-Reranker 重排（可用时）。
- 降级：Embedding 不可用 → 全文检索并显示原因；Reranker 不可用 → RRF 排序；LLM 不可用 → 仅展示证据片段。

## 6. 模块清单
见 `src/esgxk` 目录，每个子包职责单一；UI 页面位于 `esgxk/ui/pages/`。
