# ESG-XK V1.1.0 最终测试报告

测试日期：2026-09-28　·　测试机：Windows 11 Home China 10.0.26200，31 GB 内存，RTX 5060 Laptop 8 GB
测试语料：D:\ESG-XK\ceshi（V1 基线 39 个文件 + V1.1 基线 20 个文件 + V1.0.0 真实数据目录样本）
全部测试使用真实组件：真实 Worker / Parse Helper 子进程、真实 PP-OCRv6、真实 llama.cpp（Qwen3 Embedding / Reranker /
Qwen3.5-4B）；可靠性测试中的卡死 / 崩溃为真实发生（通过 ESGXK_FAULT_INJECT 仅在测试中启用）。唯一的桩对象是
test_rotation_retry_selection_unit（单元测试，仅验证旋转重试的选择逻辑，已在测试名与文档字符串中注明）。

## 1. 自动化测试（最终回归，基于最终代码）
命令：`pytest -q -s --junitxml=reports/junit_v1.1.0_final.xml`
结果：**122 个测试，122 PASS，0 FAIL，0 ERROR，0 SKIP**，耗时 1750 s。

| 模块 | 数量 | 结果 | 说明 |
|---|---:|---|---|
| test_unit_core | 8 | PASS | V1.0.0 回归 |
| test_db_migration_profile | 10 | PASS | V1.0.0 回归（迁移注册表已含 v2） |
| test_import_parse | 16 | PASS | V1.0.0 回归 |
| test_pipeline_ai | 14 | PASS | V1.0.0 回归；Run 最终状态断言按 V1.1 语义改为 COMPLETED_WITH_ERRORS |
| test_run_control | 8 | PASS | V1.0.0 回归 7 项；“Worker 崩溃”按 V1.1 语义改为 Watchdog 自动续跑，另新增兜底路径测试 1 项 |
| test_backup_resource_ui | 9 | PASS | V1.0.0 回归 |
| test_v11_office_ocr | 21 | PASS | 新增：Office 内嵌图片 OCR 全链路 |
| test_v11_reliability | 12 | PASS | 新增：超时 / Watchdog / 自动跳过（含导入哈希卡死） |
| test_v11_migration_backup | 4 | PASS | 新增：真实 V1 数据目录升级、回滚、旧备份恢复、V1.1 备份恢复 |
| test_v11_models_portable | 18 | PASS | 新增：模型清单、下载器（本机 HTTP 实测）、镜像测速、扫描电脑、便携模式、数据目录迁移、界面 |
| test_zz_source_integrity | 2 | PASS | V1 基线 39 个 + V1.1 基线 20 个源文件 size / mtime / SHA-256 零变化 |

新增测试 57 项；V1.0.0 的 65 项测试全部重新执行（其中 2 项因 V1.1 需求带来的行为变化更新了断言，
旧行为的兜底路径由新增测试覆盖）；合计 122 项。

## 2. Office 内嵌图片 OCR 证据
- DOCX 主测试文档：19 个图片出现位置（内嵌 / 浮动 anchor / VML / 表格单元格 / JPEG / BMP / TIFF）；
  5 个非知识图片按规则过滤（ICON、TOO_SMALL、TRANSPARENT、TINY_PIXEL、BLANK），14 个 OCR 成功，失败 0。
- PPTX：8 张（3 张幻灯片、组合形状、侧向 90° 截图、小图标过滤）；保存 Slide、Shape ID、Bounding Box、旋转角度。
- XLSX：锚点 B4 / D5 / A12 / C2（图标过滤），多工作表。
- 三语：中文“职业健康安全管理制度…8小时”、英文“Scope 3 … 45,600 tCO2e”、印尼语“Target pengurangan emisi 30 persen”。
- 侧向截图“旋转截图：应急演练每半年开展一次”被正确识别（PP-OCR 文本行方向处理，本例未触发旋转重试；
  旋转重试选择逻辑由单元测试覆盖）。
- “正文无关键词、图片有关键词”：ESG-XK-OCR-UNIQUE-TEST（DOCX）、ESG-XK-PPTX-OCR-UNIQUE、ESG-XK-XLSX-OCR-UNIQUE
  正文中均不存在，FTS 均命中对应文件且来源为 OFFICE_IMAGE_OCR；“职业健康”在 DOCX 中仅来自图片。
