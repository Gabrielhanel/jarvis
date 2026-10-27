"""Credenciais das integrações (endereços privados iCal, senha de aplicativo do Gmail).

- O programa só GRAVA no cofre do sistema (Windows Credential Manager, macOS Keychain, Secret Service) via `keyring`.
- Se não houver cofre disponível, NÃO grava em arquivo: informa a limitação.
- Alternativa explícita, só de LEITURA: variável de ambiente JARVIS_<NOME> (como a chave da Anthropic). O programa nunca a grava.
- Nenhuma mensagem de erro ou log inclui o valor."""
from __future__ import annotations

import logging
import os
import re
from typing import Mapping, Protocol

log = logging.getLogger("jarvis.integrations")


class SecretStoreUnavailable(Exception):
    """Não há cofre seguro neste computador; nada foi gravado."""


class SecretStore(Protocol):
    @property
    def available(self) -> bool: ...
    def get(self, name: str) -> str | None: ...
    def source(self, name: str) -> str | None: ...  # "env" | "keyring" | None
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


def env_name(name: str) -> str:
    return "JARVIS_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


class KeyringSecretStore:
    SERVICE = "Jarvis"

    def __init__(self, keyring_module=None, env: Mapping[str, str] | None = None) -> None:
        self._kr, self._env = keyring_module or None, os.environ if env is None else env  # keyring_module=False: sem cofre (testes)
        self._loaded = keyring_module is not None

    def _keyring(self):
        if not self._loaded:
            self._loaded = True
            try:
                import keyring  # type: ignore[import-not-found]
                self._kr = keyring
            except ImportError:
                self._kr = None
        return self._kr

    @property
    def available(self) -> bool:
        kr = self._keyring()
        if kr is None:
            return False
        try:
            backend = kr.get_keyring()
        except Exception:  # noqa: BLE001 - qualquer falha ao localizar o cofre = indisponível
            return False
        return getattr(backend, "priority", 0) > 0 and "fail" not in type(backend).__module__.lower()

    def source(self, name: str) -> str | None:
        if self._env.get(env_name(name)):
            return "env"
        return "keyring" if self._read(name) else None

    def get(self, name: str) -> str | None:
        value = self._env.get(env_name(name))
        if value and value.strip():
            return value.strip()
        return self._read(name)

    def _read(self, name: str) -> str | None:
        if not self.available:
            return None
        try:
            return self._keyring().get_password(self.SERVICE, name) or None
        except Exception as exc:  # noqa: BLE001
            log.warning("[SEC] Não consegui ler uma credencial do cofre (%s)", type(exc).__name__)
            return None

    def set(self, name: str, value: str) -> None:
        if not self.available:
            raise SecretStoreUnavailable("Não há cofre de credenciais disponível neste computador, então nada foi salvo. "
                                         "Instale o pacote 'keyring' (pip install -r requirements.txt) ou use a variável de ambiente "
                                         f"{env_name(name)}.")
        try:
            self._keyring().set_password(self.SERVICE, name, value)
        except Exception as exc:  # noqa: BLE001
            log.warning("[SEC] Falha ao gravar no cofre (%s)", type(exc).__name__)
            raise SecretStoreUnavailable("O cofre de credenciais recusou a gravação; nada foi salvo.") from None

    def delete(self, name: str) -> None:
        if not self.available:
            return
        try:
            self._keyring().delete_password(self.SERVICE, name)
        except Exception as exc:  # noqa: BLE001 - "não existia" também cai aqui
            log.info("[SEC] Nada a remover do cofre (%s)", type(exc).__name__)


class MemorySecretStore:
    """Cofre em memória: só para testes. Nunca grava em disco."""

    def __init__(self, available: bool = True) -> None:
        self._available, self._data = available, {}

    @property
    def available(self) -> bool:
        return self._available

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def source(self, name: str) -> str | None:
        return "keyring" if name in self._data else None

    def set(self, name: str, value: str) -> None:
        if not self._available:
            raise SecretStoreUnavailable("Não há cofre de credenciais disponível neste computador, então nada foi salvo.")
        self._data[name] = value

    def delete(self, name: str) -> None:
        self._data.pop(name, None)
