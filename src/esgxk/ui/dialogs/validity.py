"""有效性 / 有效期设置对话框。"""
from __future__ import annotations

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout


class ValidityDialog(QDialog):
    def __init__(self, parent=None, *, is_valid: bool = True, eff_from: str | None = None, eff_to: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("设置有效性与有效期")
        lay = QFormLayout(self)
        self.valid = QCheckBox("文档有效")
        self.valid.setChecked(is_valid)
        self.use_from = QCheckBox("设置生效日期")
        self.d_from = QDateEdit(QDate.fromString(eff_from, "yyyy-MM-dd") if eff_from else QDate.currentDate())
        self.use_to = QCheckBox("设置失效日期")
        self.d_to = QDateEdit(QDate.fromString(eff_to, "yyyy-MM-dd") if eff_to else QDate.currentDate().addYears(1))
        for d in (self.d_from, self.d_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        self.use_from.setChecked(bool(eff_from))
        self.use_to.setChecked(bool(eff_to))
        lay.addRow(self.valid)
        lay.addRow(self.use_from, self.d_from)
        lay.addRow(self.use_to, self.d_to)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def values(self) -> dict:
        return {
            "is_valid": self.valid.isChecked(),
            "effective_from": self.d_from.date().toString("yyyy-MM-dd") if self.use_from.isChecked() else None,
            "effective_to": self.d_to.date().toString("yyyy-MM-dd") if self.use_to.isChecked() else None,
        }
