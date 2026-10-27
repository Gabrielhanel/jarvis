"""Tipos e utilidades compartilhados pelas integrações. Nada aqui conhece a interface, a IA ou o pipeline."""
from __future__ import annotations

import http.client
import logging
import re
import socket
import ssl
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable
from urllib.parse import urlsplit

log = logging.getLogger("jarvis.integrations")

USER_AGENT = "Jarvis-Desktop/0.8 (+assistente pessoal local)"


class ServiceState(Enum):
    UNCONFIGURED = "Sem configuração"
    UPDATING = "Atualizando…"
    OK = "Conectado e atualizado"
    CACHED = "Usando dados em cache"
    ERROR = "Com erro"


class IntegrationError(Exception):
    """Falha de uma integração. `message` é SEGURA para a tela e para o log: nunca contém URL, credencial nem conteúdo privado.
    kind: config | auth | network | timeout | http | data | location | busy | unavailable."""

    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind, self.message, self.status = kind, message, status

    @property
    def transient(self) -> bool:
        return self.kind in ("network", "timeout") or (self.kind == "http" and (self.status or 0) in (429, 500, 502, 503, 504))


@dataclass(frozen=True)
class ServiceStatus:
    service: str
    state: ServiceState
    message: str = ""  # texto curto e seguro para a interface
    last_ok: datetime | None = None  # última atualização BEM-SUCEDIDA (hora local)
    error_kind: str = ""

    @property
    def stale(self) -> bool:
        return self.state in (ServiceState.CACHED, ServiceState.ERROR)


def clean_text(value: str | None, limit: int = 200) -> str:
    """Texto externo para exibição: sem caracteres de controle/formatação invisíveis, espaços normalizados e tamanho limitado."""
    if not value:
        return ""
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def norm_key(text: str) -> str:
    """Chave de comparação (sem acento, minúsculas, só letras e números): títulos repetidos viram a mesma chave."""
    folded = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, schemes: tuple[str, ...]) -> None:
        self._schemes = schemes

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if urlsplit(newurl).scheme.lower() not in self._schemes:
            fp.close()  # a resposta original ficaria aberta ao recusar o redirecionamento
            raise urllib.error.URLError("redirecionamento para esquema não permitido")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str, *, timeout: float = 10.0, max_bytes: int = 2_000_000, schemes: tuple[str, ...] = ("https",),
             allowed_hosts: frozenset[str] | None = None, headers: dict[str, str] | None = None) -> bytes:
    """GET com timeout, tamanho máximo, só https e mensagens de erro SEM a URL (ela pode ser uma credencial)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in schemes or not host:
        raise IntegrationError("config", "O endereço configurado não é um endereço https válido.")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise IntegrationError("config", "O endereço configurado não é de um servidor permitido para este serviço.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})})
    opener = urllib.request.build_opener(_SafeRedirect(schemes))
    try:
        with opener.open(request, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        exc.close()  # libera o socket da resposta de erro
        raise IntegrationError("http", f"O serviço respondeu com erro HTTP {exc.code}.", status=exc.code) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise IntegrationError("timeout", "O serviço demorou demais para responder.") from None
        raise IntegrationError("network", "Não consegui conectar ao serviço (sem internet ou serviço fora do ar).") from None
    except (socket.timeout, TimeoutError):
        raise IntegrationError("timeout", "O serviço demorou demais para responder.") from None
    except ssl.SSLError:
        raise IntegrationError("network", "Falha na conexão segura com o serviço.") from None
    except (OSError, http.client.HTTPException):
        raise IntegrationError("network", "Não consegui conectar ao serviço (sem internet ou serviço fora do ar).") from None
    if len(data) > max_bytes:
        raise IntegrationError("data", "A resposta do serviço é grande demais; ignorei.")
    return data


def with_retries(fn: Callable[[], object], attempts: int = 2, delay: float = 0.8, sleep: Callable[[float], None] = time.sleep):
    """Repete SÓ falhas transitórias (rede, timeout, 429/5xx), poucas vezes. Credenciais inválidas e dados ruins não repetem."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except IntegrationError as exc:
            if not exc.transient or attempt == attempts:
                raise
            sleep(delay * attempt)


def compute_status(service: str, *, configured: bool, updating: bool, last_ok: datetime | None, has_data: bool, error_kind: str,
                   age_s: float | None, interval_s: float, unconfigured_msg: str, fail_msgs: dict[str, str] | None = None) -> ServiceStatus:
    """Estado mostrado ao usuário a partir de fatos simples; evita repetir a mesma lógica em cada serviço."""
    if not configured:
        return ServiceStatus(service, ServiceState.UNCONFIGURED, unconfigured_msg)
    if updating:
        return ServiceStatus(service, ServiceState.UPDATING, "Atualizando…", last_ok)
    if error_kind:
        base = (fail_msgs or {}).get(error_kind) or {"network": "Sem conexão com o serviço.", "timeout": "O serviço demorou demais.",
                                                        "location": "Não encontrei a localização configurada."}.get(error_kind, "Falha ao atualizar.")
        return ServiceStatus(service, ServiceState.CACHED if has_data else ServiceState.ERROR, base + (" Mostrando dados salvos." if has_data else ""), last_ok, error_kind)
    if not has_data:
        return ServiceStatus(service, ServiceState.UPDATING, "Aguardando a primeira atualização.", None)
    if age_s is not None and age_s > interval_s * 2.5:
        return ServiceStatus(service, ServiceState.CACHED, "Dados desatualizados.", last_ok)
    return ServiceStatus(service, ServiceState.OK, "Atualizado.", last_ok)
