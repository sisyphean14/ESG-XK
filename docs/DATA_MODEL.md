# ESG-XK 数据模型（Schema V1）

数据库：`<Data Root>/database/esgxk.db`（SQLite，WAL，foreign_keys=ON，synchronous=FULL）。
版本：`PRAGMA user_version` + `schema_migrations`。定义见 `src/esgxk/db/schema_v1.py`。
日志库：`<Data Root>/logs/logs.db`（独立，避免与业务事务争用）。

## 文档与版本
| 表 | 关键字段 | 说明 |
|---|---|---|
| documents | document_id, logical_id, title, current_version_id, source_id, storage_mode(REFERENCE/MANAGED), file_state(PRESENT/MISSING), governance_status(DRAFT/PENDING_REVIEW/APPROVED/EXPIRED), is_valid, approved_at, effective_from/to, language, business_domain, department, doc_type, project, region, year, version_label, note, process_status, pipeline, is_deleted | 逻辑文档 |
| document_versions | version_id, document_id, version_no, sha256, file_name, ext, abs_path, size, mtime, mime, language, managed_path, imported_at, last_scanned_at, is_current, valid_from, valid_to, superseded_by, change_reason, parse/ocr/chunk/index/embed/knowledge_status, process_status, last_error_code | 版本链；旧版本保留，不覆盖 |
| document_events | event_type(IMPORTED/VERSION_CREATED/RENAMED/SOURCE_MISSING/GOVERNANCE/VALIDITY/REMOVED) | 文档事件 |
| sources | root_path, recursive, storage_mode, last_scan_at | 来源目录（重新同步/增量扫描） |
| tags / document_tags | | 标签 |
| custom_fields / document_field_values | | 自定义字段 |
| import_batches / import_items | status(WAITING/PROCESSING/DONE/FAILED/SKIPPED/DUPLICATE/PAUSED/CANCELLED), action(NEW/NEW_VERSION/UNCHANGED/DUPLICATE/RENAMED), sha256, error_code | 导入队列 |

## 解析 / OCR / 知识层
| 表 | 说明 |
|---|---|
| source_units | 解析单元：unit_type(heading/paragraph/table/sheet_rows/slide_text/slide_notes/page_image/image)，text，locator(JSON)，page/sheet/row_start/row_end/slide/paragraph_index/heading_path，needs_ocr，ocr_status |
| ocr_results | engine, engine_version, languages(zh,en,id), status(DONE/LOW_CONFIDENCE/FAILED), confidence, raw_text, lines_json, is_current（保留历史） |
| chunks | chunk_id, document_id, version_id, source_unit_id, chunk_index, chunk_type, text, char_count, token_estimate, language, locator, page/page_end/sheet/row_start/row_end/slide/heading/paragraph_start/paragraph_end |
| chunks_fts | FTS5(title, body, meta)，rowid = chunk_id；CJK 单字切分；删除 chunks 时触发器同步删除 |
| chunk_embeddings | chunk_id, model_id, dim, vector(float32 BLOB)；向量索引（FAISS）由此表派生 |
| knowledge_items | kind(SECTION/TABLE/NOTES), title, summary, keywords, esg_topic, locator, chunk_id |

## Run / Checkpoint
| 表 | 说明 |
|---|---|
| runs | run_id, pipeline, status(CREATED/RUNNING/PAUSED/STOP_REQUESTED/SAFE_STOPPED/EMERGENCY_STOPPED/COMPLETED/FAILED/INTERRUPTED), control_request(NONE/PAUSE/STOP), total/succeeded/failed/skipped, current_stage, checkpoint(JSON), worker_pid, worker_create_time, heartbeat_at, resume_count, resource_info |
| run_items | 每个文档版本一项：stages(JSON), status, last_completed_stage, current_stage, error_code |
| checkpoints | 每次原子提交写一条（与正式数据同一事务） |
| processing_events | 解析/OCR/索引历史（每个 Stage 一条） |
| helper_processes | OCR / EMBED / RERANK / LLM Helper：pid, create_time, port, owner_pid, status |

## RAG
| 表 | 说明 |
|---|---|
| qa_sessions | question, answer, status(ANSWERED/INSUFFICIENT/REJECTED/EVIDENCE_ONLY/ERROR), retrieval_mode, model, scope |
| qa_citations | Citation 正式结构：document_id, version_id, chunk_id, source_unit_id, locator, page, sheet, slide, excerpt, cited, score |

## 系统
`schema_migrations`、`app_settings`（JSON 值）、`counters`（embeddings_generation，用于向量索引失效判断）。