- 向量检索：中文查询“外购商品带来的范围三排放是多少”命中英文图片 OCR Chunk（跨语言）。
- RAG：“哪个文件包含 ESG-XK-OCR-UNIQUE-TEST？”→ ANSWERED，引用《职业安全环境手册_图片OCR》
  章节「2 专项检查」· 第4段附近 · 内嵌图片 4 [内嵌图片OCR]；PPTX 同理引用“第3张幻灯片 · 内嵌图片 1”。
- 缓存：同一图片出现 6 处（主文档 3 处 + 文档A + 文档B + PPTX），OCR 计算 1 次，其余 5 处缓存复用，
  每处独立 Locator 与独立 Chunk；ocr_cache.hit_count = 5。
- 版本：v1 → v2 修改其中一张图片：未变化图片复用缓存（cache_hit=1），变化图片重新 OCR（“200 万吨”），
  历史版本仍保留“100 万吨”，检索只返回当前版本。
- 子处理：“重新OCR内嵌图片”仅新增 3 条 OCR 结果、未重新解析正文。
- 安全停止：24 张图片的 PPTX 在第 6 张后安全停止（Checkpoint unit_no=6），继续后从第 7 张开始，最终 ocr_results = 24（无重复 OCR）。
- 急停：Office 图片 OCR 中急停 372 ms，临时图片清理，无半写入，重新打开后续跑至 24/24。

## 2A. 模型下载中心 / 扫描电脑 / 便携模式（真实验证）
**真实下载安装**（模拟没有任何 AI 环境的新电脑：空目录 D:\ESG-XK\ceshi\work\download_test）：
| 组件 | 来源 | 结果 |
|---|---|---|
| OCR 模型 PP-OCRv5 mobile det / rec + textline_ori（12 个文件） | ModelScope | 全部 SHA-256 一致，安装完成 |
| llama.cpp b11228 CPU | GitHub | SHA-256 一致，`llama-server --version` 验证通过 |
| Qwen3-Embedding-0.6B-Q8_0（639 MB） | ModelScope | SHA-256 一致；用下载的 llama-server CPU 实际嵌入：维度 1024，zh-en 相似度 0.834，zh-id 0.768 |
| PP-OCR 运行时 | npmmirror（Python 3.12.10 embed）、清华（pip wheel）、PyPI 镜像（paddlepaddle 3.3.1、paddleocr 3.7.0 及依赖） | 安装完成；用下载的运行时 + 下载的 PP-OCRv5 模型真实 OCR：“下载的PP-OCR运行时：职业健康安全 / Downloaded runtime works 2025 / Pengelolaan limbah B3 sesuai aturan”，置信度 0.992；pip 缓存与临时目录清理干净 |

下载器（本机 127.0.0.1 HTTP 服务器实测）：正常下载 + 校验；断点续传（验证 Range 起点 = 已下载字节）；来源不支持 Range 时从头下载；
SHA-256 不一致丢弃并换源；全部来源不一致则失败且不留文件；取消后保留 .part；数据停滞 2 秒无进展超时；单项失败不影响其他项；
安装清单 ESGXK-MODELS.json；本地文件安装仅在 SHA-256 匹配时进行；Zip Slip 拒绝；磁盘空间不足拒绝；PyPI 镜像按实测速度排序。

**扫描电脑**：真实全盘只读扫描 C: + D:（361,181 个文件夹，194 s），找到本机已有 llama-server（CPU / CUDA）、Qwen3 Embedding、
Qwen3 Reranker、Qwen3-VL LLM、PP-OCRv6 模型目录、多个 PP-OCR 运行时以及本次下载的资产；扫描前后文件 mtime / size 不变（自动化测试）；
扫描可取消。

**便携模式**：冻结 EXE 首次运行默认选择“程序文件夹内”：生成 portable.flag、profile\default.json（data_root 保存为相对路径
“ESG-XK-Data”）；程序文件夹整体移动后路径仍然解析正确（自动化测试）；普通启动（无参数）通过 portable.flag 自动找到配置。
数据目录迁移：EXE 内 24 个文件逐个 SHA-256 一致、integrity ok、文档 36 / 36、源目录保留；取消时清理目标并回到原目录；
目标非空 / 目标位于源目录内均被拒绝。

