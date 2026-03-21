from __future__ import annotations

import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .ui import MainWindow


def _build_stylesheet() -> str:
    return """
    QWidget {
        background: #07111d;
        color: #e7f3ff;
        font-family: "Microsoft YaHei UI";
        font-size: 13px;
    }
    QMainWindow {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #050d17, stop:0.55 #07111d, stop:1 #0a1625);
    }
    QFrame#Sidebar {
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0b1626, stop:1 #09101b);
        border-right: 1px solid #1c3147;
    }
    QFrame#HeroCard, QFrame#PanelCard, QFrame#ComposerCard {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(13, 29, 48, 235), stop:1 rgba(9, 20, 34, 245));
        border: 1px solid #21405c;
        border-radius: 20px;
    }
    QFrame#InsetCard {
        background: rgba(8, 19, 31, 230);
        border: 1px solid #274764;
        border-radius: 18px;
    }
    QLabel#AppTitle {
        font-family: "Bahnschrift SemiBold";
        font-size: 24px;
        font-weight: 700;
        color: #f2fbff;
    }
    QLabel#SubTitle {
        color: #8ea8c3;
        font-size: 12px;
    }
    QLabel#PageTitle {
        font-family: "Bahnschrift SemiBold";
        font-size: 22px;
        font-weight: 700;
    }
    QLabel#SectionTitle {
        font-family: "Bahnschrift SemiBold";
        font-size: 15px;
        font-weight: 700;
        color: #ebf6ff;
    }
    QLabel#MutedLabel {
        color: #95a9bf;
    }
    QLabel#StatusChip {
        padding: 6px 12px;
        border-radius: 14px;
        background: rgba(79, 214, 255, 30);
        color: #8de9ff;
        border: 1px solid #2f7e9b;
        font-family: "Bahnschrift";
        font-size: 12px;
        font-weight: 700;
    }
    QPushButton {
        background: #102033;
        border: 1px solid #29445f;
        border-radius: 14px;
        padding: 10px 16px;
        color: #eef8ff;
    }
    QPushButton:hover {
        border-color: #4dd3ff;
        background: #13304b;
    }
    QPushButton:pressed {
        background: #0e2438;
    }
    QPushButton[nav="true"] {
        text-align: left;
        padding: 12px 16px;
        font-size: 14px;
        background: transparent;
        border: 1px solid transparent;
        border-radius: 16px;
        color: #9db5cb;
    }
    QPushButton[nav="true"]:checked {
        background: #11253a;
        border-color: #3ebde8;
        color: #f3fbff;
    }
    QPushButton#accent {
        background: #0f4b66;
        border-color: #62dcff;
    }
    QPushButton#accent:hover {
        background: #15607f;
    }
    QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QListWidget, QSpinBox, QDoubleSpinBox {
        background: #091522;
        border: 1px solid #24415d;
        border-radius: 14px;
        padding: 8px 10px;
        selection-background-color: #1f6f97;
    }
    QProgressBar {
        background: #06111d;
        border: 1px solid #284765;
        border-radius: 10px;
        min-height: 14px;
    }
    QProgressBar::chunk {
        border-radius: 9px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1693c0, stop:1 #79efff);
    }
    QPlainTextEdit, QTextBrowser {
        padding: 10px 12px;
    }
    QListWidget {
        padding: 8px;
    }
    QListWidget::item {
        padding: 10px;
        margin: 4px 0;
        border-radius: 12px;
    }
    QListWidget::item:selected {
        background: #11314b;
        border: 1px solid #55d8ff;
    }
    QScrollArea#ChatTimeline {
        background: qradialgradient(cx:0.2, cy:0.2, radius:1.2, fx:0.1, fy:0.1, stop:0 rgba(33, 64, 92, 90), stop:0.35 rgba(10, 22, 37, 255), stop:1 rgba(8, 16, 28, 255));
        border: 1px solid #1f3650;
        border-radius: 22px;
    }
    QScrollArea#ChatTimeline[dragActive="true"] {
        border: 2px dashed #63dcff;
    }
    QSplitter::handle {
        background: #112135;
        width: 2px;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 12px;
        margin: 8px 0;
    }
    QScrollBar::handle:vertical {
        background: #284967;
        border-radius: 6px;
        min-height: 24px;
    }
    QDialog, QMessageBox {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #08131f, stop:1 #0a1726);
    }
    QDialog QLabel, QMessageBox QLabel {
        background: transparent;
    }
    QMessageBox QLabel {
        min-width: 360px;
    }
    QDialog QPushButton, QMessageBox QPushButton {
        min-width: 96px;
    }
    """


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Nebula Audit")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(_build_stylesheet())

    window = MainWindow()
    window.show()
    return app.exec()
