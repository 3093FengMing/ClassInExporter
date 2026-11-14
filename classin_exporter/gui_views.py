"""Qt painting and semantic styling for the course resource workspace.

Delegates retain native Qt item, selection and checkbox behavior. They only
paint the name/context hierarchy and readable status within the existing model.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QEvent, QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPaintEvent, QPalette, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QLabel, QLineEdit, QSpinBox, QStyle,
    QStyleFactory, QStyledItemDelegate, QStyleOption, QStyleOptionFrame,
    QStyleOptionSpinBox, QStyleOptionViewItem, QWidget,
)


TOKENS = {
    "surface": "#FFFFFF",
    "sidebar": "#EAF3F1",
    "selected": "#DDF1EC",
    "hover": "#F1F8F6",
    "text": "#193730",
    "secondary": "#4F6760",
    "accent": "#146B61",
    "accent_hover": "#11665E",
    "line": "#D7E4DE",
    "header": "#F4F8F6",
    "warning_bg": "#FFF6E9",
    "warning": "#8B531A",
    "error": "#AE3439",
    "disabled": "#80918C",
}


class ExportPathEdit(QLineEdit):
    """Show an unambiguous elided path when idle; edit the complete value."""

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.hasFocus():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_PanelLineEdit,
                                   option, painter, self)
        area = self.style().subElementRect(QStyle.SubElement.SE_LineEditContents, option, self)
        margins = self.textMargins()
        area = area.adjusted(margins.left() + 1, margins.top(), -margins.right() - 1, -margins.bottom())
        display = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, area.width())
        painter.setFont(self.font())
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        painter.setPen(self.palette().color(group, QPalette.ColorRole.Text))
        painter.drawText(area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, display)


class ThreadSpinBox(QSpinBox):
    """Retain native stepping and add legible standard Qt arrow primitives."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        arrow_style = QStyleFactory.create("Fusion")
        if arrow_style is None:
            raise RuntimeError("Qt Fusion style is unavailable")
        self._arrow_style = arrow_style
        self._arrow_style.setParent(self)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        for subcontrol, primitive, step in (
            (QStyle.SubControl.SC_SpinBoxUp, QStyle.PrimitiveElement.PE_IndicatorArrowUp,
             QAbstractSpinBox.StepEnabledFlag.StepUpEnabled),
            (QStyle.SubControl.SC_SpinBoxDown, QStyle.PrimitiveElement.PE_IndicatorArrowDown,
             QAbstractSpinBox.StepEnabledFlag.StepDownEnabled),
        ):
            button = self.style().subControlRect(QStyle.ComplexControl.CC_SpinBox,
                                                option, subcontrol, self)
            arrow = QStyleOption()
            arrow.rect = QRect(button.center().x() - 6, button.center().y() - 5, 12, 10)
            arrow.palette = option.palette
            arrow.state = option.state
            if not option.stepEnabled & step:
                arrow.state &= ~QStyle.StateFlag.State_Enabled
                palette = QPalette(option.palette)
                palette.setCurrentColorGroup(QPalette.ColorGroup.Disabled)
                arrow.palette = palette
            self._arrow_style.drawPrimitive(primitive, arrow, painter, self)


