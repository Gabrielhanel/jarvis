"""Cliente da Anthropic (SDK oficial). Só prepara a chamada, extrai o texto e traduz erros; não conhece a UI.

Decisões (documentação oficial da API de mensagens e do Claude Haiku 5.5):
- `max_retries=0`: o SDK repetiria sozinho (padrão 2) requisições faturáveis; aqui cada chamada é explícita.
- Sem `temperature`/`top_p`/`top_k` (os modelos atuais os rejeitam).
- `thinking: disabled` + `output_config.effort`: o pensamento adaptativo gasta tokens de `max_tokens`.
- Sem troca silenciosa de modelo: se o configurado não existir, o erro é mostrado.
As mensagens enviadas aqui SAEM do computador (Anthropic); a voz e o STT continuam locais."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from app.core.config import AIConfig

log = logging.getLogger("jarvis.ai")


class AIError(Exception):
    """Falha da IA já traduzida para o usuário. `ambiguous`: a API pode ter processado mesmo assim (ex.: timeout)."""

    def __init__(self, kind: str, message: str, retryable: bool = False, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.kind, self.message, self.retryable, self.ambiguous = kind, message, retryable, ambiguous


@dataclass(frozen=True)
class AIResult:
    text: str
    input_tokens: int | None  # contadores REAIS devolvidos pela API (None se ausentes)
    output_tokens: int | None
    model: str
    duration_s: float
    stop_reason: str | None
    request_id: str | None


def _default_client(api_key: str, cfg: AIConfig):
    import anthropic
    return anthropic.Anthropic(api_key=api_key, timeout=cfg.timeout_s, max_retries=0)


class AIService:
    def __init__(self, cfg: AIConfig, key_provider: Callable[[], str | None],
                 client_factory: Callable[[str, AIConfig], object] = _default_client) -> None:
        self.cfg, self._key, self._factory = cfg, key_provider, client_factory
        self._client, self._client_key = None, None  # criado sob demanda; criar o cliente não faz chamadas de rede

    @property
    def is_configured(self) -> bool:
        return bool(self._key())

    def generate(self, system: str, messages: list[dict]) -> AIResult:
        problem = self._config_problem()
        if problem:
            raise AIError("config", problem)
        key = self._key()
        if not key:
            raise AIError("no_key", "A chave da Anthropic não está configurada. Defina ANTHROPIC_API_KEY no arquivo .env "
                                    "(veja .env.example) e tente de novo.")
        try:
            import anthropic
        except ImportError as exc:
            raise AIError("unavailable", "O pacote 'anthropic' não está instalado. Rode: pip install -r requirements.txt") from exc
        if self._client is None or key != self._client_key:
            self._client, self._client_key = self._factory(key, self.cfg), key
        kwargs: dict = dict(model=self.cfg.model, max_tokens=self.cfg.max_output_tokens, system=system, messages=messages)
        if self.cfg.disable_thinking:
            kwargs["thinking"] = {"type": "disabled"}
        if self.cfg.effort:
            kwargs["output_config"] = {"effort": self.cfg.effort}
        t0 = time.monotonic()
        try:
            resp = self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise self._translate(anthropic, exc) from exc
        dt = time.monotonic() - t0
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        usage = getattr(resp, "usage", None)
        result = AIResult(text, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None), self.cfg.model, dt,
                          getattr(resp, "stop_reason", None), getattr(resp, "_request_id", None))
        log.info("[AI] Resposta recebida: modelo=%s entrada=%s saída=%s (reais, da API) %.1fs parada=%s",
                 result.model, result.input_tokens, result.output_tokens, dt, result.stop_reason)
        if not text:
            hint = " (o limite de saída pode estar baixo demais)" if result.stop_reason == "max_tokens" else ""
            raise AIError("empty", f"A IA não devolveu texto utilizável{hint}.", retryable=True)
        return result

    def _config_problem(self) -> str | None:
        c = self.cfg
        if not c.model:
            return "Nenhum modelo configurado em ai.model."
        if not 16 <= c.max_output_tokens <= 8192:
            return "ai.max_output_tokens deve estar entre 16 e 8192."
        if c.context_token_budget < 500:
            return "ai.context_token_budget deve ser pelo menos 500."
        return None

    @staticmethod
    def _translate(anthropic, exc) -> AIError:
        status, rid = getattr(exc, "status_code", None), getattr(exc, "request_id", None)
        log.warning("[AI] Erro da API: %s status=%s request_id=%s", type(exc).__name__, status, rid)  # sem segredos, sem conteúdo
        if isinstance(exc, anthropic.APITimeoutError):
            return AIError("timeout", "A IA demorou demais para responder. A solicitação pode ter sido processada mesmo assim; "
                                      "tente de novo se quiser.", retryable=True, ambiguous=True)
        if isinstance(exc, anthropic.APIConnectionError):
            return AIError("network", "Sem conexão com a IA no momento. Os recursos locais continuam funcionando.", retryable=True)
        if isinstance(exc, anthropic.AuthenticationError):
            return AIError("auth", "A API recusou a chave. Verifique a ANTHROPIC_API_KEY no arquivo .env.")
        if isinstance(exc, anthropic.PermissionDeniedError):
            return AIError("permission", "A chave não tem permissão para usar este modelo ou recurso.")
        if isinstance(exc, anthropic.NotFoundError):
            return AIError("model", "O modelo configurado não está disponível para esta conta. Ajuste 'ai.model' em data/settings.json.")
        if isinstance(exc, anthropic.RateLimitError):
            return AIError("rate_limit", "O serviço recusou temporariamente por excesso de requisições. Tente de novo em instantes.",
                           retryable=True)
        if isinstance(exc, anthropic.BadRequestError):
            if "credit balance" in str(exc).lower():
                return AIError("billing", "A conta da Anthropic está sem créditos. Verifique o saldo no console.")
            return AIError("bad_request", "A API recusou a solicitação (parâmetros). Verifique 'ai.model', 'ai.effort' e 'ai.disable_thinking'.")
        if isinstance(exc, anthropic.InternalServerError) or (status or 0) >= 500:
            return AIError("server", "O serviço da IA está temporariamente indisponível. Tente de novo em instantes.", retryable=True)
        return AIError("unexpected", "Falha inesperada ao falar com a IA. Veja o log para detalhes.", retryable=True)
