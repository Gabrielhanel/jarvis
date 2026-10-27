"""Leitura e gravação validadas das preferências (usa o settings.json e o `Config` que já existem). Uma gravação por vez; repetir o mesmo
valor não faz nada (nem avisa ninguém de novo); valor inválido NUNCA é gravado; o que já estava salvo e é inválido volta ao padrão."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from app.core.config import Config
from app.core.user_settings import SETTINGS, coerce

log = logging.getLogger("jarvis.settings")
_MISSING = object()


@dataclass(frozen=True)
class SetResult:
    ok: bool
    message: str
    restart: bool = False
    changed: bool = False
    value: Any = None


class SettingsService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._listeners: list[tuple[str, Callable[[str, Any], None]]] = []

    def value(self, key: str) -> Any:
        spec = SETTINGS[key]
        raw = self.config.get(key, _MISSING)
        if raw is _MISSING:
            return spec.default
        value, err = coerce(spec, raw)
        return spec.default if err else value

    def values(self) -> dict[str, Any]:
        return {k: self.value(k) for k in SETTINGS}

    def sanitize(self) -> list[str]:
        """Chaves cujo valor SALVO era inválido (o programa usa o padrão; o arquivo só muda quando o usuário salvar de novo).
        Registra a ocorrência sem o valor (pode ser sensível)."""
        bad = []
        for key, spec in SETTINGS.items():
            raw = self.config.get(key, _MISSING)
            if raw is not _MISSING and coerce(spec, raw)[1]:
                bad.append(key)
                log.warning("[SETTINGS] valor inválido em '%s'; usando o padrão", key)
        return bad

    def subscribe(self, prefix: str, fn: Callable[[str, Any], None]) -> None:
        self._listeners.append((prefix, fn))

    def set(self, key: str, raw: Any) -> SetResult:
        spec = SETTINGS.get(key)
        if spec is None:
            return SetResult(False, "Preferência desconhecida.")
        value, err = coerce(spec, raw)
        if err:
            return SetResult(False, err)
        with self._lock:
            current = self.value(key)
            if value == current and type(value) is type(current):
                return SetResult(True, "Já estava assim.", spec.restart, False, value)  # evento repetido da interface: não regrava nem reaplica
            had = self.config.get(key, _MISSING)
            self.config.set(key, value)
            try:
                self.config.save()
            except OSError as exc:
                if had is _MISSING:
                    self._drop(key)
                else:
                    self.config.set(key, had)
                log.error("[SETTINGS] não consegui gravar '%s' (%s)", key, type(exc).__name__)
                return SetResult(False, "Não consegui salvar o arquivo de configurações; a mudança foi desfeita.")
        for prefix, fn in list(self._listeners):
            if key.startswith(prefix):
                try:
                    fn(key, value)
                except Exception:  # noqa: BLE001 - um ouvinte com defeito não desfaz a gravação nem derruba o painel
                    log.exception("[SETTINGS] falha ao aplicar '%s'", key)
        return SetResult(True, "Salvo. Vale na próxima vez que abrir o Jarvis." if spec.restart else "Salvo.", spec.restart, True, value)

    def _drop(self, key: str) -> None:
        *parents, last = key.split(".")
        node = self.config._settings
        for part in parents:
            node = node.get(part, {})
        node.pop(last, None)
