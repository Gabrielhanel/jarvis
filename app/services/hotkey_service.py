"""Atalho GLOBAL do Jarvis (Etapa 9).

Realidade das plataformas (por isso o desenho com backends):
- Windows: RegisterHotKey (ctypes, sem dependência nova). É um atalho global de verdade.
- Linux (X11/Wayland) e macOS: NÃO há suporte nesta versão. O Qt não oferece atalho global, e em Wayland o sistema não permite
  captura de teclas fora da janela sem portal do compositor. Nesse caso o serviço diz isso com honestidade; a interface oferece
  só um atalho LOCAL (vale com o Jarvis em foco), claramente rotulado como tal.
Um único pressionamento nunca dispara duas vezes (MOD_NOREPEAT + trava de tempo) e o registro é liberado ao encerrar."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

log = logging.getLogger("jarvis.hotkey")
MOD_BITS = {"ALT": 0x1, "CTRL": 0x2, "SHIFT": 0x4, "WIN": 0x8}
MOD_NOREPEAT, WM_HOTKEY, ERROR_HOTKEY_ALREADY_REGISTERED = 0x4000, 0x0312, 1409
_ALIASES = {"CONTROL": "CTRL", "CTL": "CTRL", "OPTION": "ALT", "META": "WIN", "SUPER": "WIN", "WINDOWS": "WIN", "CMD": "WIN"}
_RESERVED = {"ALT+F4", "WIN+L", "CTRL+ALT+F4", "CTRL+SHIFT+F4"}
_ORDER = ("CTRL", "ALT", "SHIFT", "WIN")


@dataclass(frozen=True)
class Combo:
    mods: tuple[str, ...]
    key: str  # letra, dígito, F1..F24 ou SPACE

    @property
    def text(self) -> str:
        return "+".join([m.capitalize() if m != "CTRL" else "Ctrl" for m in self.mods] + [self.key.capitalize() if self.key == "SPACE" else self.key])

    @property
    def mod_mask(self) -> int:
        return sum(MOD_BITS[m] for m in self.mods)

    @property
    def vk(self) -> int:
        if self.key == "SPACE":
            return 0x20
        if self.key.startswith("F") and self.key[1:].isdigit():
            return 0x70 + int(self.key[1:]) - 1
        return ord(self.key)


def parse_combo(text: str) -> tuple[Combo | None, str | None]:
    """Valida e normaliza ("ctrl + alt + j" -> Ctrl+Alt+J). Regras: exatamente uma tecla; pelo menos 2 modificadores (ou 1 + tecla F),
    para não roubar atalhos comuns como Ctrl+C; combinações reservadas do sistema são recusadas."""
    parts = [p.strip().upper() for p in (text or "").split("+")]
    if not text or any(not p for p in parts) or len(parts) < 2 or len(parts) > 5:
        return None, "Use o formato Modificador+Tecla, por exemplo Ctrl+Alt+J."
    mods, keys = [], []
    for p in parts:
        p = _ALIASES.get(p, p)
        (mods if p in MOD_BITS else keys).append(p)
    if len(keys) != 1:
        return None, "A combinação precisa de exatamente uma tecla (além dos modificadores)."
    key = keys[0]
    if not (re.fullmatch(r"[A-Z0-9]", key) or re.fullmatch(r"F([1-9]|1\d|2[0-4])", key) or key == "SPACE"):
        return None, "Tecla não suportada: use letras, números, F1–F24 ou Espaço."
    if len(set(mods)) != len(mods):
        return None, "Modificador repetido."
    is_f = key.startswith("F") and key != "F" and key[1:].isdigit()
    if len(mods) < 2 and not (is_f and len(mods) == 1):
        return None, "Use pelo menos dois modificadores (ex.: Ctrl+Alt+J) ou um modificador com uma tecla F."
    combo = Combo(tuple(sorted(set(mods), key=_ORDER.index)), key)
    if combo.text.upper() in _RESERVED:
        return None, "Essa combinação é reservada pelo sistema."
    return combo, None


class HotkeyState(Enum):
    DISABLED = "desativado"
    ACTIVE = "ativo (global)"
    UNSUPPORTED = "sem suporte neste sistema"
    FAILED = "falhou"


@dataclass(frozen=True)
class HotkeyStatus:
    state: HotkeyState
    combo: str = ""
    message: str = ""


@dataclass(frozen=True)
class RegisterResult:
    ok: bool
    message: str = ""
    conflict: bool = False


class NullBackend:
    """Sem suporte a atalho global: explica por quê, não finge."""
    supported = False
    name = "nenhum"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def register(self, combo: Combo, callback: Callable[[], None]) -> RegisterResult:
        return RegisterResult(False, self.reason)

    def unregister(self) -> None:
        pass

    def handle_native_message(self, message: int) -> bool:
        return False


class WindowsBackend:
    supported = True
    name = "Windows (RegisterHotKey)"
    HOTKEY_ID = 0x4A52  # "JR"

    def __init__(self, user32=None, kernel32=None) -> None:
        self._user32 = user32 or ctypes.windll.user32  # type: ignore[attr-defined]
        self._kernel32 = kernel32 or ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._callback: Callable[[], None] | None = None
        self._registered = False

    def register(self, combo: Combo, callback: Callable[[], None]) -> RegisterResult:
        if self._registered:
            return RegisterResult(False, "Já existe um atalho registrado.")  # nunca registra duas vezes
        ok = self._user32.RegisterHotKey(None, self.HOTKEY_ID, combo.mod_mask | MOD_NOREPEAT, combo.vk)
        if ok:
            self._callback, self._registered = callback, True
            return RegisterResult(True)
        if self._kernel32.GetLastError() == ERROR_HOTKEY_ALREADY_REGISTERED:
            return RegisterResult(False, f"{combo.text} já está em uso por outro programa. Escolha outra combinação.", conflict=True)
        return RegisterResult(False, f"O Windows recusou registrar {combo.text}.")

    def unregister(self) -> None:
        if self._registered:
            self._user32.UnregisterHotKey(None, self.HOTKEY_ID)
            self._registered, self._callback = False, None

    def handle_native_message(self, message: int) -> bool:
        """Chamado pelo filtro de eventos nativos do Qt (thread principal) com o ponteiro para a MSG do Windows."""
        if not self._registered or not message:
            return False
        from ctypes import wintypes
        msg = wintypes.MSG.from_address(int(message))
        if msg.message != WM_HOTKEY or msg.wParam != self.HOTKEY_ID:
            return False
        if self._callback:
            self._callback()
        return True


def detect_backend() -> WindowsBackend | NullBackend:
    if sys.platform.startswith("win"):
        try:
            return WindowsBackend()
        except (AttributeError, OSError) as exc:
            return NullBackend(f"Não consegui acessar a API de atalhos do Windows ({type(exc).__name__}).")
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if sys.platform.startswith("linux"):
        why = ("Sessões Wayland não permitem atalhos globais sem um portal do compositor." if session == "wayland"
               else "Atalho global em Linux (X11) ainda não é suportado nesta versão.")
        return NullBackend(why + " Use o atalho local (com o Jarvis em foco) ou o botão do microfone.")
    return NullBackend("Atalho global ainda não é suportado neste sistema. Use o atalho local ou o botão do microfone.")


class HotkeyService:
    DEBOUNCE_S = 0.5

    def __init__(self, backend, on_trigger: Callable[[], None], clock: Callable[[], float] = time.monotonic) -> None:
        self.backend, self._on_trigger, self._clock = backend, on_trigger, clock
        self._lock = threading.Lock()
        self._active: Combo | None = None
        self._last = -1e9
        self.status = HotkeyStatus(HotkeyState.DISABLED)

    @property
    def supported(self) -> bool:
        return bool(getattr(self.backend, "supported", False))

    def _fire(self) -> None:
        now = self._clock()
        if now - self._last < self.DEBOUNCE_S:
            return  # o mesmo pressionamento (ou um repique) nunca aciona duas vezes
        self._last = now
        try:
            self._on_trigger()
        except Exception:  # noqa: BLE001 - o atalho nunca derruba o aplicativo
            log.exception("[HOTKEY] falha ao acionar o Jarvis")

    def apply(self, enabled: bool, combo_text: str) -> HotkeyStatus:
        """Idempotente: aplicar de novo a mesma configuração não registra outra vez. Em caso de erro, o atalho anterior que funcionava continua."""
        with self._lock:
            if not enabled:
                self._release()
                self.status = HotkeyStatus(HotkeyState.DISABLED, message="Atalho global desativado.")
                return self.status
            combo, err = parse_combo(combo_text)
            if combo is None:
                keep = f" Mantive {self._active.text}." if self._active else ""
                self.status = HotkeyStatus(HotkeyState.ACTIVE if self._active else HotkeyState.FAILED, self._active.text if self._active else "", (err or "") + keep)
                return self.status
            if not self.supported:
                self.status = HotkeyStatus(HotkeyState.UNSUPPORTED, combo.text, getattr(self.backend, "reason", "Sem suporte neste sistema."))
                return self.status
            if self._active == combo:
                return self.status  # já registrado: nada a fazer
            previous = self._active
            self._release()
            result = self.backend.register(combo, self._fire)
            if result.ok:
                self._active = combo
                self.status = HotkeyStatus(HotkeyState.ACTIVE, combo.text, f"Atalho global {combo.text} ativo.")
            else:
                restored = previous is not None and self.backend.register(previous, self._fire).ok  # volta ao que funcionava
                self._active = previous if restored else None
                self.status = HotkeyStatus(HotkeyState.ACTIVE if restored else HotkeyState.FAILED, (previous.text if restored else combo.text),
                                           result.message + (f" Mantive {previous.text}." if restored else ""))
            return self.status

    def _release(self) -> None:
        if self._active is not None:
            self.backend.unregister()
            self._active = None

    def shutdown(self) -> None:
        with self._lock:
            self._release()
            self.status = HotkeyStatus(HotkeyState.DISABLED)
