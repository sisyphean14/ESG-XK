# ESG-XK V1.1.0 验收报告

| 项目 | 内容 |
|---|---|
| 产品 | ESG-XK V1.1.0 |
| 开发状态 | **PRODUCTION READY** |
| APP_VERSION / SCHEMA_VERSION | 1.1.0 / 2 |
| 基线 | ESG-XK-V1.0.0（a403f52），分支 release/v1.1.0 |
| Git Tag | ESG-XK-V1.1.0（提交号见 VERSION.txt） |
| 工程路径 | D:\ESG-XK\gongcheng |
| 测试语料 | D:\ESG-XK\ceshi |
| 交付路径 | D:\ESG-XK\jiaofu\ESG-XK-1.1.0-portable |
| EXE | D:\ESG-XK\jiaofu\ESG-XK-1.1.0-portable\ESG-XK.exe |
| EXE SHA-256 | 3ed6a3cdeeffd98ae568fd43563ece4a89071c447f125da39fac2960c23f61e7 |
| V1.0.0 冻结交付 | D:\ESG-XK\jiaofu\ESG-XK-1.0.0-portable：388 / 388 文件与其 SHA256SUMS 一致，无新增文件（未被修改） |
| 自动化测试 | 122 / 122 PASS，0 FAIL，0 SKIP（V1.0.0 回归 65 项 + V1.1.0 新增 57 项，1750 s） |
| EXE 真实验收 | 33 / 33 PASS（冻结 EXE 内驱动真实界面，469 s，退出码 0，残留子进程 0）；普通启动 / 关闭 PASS |
| 真实下载安装 | 在空目录中真实下载并安装 OCR 模型、完整 PP-OCR 运行时、llama.cpp CPU、Qwen3-Embedding，并用其完成真实 OCR 与 Embedding |
| 缺陷 | P0 0 · P1 0 · P2 0 · P3 见“尚存限制” |

## 本机 AI 资产（仅引用原路径，未复制 / 移动 / 修改）
| 能力 | 资产 | 状态 |
|---|---|---|
| OCR | PP-OCRv6-small + PaddleOCR 3.7.0 / Paddle 3.3.1 CPU 运行时 | PASS |
| Embedding | Qwen3-Embedding-0.6B-Q8_0.gguf | PASS |
| Reranker | qwen3-reranker-0.6b-q8_0.gguf | PASS |
| LLM / RAG | Qwen3.5-4B-Q4_K_M.gguf（llama.cpp CUDA） | PASS |

