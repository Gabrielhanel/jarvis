"""ÚNICO ponto que toca o sistema operacional para o controle do computador. Troque esta classe por um falso nos testes:
nenhum teste abre programas reais.

Regras fixas deste módulo:
- nada de shell: processos só via lista de argumentos com shell=False, a partir de um executável JÁ autorizado;
- abrir pasta/URL usa as APIs do sistema (os.startfile, xdg-open/open, webbrowser), nunca texto de comando;
- fechar é sempre um pedido de fechamento NORMAL (taskkill sem /F; wmctrl -c). Não existe encerramento forçado aqui."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from app.core.actions import detect_platform

log = logging.getLogger("jarvis.actions")

_SAFE_PROCESS = re.compile(r"^[A-Za-z0-9._-]{1,64}\.exe$")
_SAFE_CLASS = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@dataclass(frozen=True)
class OpenOutcome:
    ok: bool
    detail: str = ""  # motivo técnico curto (vai para o log, não para o usuário)
    unsupported: bool = False  # ferramenta do sistema ausente


@dataclass(frozen=True)
class CloseOutcome:
    status: str  # requested | not_running | refused | error | unsupported
    detail: str = ""


class SystemBackend:
    LAUNCH_WAIT_S = 0.4  # um programa que morre logo ao iniciar com código != 0 NÃO é "aberto"
    OPEN_WAIT_S = 2.0

    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform or detect_platform()

    # --- consulta ---
    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def is_executable(self, path: str) -> bool:
        p = Path(path)
        return p.is_file() and (self.platform == "windows" or os.access(p, os.X_OK))

    def uri_registered(self, scheme: str) -> bool:
        """O sistema tem um programa registrado para este protocolo? (não abre nada)"""
        try:
            if self.platform == "windows":
                import winreg  # type: ignore[import-not-found]
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme):
                    return True
            if self.platform == "linux" and shutil.which("xdg-mime"):
                out = subprocess.run(["xdg-mime", "query", "default", f"x-scheme-handler/{scheme}"], capture_output=True, timeout=3,
                                     shell=False, check=False)
                return bool(out.stdout.strip())
        except (OSError, subprocess.SubprocessError, ImportError):
            return False
        return False

    @property
    def can_close(self) -> bool:
        return (self.platform == "windows" and shutil.which("taskkill") is not None) or (self.platform == "linux" and shutil.which("wmctrl") is not None)

    # --- abrir ---
    def open_folder(self, path: str) -> OpenOutcome:
        return self._open_with_system(path)

    def open_uri(self, uri: str) -> OpenOutcome:
        return self._open_with_system(uri)

    def open_url(self, url: str) -> bool:
        """True = o navegador padrão ACEITOU abrir. Não prova que a página carregou."""
        try:
            return bool(webbrowser.open(url, new=2))
        except webbrowser.Error as exc:
            log.warning("[ACT] navegador recusou o pedido: %s", exc)
            return False

    def launch(self, exe: str, args: list[str]) -> OpenOutcome:
        if not isinstance(exe, str) or not exe or not all(isinstance(a, str) and "\0" not in a for a in args) or "\0" in exe:
            return OpenOutcome(False, "argumentos inválidos")
        return self._spawn([exe, *args], self.LAUNCH_WAIT_S)

    def _open_with_system(self, target: str) -> OpenOutcome:
        try:
            if self.platform == "windows":
                os.startfile(target)  # type: ignore[attr-defined]  # ShellExecute: aceita pasta ou protocolo registrado
                return OpenOutcome(True)
        except OSError as exc:
            return OpenOutcome(False, f"{type(exc).__name__}: {exc.errno}")
        opener = "open" if self.platform == "macos" else "xdg-open"
        if shutil.which(opener) is None:
            return OpenOutcome(False, f"{opener} ausente", unsupported=True)
        return self._spawn([opener, target], self.OPEN_WAIT_S)

    def _spawn(self, argv: list[str], wait_s: float) -> OpenOutcome:
        kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, close_fds=True)
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except (OSError, ValueError) as exc:
            return OpenOutcome(False, f"{type(exc).__name__}")
        try:
            code = proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            return OpenOutcome(True)  # continua rodando: o pedido foi aceito
        return OpenOutcome(code == 0, f"código de saída {code}" if code else "")

    # --- fechar (SEMPRE pedido normal, nunca forçado) ---
    def close(self, process: str, wm_class: str) -> CloseOutcome:
        try:
            if self.platform == "windows":
                if not _SAFE_PROCESS.fullmatch(process or "") or not shutil.which("taskkill"):
                    return CloseOutcome("unsupported")
                r = subprocess.run(["taskkill", "/IM", process], capture_output=True, timeout=5, shell=False, check=False)  # sem /F
                return CloseOutcome("requested" if r.returncode == 0 else "not_running" if r.returncode == 128 else "refused", f"código {r.returncode}")
            if self.platform == "linux":
                if not _SAFE_CLASS.fullmatch(wm_class or "") or not shutil.which("wmctrl"):
                    return CloseOutcome("unsupported")
                r = subprocess.run(["wmctrl", "-x", "-c", wm_class], capture_output=True, timeout=5, shell=False, check=False)
                return CloseOutcome("requested" if r.returncode == 0 else "refused", f"código {r.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            return CloseOutcome("error", type(exc).__name__)
        return CloseOutcome("unsupported")