## 3. 可靠性证据（真实故障）
| 场景 | 结果 |
|---|---|
| A 正常 / B 解析永久阻塞 / C 正常 / D OCR 永久阻塞 / E 正常 | A、C、E DONE；B TIMEOUT（PARSE_TIMEOUT）；D TIMEOUT（OCR_TIMEOUT，尝试 2 次）；Run = COMPLETED_WITH_ERRORS；失败清单 JSON + CSV；无孤儿进程 |
| 3 页扫描 PDF 第 2 页 OCR 永久阻塞 | PARTIAL_SUCCESS：第 1、3 页可检索，第 2 页不产生 Chunk / 不可被引用；人工“重试失败项（仅OCR失败）”后第 2 页补齐 |
| OCR 子进程崩溃 | 重启重试后仍崩溃 → FAILED（OCR_FAILED），后续文件正常 |
| Embedding 调用无响应 | 批次超时 → 重启重试 → TIMEOUT，文档 PARTIAL，全文检索不受影响 |
| Worker 主线程卡死 | Watchdog 判定 STALLED → 终止进程树 → 自动续跑 → 第二次仍卡死 → TIMEOUT(STALLED) 跳过；A、E 完成；stall_restarts=2 |
| 安全停止时 Worker 无响应 | 超过宽限时间升级为受控强制终止（RUN_STOP_TIMEOUT），无孤儿进程 |
| 加密 PDF / 损坏 Office / 0 字节 / 无读取权限 | 快速识别为 ENCRYPTED / CORRUPTED / EMPTY / ACCESS_DENIED，不重试，正常文件继续 |
| 连续多个失败文件 | 后续正常文件全部完成 |
| RESOURCE_DENIED | 等待宽限（测试 4 秒）后记录 RESOURCE_DENIED 并跳过，其余文件继续 |
| Worker 崩溃 | Watchdog 自动续跑至完成（stall_restarts=1） |
| 程序在同一文件上反复异常退出 | 启动恢复计入卡死次数，达到上限后该文件 TIMEOUT(STALLED) 跳过，不会无限循环 |
| 重启后继续失败的 Run | 已达上限的失败项保持跳过，不重新处理；仅“重试失败项”人工触发才处理 |

## 4. V1.0.0 回归证据（节选，最终回归）
- 安全停止（真实 OCR，18 页扫描件）：3/18 页处停止 → 续跑 → 18/18，ocr_results = 18。
- 急停（真实 OCR）：198 ms，终止 Worker 进程树含 PARSE 与 OCR Helper，残留 0，integrity ok，重启后续跑完成。
- 全部 V1.0.0 功能（导入、去重、版本、解析、OCR、FTS、向量、Hybrid、RAG、治理、备份恢复、资源门禁、UI）PASS。

## 5. 迁移验证（真实 V1.0.0 数据目录）
样本：V1.0.0 正式 EXE 验收生成的数据目录（Schema 1：28 文档、20 OCR、57 Chunk / 向量、49 知识条目、1 Run / 28 项 /
188 Checkpoint、2 问答、2 个 V1 备份包）。
- 升级：Schema 1 → 2 PASS，迁移前快照生成；全部表记录数不变；历史 Chunk 来源类型正确回填；integrity ok / FK 0；检索可用。
- 回滚：注入失败的 v2 迁移 → DB_MIGRATION_FAILED，数据库保持 Schema 1、无新表、记录数不变、integrity ok；修复后再次迁移成功。
- V1 备份（Schema 1）在 V1.1.0 中恢复成功（临时副本上迁移至 Schema 2），integrity ok。
- V1.1 备份 → 删除 Office OCR 记录 → 恢复：office_images、ocr_cache 全部恢复，图片关键词可再次检索。

