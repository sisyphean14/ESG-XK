# ESG-XK V1.0.0 验收报告

| 项目 | 内容 |
|---|---|
| 产品 | ESG-XK V1.0.0 |
| 开发状态 | **PRODUCTION READY** |
| APP_VERSION / SCHEMA_VERSION | 1.0.0 / 1 |
| Git Tag | ESG-XK-V1.0.0（提交号见 VERSION.txt） |
| 工程路径 | D:\ESG-XK\gongcheng |
| 测试语料 | D:\ESG-XK\ceshi |
| 交付路径 | D:\ESG-XK\jiaofu\ESG-XK-1.0.0-portable |
| EXE | D:\ESG-XK\jiaofu\ESG-XK-1.0.0-portable\ESG-XK.exe |
| EXE SHA-256 | f6b21254157e0394f5312f93d6da53228b04ca10c687a5b90955d47823654bee |
| 默认 Data Root | D:\ESG-XK\jiaofu\ESG-XK-Data（便携目录同级，首次运行可修改） |
| 自动化测试 | 65 / 65 PASS，0 FAIL |
| EXE 真实验收 | 22 / 22 PASS；普通启动 / 关闭 PASS |
| 缺陷 | P0 0 · P1 0 · P2 0 · P3 0 |

## 本机 AI 资产（仅引用原路径，未复制 / 移动 / 修改）
| 能力 | 资产 | 状态 |
|---|---|---|
| OCR | PP-OCRv6-small（D:\Document-Knowledge-Hub\models\ocr\pp-ocrv6-small）+ PaddleOCR 3.7 / Paddle 3.3.1 CPU 运行时 | PASS |
| Embedding | Qwen3-Embedding-0.6B-Q8_0.gguf（1024 维，多语） | PASS |
| Reranker | qwen3-reranker-0.6b-q8_0.gguf | PASS |
| LLM / RAG | Qwen3.5-4B-Q4_K_M.gguf（llama.cpp b10720 CUDA，D:\Document-Knowledge-Hub\tools\llama-cpp） | PASS |

