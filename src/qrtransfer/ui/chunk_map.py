"""チャンクマップ: 受信済み=緑、未受信=灰のグリッド。多い場合は 1 セルに複数チャンクをまとめる。"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

MAX_CELLS = 2000
MAX_CELL_PX = 24
COLOR_DONE = QColor(46, 160, 67)
COLOR_PARTIAL = QColor(150, 205, 120)
COLOR_NONE = QColor(200, 200, 200)


class ChunkMapWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.total = 0
        self.per_cell = 1
        self.cells = np.zeros(0, dtype=np.float32)  # 各セルの受信率 0..1
        self.setMinimumSize(200, 80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def sizeHint(self) -> QSize:
        return QSize(360, 160)

    def set_state(self, total: int | None, bitmap: bytes) -> None:
        total = total or 0
        self.total = total
        if total == 0:
            self.cells = np.zeros(0, dtype=np.float32)
            self.per_cell = 1
            self.update()
            return
        bits = np.unpackbits(np.frombuffer(bitmap, dtype=np.uint8), count=total) if bitmap else np.zeros(total, np.uint8)
        self.per_cell = max(1, math.ceil(total / MAX_CELLS))
        n_cells = math.ceil(total / self.per_cell)
        padded = np.zeros(n_cells * self.per_cell, dtype=np.float32)
        padded[:total] = bits
        counts = padded.reshape(n_cells, self.per_cell).sum(axis=1)
        sizes = np.full(n_cells, self.per_cell, dtype=np.float32)
        sizes[-1] = total - (n_cells - 1) * self.per_cell
        self.cells = counts / sizes
        self.update()

    def _layout(self) -> tuple[int, int, int]:
        n = len(self.cells)
        w, h = max(1, self.width()), max(1, self.height())
        cols = max(1, math.ceil(math.sqrt(n * w / h))) if n else 1
        rows = max(1, math.ceil(n / cols))
        cell = max(1, min(w // cols, h // rows, MAX_CELL_PX))
        return cols, rows, cell

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().window())
        n = len(self.cells)
        if n == 0:
            p.setPen(self.palette().text().color())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "チャンクマップ（受信待ち）")
            return
        cols, rows, cell = self._layout()
        gap = 1 if cell >= 4 else 0
        for i, v in enumerate(self.cells):
            color = COLOR_DONE if v >= 1.0 else (COLOR_PARTIAL if v > 0 else COLOR_NONE)
            r, c = divmod(i, cols)
            p.fillRect(QRect(c * cell, r * cell, cell - gap, cell - gap), color)

    def mouseMoveEvent(self, event) -> None:
        n = len(self.cells)
        if not n:
            return
        cols, rows, cell = self._layout()
        pos = event.position().toPoint()
        c, r = pos.x() // cell, pos.y() // cell
        i = r * cols + c
        if 0 <= c < cols and 0 <= i < n:
            start = i * self.per_cell
            end = min(self.total, start + self.per_cell) - 1
            label = f"{start}" if start == end else f"{start}-{end}"
            QToolTip.showText(event.globalPosition().toPoint(), f"チャンク {label}: {self.cells[i] * 100:.0f}%", self)
