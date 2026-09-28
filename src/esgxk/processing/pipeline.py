"""Pipeline 与 Stage 定义。"""
from __future__ import annotations

PIPELINES: dict[str, dict] = {
    "SEARCHABLE_CORPUS": {
        "label": "可检索语料",
        "stages": ["PARSE", "OCR", "CHUNK", "INDEX", "EMBED"],
        "description": "解析、OCR、结构化切分、全文索引、元数据检索；Embedding 可用时建立基本向量索引。",
    },
    "LOCAL_KNOWLEDGE_STANDARD": {
        "label": "本地知识标准",
        "stages": ["PARSE", "OCR", "CHUNK", "INDEX", "EMBED", "KNOWLEDGE"],
        "description": "在可检索语料基础上增加语义索引、知识结构化与知识治理字段，支持 Reranking 与 RAG。",
    },
}

STAGE_LABELS = {
    "PARSE": "解析", "OCR": "OCR", "CHUNK": "切分", "INDEX": "全文索引", "EMBED": "向量化", "KNOWLEDGE": "知识结构化",
}

# 阶段分组：同一分组内的阶段按文档依次执行；分组之间切换重型模型（OCR → Embedding），
# 保证同一时刻只驻留一个重型模型（LOW_MEMORY_16G）。
PHASES: list[list[str]] = [["PARSE", "OCR", "CHUNK", "INDEX"], ["EMBED"], ["KNOWLEDGE"]]

RUN_STATUS_LABELS = {
    "CREATED": "已创建", "RUNNING": "运行中", "PAUSED": "已暂停", "STOP_REQUESTED": "正在安全停止",
    "SAFE_STOPPED": "已安全停止", "EMERGENCY_STOPPED": "已急停", "COMPLETED": "已完成", "FAILED": "失败",
    "INTERRUPTED": "已中断", "COMPLETED_WITH_ERRORS": "已完成（有错误）",
}
FINISHED_STATUSES = ("COMPLETED", "COMPLETED_WITH_ERRORS")
ACTIVE_STATUSES = ("RUNNING", "PAUSED", "STOP_REQUESTED")
RESUMABLE_STATUSES = ("CREATED", "SAFE_STOPPED", "EMERGENCY_STOPPED", "INTERRUPTED", "FAILED")

STATUS_LABELS = {
    "NOT_STARTED": "未开始", "PENDING": "待处理", "RUNNING": "处理中", "DONE": "完成", "FAILED": "失败",
    "SKIPPED": "跳过", "NOT_REQUIRED": "不需要", "LOW_CONFIDENCE": "低置信度", "ASSET_UNAVAILABLE": "资产不可用",
    "PARTIAL": "部分成功", "PROCESSING": "处理中", "RESOURCE_DENIED": "资源门禁拒绝", "TIMEOUT": "超时",
    "STALLED": "卡死", "PENDING_REVIEW": "待审核",
}
SOURCE_TYPE_LABELS = {
    "BODY": "正文", "TABLE": "表格", "NOTES": "备注", "SCANNED_OCR": "扫描页OCR", "IMAGE_OCR": "图片OCR",
    "OFFICE_IMAGE_OCR": "内嵌图片OCR",
}
OFFICE_STAGE_LABELS = {
    "PARSE": "解析Office正文 / 提取内嵌图片", "OCR": "Office图片OCR", "CHUNK": "Chunk", "INDEX": "FTS 索引",
    "EMBED": "Embedding", "KNOWLEDGE": "知识结构化",
}


def stages_for(pipeline: str, start_stage: str | None = None, only: list[str] | None = None) -> list[str]:
    stages = list(PIPELINES[pipeline]["stages"])
    if only:
        return [s for s in stages if s in only]
    if start_stage:
        if start_stage not in stages:
            raise ValueError(start_stage)
        stages = stages[stages.index(start_stage):]
    return stages


def phase_of(stage: str) -> int:
    for i, p in enumerate(PHASES):
        if stage in p:
            return i
    raise ValueError(stage)