## 最终验收门槛（需求第五十七节）
| # | 项目 | 结果 | 证据 |
|---:|---|---|---|
| 1 | ESG-XK.exe 存在 | PASS | 交付目录，SHA-256 见上 |
| 2 | 正式 EXE 能够运行 | PASS | 普通启动 2.5 s 出窗；--acceptance 22 步 |
| 3 | UI 正常 | PASS | 12 页面 + 对话框，验收截图 |
| 4 | 文档导入正常 | PASS | EXE：28 新 / 2 重复 / 1 跳过；test_import_parse |
| 5 | 目录递归扫描正常 | PASS | test_recursive_folder_import_and_dedupe；增量扫描/重新同步 |
| 6 | SHA-256 去重正常 | PASS | 不同名相同内容 → DUPLICATE；同名不同内容 → 两个文档 |
| 7 | 版本识别正常 | PASS | test_versions_rename_missing：v1→v2 版本链、失效时间、superseded_by |
| 8 | PDF 解析 | PASS | 页码/段落/标题 |
| 9 | DOCX 解析 | PASS | 标题层级/段落/表格 |
| 10 | XLSX 解析 | PASS | 3 个 Sheet、行号 |
| 11 | PPTX 解析 | PASS | Slide/表格/备注 |
| 12 | 图片 OCR | PASS | PNG/JPG/WEBP/BMP/多帧 TIFF |
| 13 | 扫描 PDF OCR | PASS | 中/英/印尼扫描件 + 18 页运行控制语料 |
| 14 | 中文 OCR | PASS | 置信度 > 0.99 |
| 15 | 英文 OCR | PASS | |
| 16 | 印尼语 OCR | PASS | 印尼语扫描件与 WEBP 正确识别（PP-OCRv6 拉丁字符识别；无独立印尼语模型，见限制） |
| 17 | Chunk 正常 | PASS | 页/Sheet/Slide/标题/表格边界，超长切分 |
| 18 | Locator 正常 | PASS | test_chunks_have_locators |
| 19 | FTS 搜索 | PASS | FTS5 + CJK 子串匹配 + 高亮 |
| 20 | Metadata 搜索 | PASS | 部门/年份/类型/标签/自定义字段 |
| 21 | Vector Search | PASS | 中文查询命中英文 37% 可再生电力 |
| 22 | Hybrid Search | PASS | RRF 融合 |
| 23 | Reranker | PASS | 印尼语查询首条命中 2.150 ton CO2e |
| 24 | RAG | PASS | 答案 12,345 吨，带引用 |
| 25 | Citation | PASS | qa_citations 持久化（document/version/chunk/source_unit/locator/excerpt） |
| 26 | 文档版本 | PASS | 版本历史可见 |
| 27 | 审核/有效状态 | PASS | 草稿不进入 RAG；无效文档排除；管理员可放开草稿 |
| 28 | Run | PASS | 状态机与计数 |
| 29 | Pause | PASS | 暂停期间完成数不变 |
| 30 | Resume | PASS | |
| 31 | Safe Stop | PASS | 3/18 页处停止，Worker 退出 |
| 32 | Checkpoint 续跑 | PASS | resume_count=1，18/18，页面不重复 OCR |
| 33 | Emergency Stop | PASS | 129–419 ms 完成 |
| 34 | 急停后 Integrity OK | PASS | integrity ok，FK 0，无半写入 |
| 35 | Lease 释放 | PASS | 处理租约释放；数据目录租约退出释放 |
| 36 | Worker 释放 | PASS | 残留 0 |
| 37 | Helper 释放 | PASS | 残留 0（OCR / llama-server） |
| 38 | Port 释放 | PASS | Embedding Helper 端口急停后不再监听 |
| 39 | Backup | PASS | Manifest + SHA-256 |
| 40 | Restore | PASS | 安全备份 + 校验 + 回滚 |
| 41 | Profile | PASS | %LOCALAPPDATA%\ESG-XK\profiles，strict_no_fallback |
| 42 | Data Root 切换 | PASS | test_data_root_switch；失效时明确阻止 |
| 43 | 源文件 0 修改 | PASS | 39 个基线文件 size/mtime/SHA-256 零变化 |
| 44 | 测试全部完成 | PASS | 65/65 + EXE 22/22 |
| 45 | 无 P0 | PASS | |
| 46 | 无 P1 | PASS | 开发期发现的 P1（备份同名覆盖）已修复并回归 |
| 47 | 正式交付包 | PASS | 便携目录 + 文档 + MANIFEST + SHA256SUMS + LICENSES |
| 48 | Git clean | PASS | 发布时 `git status` 为空 |
| 49 | Tag | PASS | ESG-XK-V1.0.0 |
| 50 | 最终验收报告 | PASS | 本文件 + FINAL_TEST_REPORT.md |

## 禁止项核对
- 无计划任务 / 定时 / 夜间处理 / 服务 / 开机自启：源码扫描测试 PASS；程序不注册任何系统任务。
- 程序退出后不继续处理：Job Object 强制回收；运行中关闭测试残留 0。
- 不上传、不联网、无遥测：Helper 仅监听 127.0.0.1 随机端口；OCR 运行时设置离线环境变量。

## 尚存限制
1. DOCX / PPTX 中内嵌的图片不做 OCR（扫描 PDF 与独立图片均支持）。
2. 印尼语 OCR 使用 PP-OCRv6 的拉丁字符识别，无独立印尼语语言模型；本机测试语料识别正确，特殊排版的准确度受引擎限制。
3. OCR 为 CPU 推理（约 3–4 秒/页），首次加载 OCR 模型可能需要约 1 分钟。
4. AI 资产不随交付包分发：换机后需在“AI资产中心”配置本机资产；未配置时 AI 功能显示“尚未配置AI资产”，其余功能正常。
5. 仅全文检索模式下跨语言依赖“跨语言查询扩展”（需本地 LLM）；Embedding 可用时为原生跨语言检索。
6. 加密 PDF 不支持（报 PARSE_ENCRYPTED）；单文件上限 1 GiB。
7. 急停释放“处理租约”；GUI 实例本身的数据目录租约在程序运行期间保持（防止第二个实例写库），退出时释放。
8. EXE 未进行代码签名，首次运行时 Windows SmartScreen 可能提示。
9. “预计剩余”为按平均耗时的粗略估算。
