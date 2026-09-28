# ESG-XK V1.0.0 — 本地 ESG 文档知识库管理系统

ESG-XK 是一款在 Windows 本机运行的 ESG 文档知识库：导入与管理 PDF / Word / Excel / PowerPoint / 文本 / 图片文档，
解析与 OCR（中文 / English / Bahasa Indonesia），全文检索、元数据检索、跨语言语义检索，以及带引用的本地 AI 问答。

## 快速开始
1. 双击 `ESG-XK.exe`（无需安装、无需 IDE、无需 Python）。
2. 首次运行选择数据目录（默认 `..\ESG-XK-Data`，即与便携目录同级）。
3. 左侧“导入中心”→ 导入文件或文件夹（源文件只读，不会被修改）。
4. “处理中心”→ 选择文档与 Pipeline →【开始处理】。
5. “全文搜索 / 知识检索 / AI问答”使用知识库。

## 核心原则
- **源文件只读**：不修改、不覆盖、不删除、不重命名任何源文件；“托管副本”仅在您选择时复制。
- **无定时处理**：没有计划任务、夜间处理或后台服务；所有处理都由您点击开始，程序退出后不会继续处理。
- **可停可续**：暂停 / 继续 / 安全停止（保存 Checkpoint）/ 急停，均可从 Checkpoint 续跑。
- **可追溯**：检索结果与 AI 回答都带文档、版本、页码 / Sheet / Slide / 段落定位与原文片段；无依据时显示“知识库中未找到足够依据”。
- **本地优先**：不上传文件、不联网、无遥测、无账号。AI 资产（OCR、Embedding、Reranker、LLM）为可选增强，未配置时其余功能照常使用。

## 目录说明
| 文件 | 说明 |
|---|---|
| ESG-XK.exe | 主程序（同一 EXE 也作为处理 Worker 由程序自身按需启动） |
| _internal\ | 运行时组件（请勿修改） |
| config\profile.template.json | 本机 Profile 模板（说明字段含义；实际 Profile 位于 %LOCALAPPDATA%\ESG-XK\profiles） |
| USER_GUIDE_CN.md / ADMIN_GUIDE_CN.md | 用户说明 / 管理员说明 |
| RELEASE_NOTES.md / VERSION.txt | 版本说明 |
| MANIFEST.json / SHA256SUMS.txt | 交付清单与校验值 |
| LICENSES\ | 第三方组件许可证 |

系统要求：Windows 10/11 x64；建议 16 GB 内存；AI 功能需要本机已有的 llama.cpp 与 GGUF 模型、PP-OCR 运行时（见管理员说明）。