class BoundedDetailLabel(QLabel):
    """Keep complete accessible text, and elide a tall preview at two lines.

    The tooltip and resource dialog expose the full value. A preview that is
    already short uses QLabel's native painting and text selection unchanged.
    """

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.heightForWidth(self.width()) <= self.height() or self.hasSelectedText():
            super().paintEvent(event)
            return
        area = self.contentsRect()
        metrics = self.fontMetrics()
        line_height = metrics.height()
        layout = QTextLayout(self.text(), self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        first_line = layout.createLine()
        first_line.setLineWidth(area.width())
        count = first_line.textLength()
        layout.endLayout()
        first = self.text()[:count].rstrip()
        remainder = self.text()[count:].replace("\n", " ")
        last = metrics.elidedText(remainder, Qt.TextElideMode.ElideMiddle, area.width())
        top = area.y() + max(0, (area.height() - line_height * 2) // 2)
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        painter.drawText(QRect(area.x(), top, area.width(), line_height),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, first)
        painter.drawText(QRect(area.x(), top + line_height, area.width(), line_height),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, last)


def workspace_stylesheet() -> str:
    return """
        QWidget { background: %(surface)s; color: %(text)s; font-size: 14px; }
        QWidget#sidebar, QWidget#sidebar QLabel, QWidget#sidebar QListWidget {
            background: %(sidebar)s;
        }
        QWidget#topbar { border-bottom: 1px solid %(line)s; }
        QWidget#exportbar { border-top: 1px solid %(line)s; }
        QLabel { border: none; }
        QLabel[role="title"] { font-size: 22px; font-weight: 700; }
        QLabel[role="pageTitle"] { font-size: 20px; font-weight: 700; }
        QLabel[role="secondary"] { color: %(secondary)s; font-size: 13px; }
        QLabel[role="strong"] { font-weight: 700; }
        QLabel#sessionStatus { color: %(accent)s; font-size: 13px; }
        QLabel#detailName { font-weight: 700; }
        QLabel#selectionCount { font-weight: 700; }
        QLineEdit, QSpinBox { border: 1px solid %(line)s; border-radius: 5px;
            padding: 7px 12px; background: %(surface)s; selection-background-color: %(selected)s;
            selection-color: %(text)s; min-height: 20px; }
        QLineEdit:focus, QSpinBox:focus { border-color: %(accent)s; }
        QLineEdit:disabled, QSpinBox:disabled { color: %(disabled)s; background: %(header)s; }
        QSpinBox { padding-right: 28px; min-width: 42px; }
        QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right;
            width: 22px; height: 18px; border-left: 1px solid %(line)s;
            border-bottom: 1px solid %(line)s; background: %(header)s;
            border-top-right-radius: 4px; }
        QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right;
            width: 22px; height: 18px; border-left: 1px solid %(line)s;
            background: %(header)s; border-bottom-right-radius: 4px; }
        QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: %(selected)s; }
        QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background: %(line)s; }
        QLineEdit#outputDir { border: 1px solid transparent; padding: 2px 4px;
            color: %(secondary)s; font-size: 13px; }
        QLineEdit#outputDir:hover { border-color: %(line)s; }
        QLineEdit#outputDir:focus { border-color: %(accent)s; color: %(text)s; }
        QPushButton { background: %(surface)s; border: 1px solid %(line)s;
            border-radius: 5px; padding: 7px 16px; min-height: 20px; }
        QPushButton:hover { background: %(hover)s; border-color: %(accent)s; }
        QPushButton:pressed { background: %(selected)s; border-color: %(accent)s; }
        QPushButton:focus { border-color: %(accent)s; background: %(hover)s; }
        QPushButton[primary="true"] { background: %(accent)s; border-color: %(accent)s;
            color: white; font-weight: 600; }
        QPushButton[primary="true"]:hover { background: %(accent_hover)s; }
        QPushButton[primary="true"]:pressed { background: #0D554E; }
        QPushButton[primary="true"]:focus { border: 2px solid %(text)s; padding: 6px 15px; }
        QPushButton:disabled { background: %(header)s; color: %(disabled)s; border-color: %(line)s; }
        QPushButton[quiet="true"] { border: 1px solid transparent; padding: 5px 10px;
            color: %(accent)s; background: transparent; font-size: 13px; }
        QPushButton[quiet="true"]:hover, QPushButton[quiet="true"]:focus {
            border-color: %(accent)s; background: %(hover)s; }
        QPushButton[quiet="true"]:pressed { background: %(selected)s; }
        QListWidget { border: none; outline: none; background: %(sidebar)s; }
        QTreeWidget { border: none; background: %(surface)s; outline: none;
            alternate-background-color: %(surface)s; selection-background-color: %(selected)s;
            selection-color: %(text)s; }
        QTreeWidget::item { border-bottom: 1px solid %(line)s; }
        QTreeWidget::item:selected { background: %(selected)s; color: %(text)s; }
        QTreeWidget::item:hover { background: %(hover)s; }
        QHeaderView::section { background: %(header)s; color: %(secondary)s;
            border: none; padding: 6px 8px; font-size: 13px; }
        QTreeWidget::indicator, QCheckBox::indicator { width: 19px; height: 19px; }
        QTreeWidget::indicator:disabled { background: #E7EFEC; border: 1px solid #B8CEC5; }
        QWidget#kindTabLine { border-bottom: 1px solid %(line)s; }
        QTabBar { background: %(surface)s; }
        QTabBar::tab { background: %(surface)s; color: %(secondary)s;
            padding: 8px 27px; border: none; border-bottom: 2px solid %(line)s; }
        QTabBar::tab:selected { color: %(accent)s; font-weight: 600; border-bottom-color: %(accent)s; }
        QTabBar::tab:hover { background: %(hover)s; }
        QTabBar::tab:focus { background: %(selected)s; }
        QWidget#scanWarning { background: %(warning_bg)s; border-radius: 4px; }
        QWidget#scanWarning QLabel, QWidget#scanWarning QPushButton { background: transparent; }
        QLabel#scanWarningText { color: %(warning)s; font-size: 13px; }
        QPlainTextEdit { border: 1px solid %(line)s; border-radius: 5px;
            padding: 10px; selection-background-color: %(selected)s; }
        QProgressBar { border: none; background: %(line)s; }
        QProgressBar::chunk { background: %(accent)s; }
        QScrollBar:vertical { background: %(header)s; width: 10px; margin: 0; }
        QScrollBar::handle:vertical { background: #A4C0B7; min-height: 28px; border-radius: 4px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        QToolTip { background: %(text)s; color: white; border: none; padding: 6px; }
    """ % TOKENS


class ResourceDelegate(QStyledItemDelegate):
    """Display a two-line name, with native checkbox and selection behavior."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:
        return QSize(super().sizeHint(option, index).width(), 49)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if index.column() == 0:
            background = QStyleOptionViewItem(opt)
            background.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
            background.text = ""
            style = opt.widget.style() if opt.widget else QApplication.style()
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter, opt.widget)
            opt.rect = opt.rect.adjusted(10, 0, 0, 0)
            if index.data(Qt.ItemDataRole.UserRole + 3):
                opt.state &= ~QStyle.StateFlag.State_Enabled
                opt.palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor("#E7EFEC"))
                opt.palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor("#E7EFEC"))
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
            return
        if index.column() not in (1, 6):
            super().paint(painter, opt, index)
            return
        value = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        painter.save()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        area = option.rect.adjusted(8, 4, -10, -4)
        font = QFont(option.font)
        font.setPixelSize(14)
        painter.setFont(font)
        if index.column() == 1:
            painter.setPen(QColor(TOKENS["text"]))
            name = painter.fontMetrics().elidedText(value, Qt.TextElideMode.ElideMiddle, area.width())
            painter.drawText(QRect(area.x(), area.y(), area.width(), 22),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QColor(TOKENS["secondary"]))
            context = str(index.data(Qt.ItemDataRole.UserRole + 1) or "")
            context = painter.fontMetrics().elidedText(context, Qt.TextElideMode.ElideRight, area.width())
            painter.drawText(QRect(area.x(), area.y() + 23, area.width(), 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, context)
        else:
            display = str(index.data(Qt.ItemDataRole.UserRole + 2) or value)
            color = TOKENS["error"] if value == "下载失败" else TOKENS["warning"] if any(
                word in value for word in ("未确认", "权限", "部分", "片段")) else TOKENS["accent"] if (
                    value.startswith(("已导出", "已下载")) or "完整缓存" in value) else TOKENS["text"]
            painter.setPen(QColor(color))
            font.setPixelSize(13)
            painter.setFont(font)
            painter.drawText(area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter |
                             Qt.TextFlag.TextWordWrap, display)
        painter.restore()

    def editorEvent(self, event: QEvent, model: QAbstractItemModel, option: QStyleOptionViewItem,
                    index: QModelIndex | QPersistentModelIndex) -> bool:
        if index.column() == 0:
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.adjusted(10, 0, 0, 0)
        return super().editorEvent(event, model, option, index)


class CourseDelegate(QStyledItemDelegate):
    """Paint the native sidebar item's label/count without fake interactive UI."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        section = bool(index.data(Qt.ItemDataRole.UserRole + 2))
        painter.save()
        font = QFont(option.font)
        font.setPixelSize(13 if section else 14)
        if section or option.state & QStyle.StateFlag.State_Selected:
            font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if not section and (selected or option.state & QStyle.StateFlag.State_MouseOver):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(TOKENS["selected"] if selected else TOKENS["hover"]))
            painter.drawRoundedRect(rect.adjusted(0, 2, 0, -2), 4, 4)
        if not section and option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(TOKENS["accent"]), 1))
            painter.drawRoundedRect(rect.adjusted(1, 3, -1, -3), 4, 4)
        painter.setPen(QColor(TOKENS["secondary"] if section else TOKENS["accent"] if selected else TOKENS["text"]))
        if section:
            painter.drawText(rect.adjusted(12, 0, -12, -9),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, index.data() or "")
        else:
            area = rect.adjusted(12, 0, -40, 0)
            name = painter.fontMetrics().elidedText(str(index.data() or ""), Qt.TextElideMode.ElideRight, area.width())
            painter.drawText(area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
            painter.drawText(rect.adjusted(0, 0, -12, 0), Qt.AlignmentFlag.AlignRight |
                             Qt.AlignmentFlag.AlignVCenter, str(index.data(Qt.ItemDataRole.UserRole + 1) or 0))
        painter.restore()
