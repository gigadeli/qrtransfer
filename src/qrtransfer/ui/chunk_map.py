"""チャンクマップ: 受信済み=緑、未受信=灰のグリッド。多い場合は 1 セルに複数チャンクをまとめる。"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from . import theme

MAX_CELLS = 2000
MAX_CELL_PX = 24


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

    def _origin(self, cols: int, cell: int) -> int:
        return max(0, (self.width() - cols * cell) // 2)  # 横方向は中央に寄せる

    def paintEvent(self, event) -> None:
        t = theme.tokens()
        p = QPainter(self)
        n = len(self.cells)
        if n == 0:
            p.setPen(QColor(t.muted))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "チャンクマップ（受信待ち）")
            return
        done, partial, none = QColor(t.chunk_done), QColor(t.chunk_partial), QColor(t.chunk_none)
        cols, rows, cell = self._layout()
        gap = 2 if cell >= 10 else (1 if cell >= 4 else 0)
        radius = min(3.0, (cell - gap) / 4) if cell >= 6 else 0
        ox = self._origin(cols, cell)
        if radius:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for i, v in enumerate(self.cells):
            p.setBrush(done if v >= 1.0 else (partial if v > 0 else none))
            r, c = divmod(i, cols)
            rect = QRectF(ox + c * cell, r * cell, cell - gap, cell - gap)
            if radius:
                p.drawRoundedRect(rect, radius, radius)
            else:
                p.drawRect(rect)

    def mouseMoveEvent(self, event) -> None:
        n = len(self.cells)
        if not n:
            return
        cols, rows, cell = self._layout()
        pos = event.position().toPoint()
        c, r = (pos.x() - self._origin(cols, cell)) // cell, pos.y() // cell
        i = r * cols + c
        if 0 <= c < cols and 0 <= i < n:
            start = i * self.per_cell
            end = min(self.total, start + self.per_cell) - 1
            label = f"{start}" if start == end else f"{start}-{end}"
            QToolTip.showText(event.globalPosition().toPoint(), f"チャンク {label}: {self.cells[i] * 100:.0f}%", self)
