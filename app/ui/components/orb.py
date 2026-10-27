"""Orb central do Jarvis, desenhado com QPainter. Só conhece o estado visual."""
from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui.states import JarvisState as S
from app.ui.styles.theme import STATE_COLORS

# (intensidade do brilho, rotação dos arcos em graus/s)
PROFILE = {S.IDLE: (.55, 14), S.LISTENING: (.85, 30), S.THINKING: (.8, 240),
           S.SPEAKING: (1.0, 70), S.ERROR: (.8, 0), S.OFFLINE: (.22, 0)}


def _alpha(color: QColor, a: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(max(0.0, min(1.0, a)))
    return out


def _step(a: int, b: int) -> int:
    d = b - a
    return b if abs(d) <= 2 else a + round(d * .2)


class JarvisOrb(QWidget):
    FRAME_MS = 33  # ~30 fps nos estados ativos
    IDLE_FRAME_MS = 50  # 20 fps em repouso (movimento lento)
    # O timer só roda quando o orb está visível e animando.

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = S.IDLE
        self._color = QColor(STATE_COLORS[S.IDLE])
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(self.IDLE_FRAME_MS)
        self._halo: tuple[tuple, QPixmap] | None = None
        self._timer.timeout.connect(self._tick)
        self.setMinimumSize(140, 140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    @property
    def state(self) -> S:
        return self._state

    def set_state(self, state: S | str) -> None:
        self._state = S.coerce(state)
        self._timer.setInterval(self.IDLE_FRAME_MS if self._state is S.IDLE else self.FRAME_MS)
        if self.isVisible() and not self._timer.isActive():
            self._timer.start()
        self.update()

    def _tick(self) -> None:
        target, c = QColor(STATE_COLORS[self._state]), self._color
        self._color = QColor(_step(c.red(), target.red()), _step(c.green(), target.green()),
                             _step(c.blue(), target.blue()))
        if self._state is S.OFFLINE and self._color == target:
            self._timer.stop()  # offline é estático: sem custo de CPU
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def _halo_pixmap(self, rad: float, col: QColor, glow: float) -> QPixmap:
        dpr = self.devicePixelRatioF()
        key = (round(rad), col.rgb(), glow, dpr)
        if self._halo is None or self._halo[0] != key:
            pm = QPixmap(max(2, int(2 * rad * dpr)), max(2, int(2 * rad * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            q.setPen(Qt.PenStyle.NoPen)
            grad = QRadialGradient(QPointF(rad, rad), rad)
            for pos, a in ((0, .5), (.45, .2), (1, 0)):
                grad.setColorAt(pos, _alpha(col, a * glow * 1.4))
            q.setBrush(QBrush(grad))
            q.drawEllipse(QPointF(rad, rad), rad, rad)
            q.end()
            self._halo = (key, pm)
        return self._halo[1]

    def _pulse(self, t: float) -> float:
        s = self._state
        if s is S.IDLE:
            return .035 * math.sin(t * 1.6)
        if s is S.LISTENING:
            return .05 * math.sin(t * 3.2)
        if s is S.THINKING:
            return .03 * math.sin(t * 7)
        if s is S.SPEAKING:
            return .02 + .11 * abs(math.sin(t * 5.5) * math.sin(t * 1.9 + 1))
        if s is S.ERROR:
            return .05 * math.sin(t * 9)
        return 0.0

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) / 2
        t = self._clock.elapsed() / 1000
        glow, spin = PROFILE[self._state]
        col = self._color
        r0 = radius * .30 * (1 + self._pulse(t))

        halo_r = radius * .84  # brilho externo: imagem em cache, só refeita se cor/tamanho mudarem
        p.setOpacity(max(.5, min(1.0, .85 + self._pulse(t) * 1.5)))
        p.drawPixmap(QPointF(c.x() - halo_r, c.y() - halo_r), self._halo_pixmap(halo_r, col, glow))
        p.setOpacity(1.0)

        p.setBrush(Qt.BrushStyle.NoBrush)  # anéis fixos
        for k, a in ((1.5, .30), (2.0, .16)):
            p.setPen(QPen(_alpha(col, a * (.4 + glow)), 1.2))
            p.drawEllipse(c, r0 * k, r0 * k)

        for ring, count, span, sign, a in ((1.75, 3, 55, 1, .9), (2.25, 2, 40, -1, .5)):  # arcos giratórios
            rr = r0 * ring
            rect = QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr)
            p.setPen(QPen(_alpha(col, a * (.5 + glow / 2)), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(count):
                p.drawArc(rect, int((sign * spin * t + i * 360 / count) * 16), span * 16)

        if self._state in (S.LISTENING, S.SPEAKING):  # ondas que se expandem
            speed = .55 if self._state is S.LISTENING else 1.1
            for i in range(2):
                k = (t * speed + i * .5) % 1
                rr = r0 * 1.2 + (radius * .94 - r0 * 1.2) * k
                p.setPen(QPen(_alpha(col, .55 * (1 - k)), 1.6))
                p.drawEllipse(c, rr, rr)

        core = QRadialGradient(c, r0, QPointF(c.x() - r0 * .3, c.y() - r0 * .35))  # núcleo
        core.setColorAt(0, QColor("#ffffff"))
        core.setColorAt(.25, col.lighter(130))
        core.setColorAt(1, col.darker(320))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(core))
        p.drawEllipse(c, r0, r0)