## V1.1.0 验收项
| # | 项目 | 结果 | 证据 |
|---:|---|---|---|
| 1 | APP_VERSION 1.1.0 / Schema 2 | PASS | 关于页、VERSION.txt、窗口标题 |
| 2 | Schema 1 → 2 迁移（真实 V1.0.0 数据目录） | PASS | 记录数不变、快照、integrity ok、FK 0 |
| 3 | 迁移失败回滚 | PASS | 注入失败 → 保持 Schema 1、无新表 |
| 4 | DOCX 内嵌图片 OCR | PASS | EXE：图片 21 / OCR 成功 16（其余为规则过滤） |
| 5 | PPTX 内嵌图片 OCR | PASS | EXE：8 / 7；Slide / Shape / BBox |
| 6 | XLSX 内嵌图片 OCR | PASS | EXE：4 / 3；锚点单元格 |
| 7 | 三语 OCR（中 / 英 / 印尼） | PASS | 图片内三语文字识别并可检索 |
| 8 | 图片过滤（图标 / 极小 / 透明 / 空白 / 不支持格式） | PASS | 过滤原因可见 |
| 9 | SHA-256 去重 + OCR 缓存 | PASS | 6 处出现仅 OCR 1 次，每处独立定位 |
| 10 | 版本变化：未变图片复用、变化图片重识别 | PASS | 历史版本保留旧文字 |
| 11 | 正文无、图片有的唯一标记：FTS / 向量 / RAG | PASS | EXE：FTS 命中、向量命中、RAG 引用「章节 · 第4段附近 · 内嵌图片 4 [内嵌图片OCR]」 |
| 12 | 子处理（重新解析正文 / 重新 OCR 内嵌图片 / 重新 Chunk / 索引 / 向量） | PASS | 仅执行所选阶段 |
| 13 | Office OCR 中安全停止 / 急停 / 续跑 | PASS | 第 6 张停止后从第 7 张继续，无重复 OCR |
| 14 | 解析卡死 / OCR 卡死自动超时跳过 | PASS | EXE：B PARSE_TIMEOUT、D OCR_TIMEOUT，其余完成，COMPLETED_WITH_ERRORS |
| 15 | Worker 卡死 / 崩溃 → Watchdog | PASS | 终止进程树、自动续跑、达到上限后跳过，不无限循环 |
| 16 | 部分成功（单页失败） | PASS | 其余页可检索，失败页不可被引用；人工重试补齐 |
| 17 | 失败清单与人工重试 | PASS | JSON + CSV；按类别重试 |
| 18 | 无孤儿进程 / 无 RUNNING 残留 | PASS | EXE 退出后残留 0；last_shutdown 全 0 |
| 19 | 备份 / 恢复含新表 | PASS | office_images、ocr_cache 恢复；V1 备份可恢复 |
| 20 | **模型下载中心** | PASS | EXE：清单 8 项、硬件推荐（已配置同类资产不重复推荐）、下载前选择位置并显示磁盘空间；真实下载 llama.cpp CPU → SHA-256 → 安装 → 运行验证 |
| 21 | **完整 PP-OCR 运行时下载安装** | PASS | 空目录：嵌入式 Python 3.12.10 + paddlepaddle 3.3.1 + paddleocr 3.7.0，用下载的 PP-OCRv5 模型 OCR 三语置信度 0.992 |
| 22 | 下载可靠性 | PASS | 本地 HTTP 实测：断点续传、SHA-256 不一致拒绝并换源、取消保留 .part、无进展超时、磁盘空间不足拒绝、Zip Slip 拒绝、镜像测速排序 |
| 23 | **扫描电脑** | PASS | EXE 中扫描并连接已有资产；真实全盘（C: + D:，36 万文件夹，194 s）只读扫描找到本机全部已有 AI 资产 |
| 24 | **数据保存在程序文件夹内（便携模式）** | PASS | EXE 首次运行默认“程序文件夹内”：portable.flag + profile\ + ESG-XK-Data\，Profile 保存相对路径；普通启动自动识别 |
| 25 | **数据目录迁移** | PASS | EXE：复制 24 个文件、SHA-256 24 / 24、integrity ok、文档 36 / 36，源目录保留；取消 / 失败自动回退（自动化测试） |
| 26 | 整个程序文件夹移动后仍可用 | PASS | 自动化测试：移动文件夹后相对路径解析到新位置 |
| 27 | 源文件 0 修改 | PASS | V1 基线 39 + V1.1 基线 20 个源文件 SHA-256 不变 |
| 28 | 已有 AI 资产 0 修改 | PASS | 扫描前后文件 mtime / size 不变（自动化测试）；仅引用路径 |
| 29 | 交付包不含用户数据 | PASS | build_release 排除 portable.flag / profile / ESG-XK-Data / AI-Models |
| 30 | V1.0.0 冻结目录未修改 | PASS | 388 / 388 |

## 禁止项核对
- 无计划任务 / 定时 / 夜间处理 / 服务 / 开机自启：源码扫描测试 PASS。
- 无自动扫描、自动 OCR、自动下载：扫描电脑与模型下载都必须由用户点击开始；急停与退出都会取消下载 / 扫描。
- 不上传、无遥测、无账号：仅在“模型下载”中点击开始后访问下载源（只下载）。
- 程序退出后无任何进程继续运行（EXE 验收按 create_time 核对）。

## 尚存限制
1. DOCX 页眉 / 页脚中的图片不做 OCR；Excel “图片放在单元格中”（Place in Cell / richData）不支持；EMF / WMF / SVG 不做 OCR。
2. 备份 / 恢复本身未套用“无进展超时”（大数据目录备份耗时与数据量成正比，可在处理中心外手动操作）。
3. 导入时文件读取卡死会在超时后跳过该文件，但被阻塞的读取线程（守护线程）要到操作系统 I/O 返回后才结束。
4. 模型下载速度取决于网络与镜像；PP-OCR 运行时安装需下载约 0.9 GB 依赖（本机实测约 15 分钟）。下载源由第三方提供，
   所有文件以内置 SHA-256 校验。
5. PP-OCR 运行时仅提供 CPU 版（与 V1.0.0 一致）。
6. 扫描整块磁盘需要几分钟；GGUF 小于 20 MB 的文件不列出。
7. EXE 未进行代码签名，首次运行时 Windows SmartScreen 可能提示。
