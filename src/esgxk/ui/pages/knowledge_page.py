"""知识管理：审核治理、标签、自定义字段、知识条目。"""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QInputDialog, QLineEdit, QTabWidget, QVBoxLayout, QWidget

from esgxk.knowledge.structuring import ESG_TOPICS
from esgxk.parsers.base import locator_text
from esgxk.ui.widgets import (GOV_LABELS, Card, PageBase, autosize, button, confirm, current_key, fill_table, hbox,
                              info, make_table, muted, selected_keys, show_error)


class KnowledgePage(PageBase):
    title = "知识管理"
    description = "知识治理（草稿 → 待审核 → 已批准 → 已失效）、标签、自定义字段与结构化知识条目"

    def __init__(self, main):
        super().__init__(main)
        tabs = QTabWidget()
        # ---- 审核治理
        w = QWidget()
        lay = QVBoxLayout(w)
        self.gov_filter = QComboBox()
        self.gov_filter.addItem("全部", "")
        for k, v in GOV_LABELS.items():
            self.gov_filter.addItem(v, k)
        self.gov_filter.currentIndexChanged.connect(self.refresh_gov)
        lay.addLayout(hbox(self.gov_filter, None,
                           button("提交审核", "", lambda: self._set_gov("PENDING_REVIEW")),
                           button("批准", "Primary", lambda: self._set_gov("APPROVED")),
                           button("退回草稿", "", lambda: self._set_gov("DRAFT")),
                           button("标记失效", "Danger", lambda: self._set_gov("EXPIRED")),
                           button("设置有效期…", "", self._validity)))
        self.t_gov = make_table(["标题", "审核状态", "有效", "生效日期", "失效日期", "批准时间", "处理状态", "版本"], multi=True)
        self.t_gov.doubleClicked.connect(lambda _i: self._detail(self.t_gov))
        lay.addWidget(self.t_gov, 1)
        lay.addWidget(muted("仅“已批准 + 当前有效”的文档默认进入正式 RAG 回答。管理员可在 设置 → 检索与问答 中允许 RAG 使用草稿。"))
        tabs.addTab(w, "审核治理")
        # ---- 标签
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addLayout(hbox(button("新建标签", "Primary", self._new_tag), button("重命名 / 合并", "", self._rename_tag),
                           button("删除标签", "Danger", self._delete_tag), None))
        self.t_tags = make_table(["标签", "文档数", "创建时间"], stretch_col=0)
        lay.addWidget(self.t_tags, 1)
        tabs.addTab(w, "标签管理")
        # ---- 自定义字段
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addLayout(hbox(button("新增字段", "Primary", self._new_field), button("删除字段", "Danger", self._del_field), None))
        self.t_fields = make_table(["字段名", "类型", "已填写文档数", "创建时间"], stretch_col=0)
        lay.addWidget(self.t_fields, 1)
        lay.addWidget(muted("内置字段：业务领域、部门、文档类型、项目、国家/区域、年份、版本、状态、备注。自定义字段在文档详情中填写，"
                            "并参与元数据检索。"))
        tabs.addTab(w, "自定义字段")
        # ---- 知识条目
        w = QWidget()
        lay = QVBoxLayout(w)
        self.topic = QComboBox()
        self.topic.addItem("全部 ESG 主题", "")
        for t in ESG_TOPICS:
            self.topic.addItem(t, t)
        self.topic.currentIndexChanged.connect(self.refresh_items)
        self.kq = QLineEdit()
        self.kq.setPlaceholderText("搜索标题 / 摘要 / 关键词")
        self.kq.returnPressed.connect(self.refresh_items)
        lay.addLayout(hbox(self.topic, self.kq, button("查询", "", self.refresh_items)))
        self.t_items = make_table(["标题", "ESG主题", "类型", "摘要", "关键词", "文档", "版本", "审核", "定位"])
        self.t_items.doubleClicked.connect(self._open_item)
        lay.addWidget(self.t_items, 1)
        lay.addWidget(muted("知识条目由“本地知识标准”Pipeline 的知识结构化阶段生成：按章节/表格/幻灯片聚合，摘要取自原文首句，"
                            "ESG 主题由三语词典判定；不调用大模型，每条都可追溯到 Chunk 与原文定位。"))
        tabs.addTab(w, "知识条目")
        card = Card()
        card.lay.addWidget(tabs)
        self.root.addWidget(card, 1)

    def refresh(self) -> None:
        self.refresh_gov()
        self.refresh_tags()
        self.refresh_fields()
        self.refresh_items()

    def refresh_gov(self) -> None:
        rows = self.ctx.docs.list(governance=self.gov_filter.currentData() or None)
        fill_table(self.t_gov, [[r["title"], r["governance_status"], "是" if r["is_valid"] else "否",
                                 r["effective_from"] or "", r["effective_to"] or "", (r["approved_at"] or "")[:16],
                                 r["process_status"], f"v{r['version_no']}"] for r in rows],
                   [r["document_id"] for r in rows], status_cols=[1, 6])
        autosize(self.t_gov, 320)

    def refresh_tags(self) -> None:
        tags = self.ctx.docs.tags()
        fill_table(self.t_tags, [[t["name"], t["doc_count"], t["created_at"][:16]] for t in tags], [t["name"] for t in tags])

    def refresh_fields(self) -> None:
        fs = [dict(r) for r in self.ctx.conn.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM document_field_values v WHERE v.field_id=f.field_id) n FROM custom_fields f")]
        fill_table(self.t_fields, [[f["name"], f["field_type"], f["n"], f["created_at"][:16]] for f in fs],
                   [f["field_id"] for f in fs])

    def refresh_items(self) -> None:
        items = self.ctx.docs.knowledge(topic=self.topic.currentData() or None, text=self.kq.text().strip())
        fill_table(self.t_items, [[k["title"], k["esg_topic"] or "", k["kind"], k["summary"], k["keywords"] or "",
                                   k["document_title"], f"v{k['version_no']}", k["governance_status"],
                                   locator_text(k["locator"])] for k in items],
                   [k["chunk_id"] for k in items], status_cols=[7])
        autosize(self.t_items, 300)

    # ---- 动作
    def _set_gov(self, status: str) -> None:
        ids = [k for k in selected_keys(self.t_gov) if k]
        if not ids:
            info(self, "提示", "请选择文档（可多选）")
            return
        self.ctx.docs.set_governance(ids, status)
        self.refresh_gov()

    def _validity(self) -> None:
        ids = [k for k in selected_keys(self.t_gov) if k]
        if not ids:
            info(self, "提示", "请选择文档")
            return
        from esgxk.ui.dialogs.validity import ValidityDialog
        d = self.ctx.docs.get(ids[0])
        dlg = ValidityDialog(self, is_valid=bool(d["is_valid"]), eff_from=d["effective_from"], eff_to=d["effective_to"])
        if dlg.exec():
            v = dlg.values()
            self.ctx.docs.set_validity(ids, v["is_valid"], v["effective_from"], v["effective_to"], set_dates=True)
            self.refresh_gov()

    def _detail(self, table) -> None:
        did = current_key(table)
        if did:
            from esgxk.ui.dialogs.document_detail import DocumentDetailDialog
            DocumentDetailDialog(self.main, did).exec()
            self.refresh()

    def _new_tag(self) -> None:
        name, ok = QInputDialog.getText(self, "新建标签", "标签名：")
        if ok and name.strip():
            try:
                self.ctx.docs._tag_id(name.strip())
            except Exception as exc:
                show_error(self, exc)
            self.refresh_tags()

    def _rename_tag(self) -> None:
        old = current_key(self.t_tags)
        if not old:
            return
        new, ok = QInputDialog.getText(self, "重命名标签", f"将「{old}」改为（若与已有标签同名则合并）：", text=old)
        if ok and new.strip() and new.strip() != old:
            try:
                self.ctx.docs.rename_tag(old, new.strip())
            except Exception as exc:
                show_error(self, exc)
            self.refresh_tags()

    def _delete_tag(self) -> None:
        name = current_key(self.t_tags)
        if name and confirm(self, "删除标签", f"删除标签「{name}」？（仅移除标签关联，不影响文档）", danger=True):
            self.ctx.docs.delete_tag(name)
            self.refresh_tags()

    def _new_field(self) -> None:
        name, ok = QInputDialog.getText(self, "新增自定义字段", "字段名：")
        if ok and name.strip():
            typ, ok2 = QInputDialog.getItem(self, "字段类型", "类型：", ["TEXT", "NUMBER", "DATE"], 0, False)
            if ok2:
                try:
                    self.ctx.docs.add_custom_field(name.strip(), typ)
                except Exception as exc:
                    show_error(self, exc)
                self.refresh_fields()

    def _del_field(self) -> None:
        fid = current_key(self.t_fields)
        if fid and confirm(self, "删除字段", "删除该自定义字段及其所有已填写的值？", danger=True):
            self.ctx.docs.delete_custom_field(fid)
            self.refresh_fields()

    def _open_item(self) -> None:
        cid = current_key(self.t_items)
        if cid:
            from esgxk.ui.dialogs.source_viewer import SourceViewer
            SourceViewer(self.main, cid).exec()