## 6. EXE 真实验收（最终构建）
EXE：`D:\ESG-XK\jiaofu\ESG-XK-1.1.0-portable\ESG-XK.exe`
SHA-256：3ed6a3cdeeffd98ae568fd43563ece4a89071c447f125da39fac2960c23f61e7
- `--acceptance`（冻结 EXE 内驱动真实界面）：**33 步 33 PASS / 0 FAIL**，469 s。首次运行由 EXE 自身处理（默认“程序文件夹内”）；
  含 DOCX / PPTX / XLSX 内嵌图片 OCR（DOCX 16/21、PPTX 7/8、XLSX 3/4 OCR 成功，其余为按规则过滤的非知识图片）、
  FTS / 向量命中内嵌图片、RAG 引用内嵌图片位置、暂停 / 继续 / 安全停止 / Checkpoint 续跑 / 急停（123 ms）、
  解析卡死与 OCR 卡死超时跳过（COMPLETED_WITH_ERRORS）、备份 / 恢复、文档详情内嵌图片页签、日志、AI资产中心、
  扫描电脑、模型下载中心（硬件推荐 + 真实下载安装 llama.cpp CPU）、数据目录迁移（SHA-256 24/24）。
  退出码 0；退出后残留子进程 0（按 create_time 校验）；last_shutdown：Worker 0 / Helper 0 / 端口 0 / 处理租约释放 / 数据目录租约释放。
- 普通启动（无参数，便携模式）：2.5 s 出窗「ESG-XK 本地ESG文档知识库 V1.1.0」，读取迁移后的数据目录，空闲无子进程；
  窗口关闭消息后退出码 0，退出核对全部为 0。
- 界面为最终 Aurora Glass 主题（蓝 → 青 → 绿渐变、通透玻璃层次），上述 EXE 验收与全量回归均在该主题构建上执行。

## 7. 数据库与源文件
- 所有完整性校验以新连接执行：PRAGMA integrity_check = ok；foreign_key_check 无错误。
- 源文件：V1 基线 39 个 + V1.1 基线 20 个（DOCX / PPTX / XLSX / 可靠性语料）size / mtime / SHA-256 全部不变。
- V1.0.0 冻结交付目录：388 个文件与其 SHA256SUMS 全部一致（未被修改）。

## 8. 开发期发现并修复的缺陷（V1.1.0）
| 等级 | 缺陷 | 处理 |
|---|---|---|
| P1 | 冻结 EXE 中 Parse Helper 以系统 ANSI 代码页读取 stdin，中文路径请求被误解码 → 源文件“缺失” | 子进程协议改为字节读取 + 显式 UTF-8；客户端发送 ASCII 转义 JSON；冻结 EXE 实测通过 |
| P2 | OCR Helper 超时重启加载模型期间不上报进展，Watchdog 阈值较小时会误判 Worker 卡死 | Helper 启动等待期间上报进展（受 timeout.helper_start 约束）；Watchdog 有效阈值不低于最大单元超时 + 30 秒 |
| P2 | 恢复备份时 Watchdog 线程持有数据库连接导致无法替换数据库文件 | 恢复前停止并等待 Watchdog |
| P3 | 启动恢复后中断 Run 保留过期阶段信息 | 恢复时清除 current_stage / progress_label |
| P3 | OCR 全部超时时阶段事件错误码显示 OCR_FAILED | 按失败性质记录 OCR_TIMEOUT / RESOURCE_WAIT_TIMEOUT / OCR_FAILED |

| P1 | 真实下载测试发现：嵌入式 Python 中用 “wheel\pip” 路径安装 pip 被 pip 拒绝（Windows 保护），PP-OCR 运行时安装失败 | 改为 runpy 启动 pip；空目录真实重装通过 |
| P2 | 真实网络下清华镜像 paddle 包仅约 19 KB/s，安装可能极慢 | 新增阿里云镜像；安装前按实测速度排序镜像；pip --retries 10 |
| P3 | pip 临时下载文件写入 C 盘 TEMP，且不计入“无进展”判断 | TMP / TEMP 指向安装目录内 .pip-tmp，完成后删除 |
| P3 | 自动化测试隔离问题：页面模块按名称导入 hardware，单独补丁无效（仅影响测试） | 同时补丁页面模块；按全量顺序复测通过 |

当前未关闭缺陷：P0 = 0，P1 = 0，P2 = 0；P3 见验收报告“尚存限制”。
