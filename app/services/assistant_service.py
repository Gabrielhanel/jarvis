"""Entrada única de mensagens (texto digitado e voz) -> rota local ou IA -> resposta, uma vez cada.

Regras de proteção (cada uma tem teste):
- Uma submissão = um ID criado na ORIGEM do evento (caixa de texto ou fala finalizada). O mesmo ID chegando de novo
  (sinal duplicado) é ignorado; textos iguais com IDs diferentes são duas mensagens legítimas.
- Processamento SERIAL numa única thread: nunca há duas chamadas concorrentes; fila limitada (`max_pending`).
- A resposta pertence ao ID que a originou; trocar a conversa (`reset_conversation`) descarta respostas antigas.
- Sem retry automático (o SDK também roda com max_retries=0): falhas viram um erro claro e um retry EXPLÍCITO do usuário,
  limitado. Timeout é ambíguo: a API pode ter processado a solicitação.
- Pedidos que dependem de ações/serviços inexistentes são respondidos localmente, sem chamar a IA.

Persistência e memória (Etapa 6), sem criar um segundo pipeline:
- A mensagem aceita é gravada no banco ANTES de qualquer operação de rede; a resposta é gravada ANTES de ir para a tela.
- O banco reforça os IDs (UNIQUE por submissão+papel); o `retry` reutiliza a mensagem original, sem duplicá-la.
- Comandos claros de memória ("Lembre que...") são executados localmente, sem chamar a IA.
- Memórias relevantes são selecionadas localmente e entram no prompt como dados delimitados, dentro do orçamento de contexto.
- Se o banco falhar, o fluxo continua como na Etapa 5 e o usuário é avisado: nada é dado como salvo sem confirmação."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
import queue
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from PySide6.QtCore import QObject, Signal

from app.core.capabilities import Capability, CapabilityRegistry, build_system_prompt
from app.core.action_intents import classify_action, classify_confirmation
from app.core.config import AIConfig, MemoryConfig
from app.core.modes import profile as mode_profile
from app.core.integration_intents import classify_integration
from app.core.conversation import ConversationContext, estimate_tokens
from app.core.intents import classify, classify_memory, local_reply
from app.database.database import DatabaseError, IntegrityViolation
from app.services.action_service import ActionService
from app.services.ai_service import AIError, AIService
from app.services.history_service import HistoryService
from app.services.memory_service import MemoryReply, MemoryService

log = logging.getLogger("jarvis.ai")


class SubmissionState(Enum):
    PENDING = auto()
    PROCESSING = auto()
    DONE = auto()
    FAILED = auto()


class AssistantEvent(Enum):
    STARTED = auto()
    RESPONSE = auto()
    FAILED = auto()
    NOTICE = auto()  # aviso técnico (ex.: persistência indisponível); não é mensagem da conversa
    CONFIRMATION = auto()  # (id, resumo da ação, prazo em s): a UI mostra Confirmar/Cancelar
    CONFIRMATION_CLEARED = auto()  # (id, motivo): a UI esconde os botões
    ACTION_RESULT = auto()  # (id, texto, status): resultado de uma ação confirmada/cancelada pelos botões (não é mensagem da conversa)


@dataclass
class Submission:
    id: str
    text: str
    source: str
    epoch: int
    state: SubmissionState = SubmissionState.PENDING
    attempts: int = 1
    retryable: bool = False
    conversation_id: str | None = None  # fixado na aceitação: a resposta pertence a esta conversa
    persisted: bool = False  # a mensagem do usuário está no banco


class AssistantPipeline:
    LIMIT = 512  # IDs lembrados (evita crescer sem fim)

    def __init__(self, cfg: AIConfig, ai: AIService, registry: CapabilityRegistry, emit: Callable[..., None],
                 system_prompt: Callable[[CapabilityRegistry], str] = build_system_prompt,
                 history: HistoryService | None = None, memory: MemoryService | None = None,
                 memory_cfg: MemoryConfig | None = None, actions: ActionService | None = None, integrations=None,
                 routines=None, metrics=None, mode_provider: Callable[[], str] | None = None) -> None:
        self.cfg, self.ai, self.registry, self._emit_raw, self._system_prompt = cfg, ai, registry, emit, system_prompt
        self.routines, self.metrics = routines, metrics
        self.mode = mode_provider or (lambda: "EQUILIBRADO")  # o modo é lido a cada mensagem: trocar não exige reiniciar
        self._tl = threading.local()  # estado da submissão em processamento NESTA thread (conversa de origem, saída suprimida)
        self.history, self.memory, self.actions, self.integrations = history, memory, actions, integrations
        self._pool: ThreadPoolExecutor | None = None  # cliques de Confirmar/Cancelar da UI (fora da thread de UI e da fila da IA)
        self._pool_lock = threading.Lock()
        self.memory_cfg = memory_cfg or (memory.cfg if memory is not None else MemoryConfig())
        self._persist_warned = False
        self._current: Submission | None = None  # em processamento (para finalize() ao encerrar)
        self.context = ConversationContext(cfg)
        self.stats = {"requests": 0, "input_tokens": 0, "output_tokens": 0}  # só contadores REAIS devolvidos pela API
        self._queue: queue.Queue[Submission] = queue.Queue()
        self._lock = threading.Lock()
        self._subs: OrderedDict[str, Submission] = OrderedDict()
        self._epoch = 0
        self._stop = threading.Event()

    # --- API (segura entre threads) ---
    def submit(self, text: str, source: str = "texto", submission_id: str | None = None) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        sid = submission_id or uuid.uuid4().hex
        with self._lock:
            if sid in self._subs:
                log.info("[AI] Submissão %s duplicada: ignorada", sid[:8])
                return None
            sub = self._subs[sid] = Submission(sid, text, source, self._epoch)
            while len(self._subs) > self.LIMIT:
                self._subs.popitem(last=False)
            waiting = self._queue.qsize()
        if len(text) > self.cfg.max_message_chars:
            self._fail(sub, AIError("too_long", f"A mensagem tem {len(text)} caracteres e o limite é {self.cfg.max_message_chars}. "
                                                "Envie em partes menores."))
        elif waiting >= self.cfg.max_pending:
            self._fail(sub, AIError("busy", "Há mensagens demais aguardando. Espere terminar e envie de novo.", retryable=True))
        elif self._accept(sub):
            self._queue.put(sub)
        else:
            return None  # já registrada no banco: duplicada
        return sid

    def retry(self, sid: str) -> bool:
        """Nova tentativa EXPLÍCITA de uma submissão que falhou. Chamadas repetidas viram uma só."""
        with self._lock:
            sub = self._subs.get(sid)
            if sub is None or sub.state is not SubmissionState.FAILED or not sub.retryable or sub.attempts > self.cfg.max_manual_retries:
                return False
            sub.state, sub.attempts, sub.epoch = SubmissionState.PENDING, sub.attempts + 1, self._epoch
        self._mark(sub, "pending", attempts=sub.attempts)  # a MESMA mensagem volta à fila: nada é duplicado no histórico
        self._queue.put(sub)
        return True

    def reset_conversation(self) -> None:
        with self._lock:
            self._epoch += 1  # respostas em andamento da conversa antiga serão descartadas
        self.context.clear()

    def restore_context(self) -> int:
        """Reconstrói o contexto temporário a partir das trocas COMPLETAS da conversa ativa. Não chama a IA. Devolve quantas trocas."""
        cid = self.history.active_id if self.history is not None else None
        if self.history is None or cid is None:
            return 0
        try:
            turns = self.history.completed_turns(cid, self.memory_cfg.history_turns)
        except DatabaseError as exc:
            log.error("[HIST] Não consegui restaurar o contexto: %s", exc)
            return 0
        self.context.load_turns(turns)
        return len(turns)

    def activate_conversation(self, conversation_id: str) -> int:
        """Prepara a troca de conversa (a interface de sessões é da Etapa 9): respostas pendentes da anterior não vão para a nova tela."""
        if self.history is None:
            raise RuntimeError("Histórico indisponível.")
        self.history.set_active(conversation_id)
        with self._lock:
            self._epoch += 1
        self.context.clear()
        if self.actions is not None:  # uma confirmação pendente era da conversa anterior: não vale na nova
            for pid, reason in self.actions.cancel_pending("conversation_changed"):
                self._emit(AssistantEvent.CONFIRMATION_CLEARED, pid, reason)
        return self.restore_context()

    def stop(self) -> None:
        self._stop.set()
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if self.actions is not None:
            self.actions.cancel_pending("shutdown")  # uma confirmação pendente nunca sobrevive ao aplicativo

    # --- confirmação de ações (vinda dos botões da interface) ---
    def resolve_confirmation(self, pending_id: str, accept: bool) -> None:
        self._run_in_pool(self._resolve, pending_id, accept)

    def expire_confirmation(self, pending_id: str) -> None:
        self._run_in_pool(self._resolve, pending_id, None)

    def _run_in_pool(self, fn, *args) -> None:
        if self.actions is None or self._stop.is_set():
            return
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-action")
            self._pool.submit(fn, *args)

    def _resolve(self, pending_id: str, accept: bool | None) -> None:
        try:
            acts = self.actions
            outcome = acts.expire(pending_id) if accept is None else acts.confirm(pending_id) if accept else acts.cancel(pending_id)
        except Exception:  # noqa: BLE001
            log.exception("[ACT] Falha ao resolver a confirmação %s", pending_id[:8])
            self._emit(AssistantEvent.ACTION_RESULT, pending_id, "Não consegui executar isso agora.", "ERROR")
            return
        for pid, reason in outcome.cleared:
            self._emit(AssistantEvent.CONFIRMATION_CLEARED, pid, reason)
        if outcome.result.message:
            self._emit(AssistantEvent.ACTION_RESULT, pending_id, outcome.result.message, outcome.result.status.name)

    def finalize(self) -> None:
        """Chamado ao encerrar, depois de esperar a thread: uma requisição ainda em andamento fica `unconfirmed` (a API pode ter
        processado) em vez de `processing`. O UPDATE só vale se o estado ainda for `processing` (não sobrescreve uma conclusão)."""
        sub = self._current
        if sub is not None and sub.persisted and sub.state is SubmissionState.PROCESSING:
            self._mark(sub, "unconfirmed", "shutdown", only_from=("processing",))

    # --- thread de trabalho ---
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                sub = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._current = sub
            try:
                self._process(sub)
            except Exception:  # noqa: BLE001 - registrado com traceback; a thread não pode morrer por uma mensagem
                log.exception("[AI] Erro inesperado ao processar a submissão %s", sub.id[:8])
                self._fail(sub, AIError("unexpected", "Falha inesperada ao processar a mensagem. Veja o log.", retryable=True))
            finally:
                self._current = None
                self._tl.suppress, self._tl.ctx = False, None

    def _stale(self, sub: Submission) -> bool:
        """A submissão pertence a OUTRA conversa que não a ativa agora? (sem histórico: vale o contador de reinícios da Etapa 5)."""
        with self._lock:
            if self.history is not None and sub.conversation_id is not None:
                return sub.conversation_id != self.history.active_id
            return sub.epoch != self._epoch

    def _emit(self, *args) -> None:
        """Saída para a interface. Uma submissão de OUTRA conversa nunca aparece na tela atual (é só gravada na conversa de origem)."""
        if not getattr(self._tl, "suppress", False):
            self._emit_raw(*args)

    def _context(self) -> ConversationContext:
        return getattr(self._tl, "ctx", None) or self.context  # a submissão de outra conversa usa o contexto DA SUA conversa

    def apply_ai_config(self, cfg: AIConfig) -> None:
        """Aplica limites/modelo novos sem reiniciar (trocas de modo e de modelo)."""
        self.cfg, self.ai.cfg = cfg, cfg
        self.context.set_config(cfg)

    def _process(self, sub: Submission) -> None:
        foreign = self._stale(sub)
        self._tl.suppress, self._tl.ctx = foreign, None
        if foreign:
            if self.history is None:
                self._mark(sub, "interrupted", "conversation_changed")
                return
            # O usuário trocou de conversa com esta mensagem ainda na fila: ela continua sendo processada NA conversa de origem,
            # com o contexto DELA; a resposta é gravada lá e não aparece na tela atual.
            ctx = ConversationContext(self.cfg)
            try:
                ctx.load_turns(self.history.completed_turns(sub.conversation_id, self.memory_cfg.history_turns))
            except DatabaseError as exc:
                log.warning("[AI] Não consegui montar o contexto da conversa de origem: %s", exc)
            self._tl.ctx = ctx
        sub.state = SubmissionState.PROCESSING
        self._mark(sub, "processing")
        if foreign and self._foreign_blocked(sub):
            return
        if self.memory_cfg.enabled and (cmd := classify_memory(sub.text)) is not None:  # comando de memória: local, sem IA
            self._emit(AssistantEvent.STARTED, sub.id, "local")
            if self.memory is None or not self.memory.available:
                reply = MemoryReply("A memória permanente não está disponível agora (banco de dados indisponível). Nada foi salvo ou alterado.", op=cmd.kind.name.lower())
            else:
                reply = self.memory.execute(cmd, sub.conversation_id)
            if not reply.ok:  # falha técnica: nunca vira resposta de sucesso
                self._fail(sub, AIError("memory", reply.text, retryable=True))
                return
            self._context().add_exchange(sub.text, reply.text)
            self._complete(sub, reply.text, "local", {"api_call": False, "memory_op": reply.op, "memory_changed": reply.changed})
            return
        if self._handle_routine(sub):  # rotinas do usuário: só combinam ações que já existem, pelos mesmos caminhos seguros
            return
        if self._handle_action(sub):  # controle do computador: local, validado, sem IA
            return
        if self._handle_integration(sub):  # agenda, e-mails, notícias, clima, resumo da manhã: local, sem IA
            return
        intent = classify(sub.text)
        if intent is not None:  # resposta local verdadeira: nenhuma chamada à API
            self._emit(AssistantEvent.STARTED, sub.id, "local")
            reply = local_reply(intent, self.registry)
            self._context().add_exchange(sub.text, reply)
            self._complete(sub, reply, "local", {"api_call": False})
            return
        prof = mode_profile(self.mode())
        if not prof.ai_chat:  # modos Local/Offline: nenhuma mensagem sai para a IA (e nada é inventado no lugar)
            self._emit(AssistantEvent.STARTED, sub.id, "local")
            self._complete(sub, f"Estou no modo {prof.label}: não envio mensagens à IA. Comandos locais, memória, rotinas, agenda, e-mails, notícias e clima "
                                "continuam funcionando. Troque o modo em Configurações.", "local", {"api_call": False, "mode_blocked": True})
            return
        self.registry.set_available(Capability.CONVERSATION_AI, self.cfg.enabled and self.ai.is_configured)
        if not self.cfg.enabled:
            self._fail(sub, AIError("disabled", "A IA está desativada na configuração (ai.enabled)."))
            return
        self._emit(AssistantEvent.STARTED, sub.id, "ia")
        self.registry.set_available(Capability.MEMORY, self.memory is not None and self.memory.available)
        base_system = self._system_prompt(self.registry)
        block, memories_used = self._memory_block(sub.text, base_system)  # busca LOCAL; nenhuma chamada extra à IA
        system = base_system + block
        built = self._context().build(sub.text, system)
        try:
            result = self.ai.generate(system, built.messages)
        except AIError as err:
            self._fail(sub, err)
            return
        self.stats["requests"] += 1
        self.stats["input_tokens"] += result.input_tokens or 0
        self.stats["output_tokens"] += result.output_tokens or 0
        log.info("[AI] %s: entrada estimada=%d real=%s | saída real=%s | histórico: %d msgs (%d trocas omitidas)",
                 sub.id[:8], built.estimated_input_tokens, result.input_tokens, result.output_tokens,
                 len(built.messages) - 1, built.dropped_turns)
        meta = {
            "api_call": True, "model": result.model, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "estimated_input_tokens": built.estimated_input_tokens, "dropped_turns": built.dropped_turns,
            "duration_s": result.duration_s, "truncated": result.stop_reason == "max_tokens", "attempts": sub.attempts,
            "memories_used": memories_used}
        if self._stale(sub):
            log.info("[AI] Resposta da submissão %s descartada: a conversa foi reiniciada enquanto aguardava", sub.id[:8])
            sub.state = SubmissionState.DONE
            self._persist_response(sub, result.text, "ia", meta)  # foi paga e pertence à conversa de origem; só não vai para a tela atual
            self._record_metrics(sub, True, "ia", meta)
            return
        self._context().add_exchange(sub.text, result.text)
        self._complete(sub, result.text, "ia", meta)

    def _complete(self, sub: Submission, text: str, origin: str, meta: dict) -> None:
        with self._lock:
            if sub.state is SubmissionState.DONE:
                return  # uma submissão só responde uma vez
            sub.state = SubmissionState.DONE
        self._persist_response(sub, text, origin, meta)  # primeiro o banco, depois a tela
        self._record_metrics(sub, True, origin, meta)
        self._emit(AssistantEvent.RESPONSE, sub.id, text, origin, {**meta, "conversation_id": sub.conversation_id})

    def _fail(self, sub: Submission, err: AIError) -> None:
        sub.state, sub.retryable = SubmissionState.FAILED, err.retryable
        self._mark(sub, "unconfirmed" if err.ambiguous else "failed", err.kind)  # timeout: a API pode ter processado
        self._record_metrics(sub, False, None, {}, err)
        log.info("[AI] Submissão %s falhou: %s", sub.id[:8], err.kind)
        self._emit(AssistantEvent.FAILED, sub.id, err.kind, err.message, err.retryable, err.ambiguous)


    # --- controle do computador (Etapa 7) ---
    def _handle_action(self, sub: Submission) -> bool:
        """True = a mensagem foi tratada aqui (pedido de ação ou resposta a uma confirmação pendente). A IA nunca decide nem
        confirma ações: só o texto do USUÁRIO chega aqui, e o ActionService revalida tudo."""
        acts = self.actions
        if acts is None or not acts.enabled:
            return False
        decision = classify_confirmation(sub.text, sub.source)
        if decision and acts.awaiting_decision():  # "sim"/"cancelar" só valem COM uma confirmação pendente (ou recém-expirada)
            self._finish_action(sub, acts.handle_decision(decision))
            return True
        parse = classify_action(sub.text)
        if parse is None:
            for pid, reason in acts.cancel_pending("other_message"):  # qualquer outra conversa invalida a confirmação pendente
                self._emit(AssistantEvent.CONFIRMATION_CLEARED, pid, reason)
            return False
        self._finish_action(sub, acts.handle(parse, sub.source))
        return True

    # --- métricas (só metadados; melhor esforço) ---
    @staticmethod
    def _category(meta: dict) -> str:
        return ("modo" if meta.get("mode_blocked") else "memória" if "memory_op" in meta else "ação" if "action" in meta else "rotina" if "routine" in meta
                else "integração" if "integration" in meta else "resposta local")

    def _record_metrics(self, sub: Submission, ok: bool, origin: str | None, meta: dict, err: AIError | None = None) -> None:
        if self.metrics is None:
            return
        try:
            if ok:
                kind, category = ("ai", None) if origin == "ia" else ("local", self._category(meta))
                self.metrics.record(submission_id=sub.id, kind=kind, ok=True, category=category, source=sub.source, model=meta.get("model"),
                                    input_tokens=meta.get("input_tokens"), output_tokens=meta.get("output_tokens"), duration_s=meta.get("duration_s"),
                                    attempts=sub.attempts)
            else:  # recusada na entrada (grande demais/fila cheia) ou falha local não é chamada à IA
                kind = "local" if err is not None and err.kind in ("memory", "too_long", "busy") else "ai"
                self.metrics.record(submission_id=sub.id, kind=kind, ok=False, category="erro" if kind == "local" else None, source=sub.source,
                                    error_kind=err.kind if err else None, attempts=sub.attempts)
        except Exception:  # noqa: BLE001 - métrica nunca atrapalha o atendimento
            log.exception("[METRICS] falha ao registrar")

    # --- rotinas ---
    def _foreign_blocked(self, sub: Submission) -> bool:
        """Mensagem de uma conversa que já não é a ativa: pedidos de AÇÃO e rotinas NÃO são executados fora do seu momento."""
        is_action = self.actions is not None and self.actions.enabled and classify_action(sub.text) is not None
        is_routine = self.routines is not None and self.routines.match(sub.text) is not None
        if not (is_action or is_routine):
            return False
        self._complete(sub, "Você trocou de conversa antes de este pedido começar, então não o executei. Peça de novo se ainda quiser.", "local",
                       {"api_call": False, "mode_blocked": False})
        return True

    def _handle_routine(self, sub: Submission) -> bool:
        if self.routines is None:
            return False
        match = self.routines.match(sub.text)
        if match is None:
            return False
        self._emit(AssistantEvent.STARTED, sub.id, "local")
        try:
            result = self.routines.run(match.routine.id, sub.source) if match.routine is not None else None
            text = result.text if result is not None else match.message
        except Exception:  # noqa: BLE001
            log.exception("[ROUTINES] falha ao executar")
            result, text = None, "Não consegui executar a rotina agora. O restante do Jarvis continua funcionando."
        if result is not None:
            for pid, reason in result.cleared:
                self._emit(AssistantEvent.CONFIRMATION_CLEARED, pid, reason)
        self._complete(sub, text, "local", {"api_call": False, "routine": result.name if result else "", "routine_status": result.status if result else "refused",
                                            "private": bool(result and result.private)})
        if result is not None and result.pending is not None:  # a confirmação é a da Etapa 7: os botões aparecem depois do texto
            p = result.pending
            self._emit(AssistantEvent.CONFIRMATION, p.id, p.summary, p.ttl_s)
        return True

    def _handle_integration(self, sub: Submission) -> bool:
        """Consultas a agenda/e-mail/notícias/clima/resumo. Só o texto do USUÁRIO é interpretado; o que vem de e-mails, feeds e
        respostas HTTP é dado e nunca é lido como comando. A resposta contém dados privados: não vai ao contexto da IA."""
        if self.integrations is None:
            return False
        cmd = classify_integration(sub.text)
        if cmd is None:
            return False
        self._emit(AssistantEvent.STARTED, sub.id, "local")
        try:
            reply = self.integrations.answer(cmd)
            text = reply.text
        except Exception:  # noqa: BLE001 - integração com defeito nunca derruba o pipeline
            log.exception("[INT] Falha ao responder %s", cmd.kind.name)
            text = "Não consegui consultar isso agora. O restante do Jarvis continua funcionando."
        self._complete(sub, text, "local", {"api_call": False, "integration": cmd.kind.name.lower(), "private": True})
        return True

    def _finish_action(self, sub: Submission, outcome) -> None:
        self._emit(AssistantEvent.STARTED, sub.id, "local")
        for pid, reason in outcome.cleared:
            self._emit(AssistantEvent.CONFIRMATION_CLEARED, pid, reason)
        result = outcome.result
        self._context().add_exchange(sub.text, result.message)
        self._complete(sub, result.message, "local", {"api_call": False, "action": result.kind.value if result.kind else None,
                                                      "action_status": result.status.name})
        if outcome.pending is not None:  # os botões aparecem DEPOIS da pergunta
            p = outcome.pending
            self._emit(AssistantEvent.CONFIRMATION, p.id, p.summary, p.ttl_s)

    # --- persistência (nunca deixa o fluxo cair: falha de banco vira aviso, não resposta falsa) ---
    def _accept(self, sub: Submission) -> bool:
        """Registra a mensagem aceita no histórico antes de qualquer processamento. False = submissão já existente no banco."""
        if self.history is None or self.history.active_id is None:
            return True
        sub.conversation_id = self.history.active_id
        try:
            created = self.history.record_user(sub.conversation_id, sub.id, sub.text, sub.source)
        except DatabaseError as exc:
            log.error("[HIST] Não consegui registrar a mensagem %s: %s", sub.id[:8], exc)
            self._warn_persistence()
            return True  # responde mesmo assim; só não há registro
        if not created:
            log.info("[HIST] Submissão %s já registrada: ignorada", sub.id[:8])
            return False
        sub.persisted, self._persist_warned = True, False
        return True

    def _mark(self, sub: Submission, state: str, error_kind: str | None = None, attempts: int | None = None,
              only_from: tuple[str, ...] | None = None) -> None:
        if self.history is None or not sub.persisted:
            return
        try:
            self.history.mark_state(sub.id, state, error_kind, attempts, only_from)
        except DatabaseError as exc:
            log.error("[HIST] Não consegui gravar o estado '%s' de %s: %s", state, sub.id[:8], exc)
            self._warn_persistence()

    def _persist_response(self, sub: Submission, text: str, origin: str, meta: dict) -> None:
        if self.history is None or not sub.persisted or sub.conversation_id is None:
            return
        try:
            if not self.history.record_response(sub.conversation_id, sub.id, text, origin, meta, sub.attempts):
                log.info("[HIST] Resposta de %s já registrada: ignorada", sub.id[:8])
        except IntegrityViolation:
            log.info("[HIST] A conversa de %s foi excluída antes da resposta chegar; resposta descartada", sub.id[:8])
        except DatabaseError as exc:
            log.error("[HIST] Não consegui registrar a resposta de %s: %s", sub.id[:8], exc)
            self._warn_persistence()

    def _warn_persistence(self) -> None:
        if not self._persist_warned:  # um aviso por sequência de falhas, não um por mensagem
            self._persist_warned = True
            self._emit(AssistantEvent.NOTICE, "persistence",
                       "Não consegui salvar no histórico. A conversa continua, mas esta parte pode não existir depois de reiniciar.")

    def _memory_block(self, text: str, base_system: str) -> tuple[str, int]:
        if self.memory is None or not self.memory.available:
            return "", 0
        est = lambda t: estimate_tokens(t, self.cfg.chars_per_token)
        room = self.cfg.context_token_budget - est(base_system) - est(text)  # a memória nunca estoura o orçamento total
        try:
            block, n = self.memory.context_block(text, est, room)
        except DatabaseError as exc:
            log.warning("[MEM] Memória indisponível nesta requisição: %s", exc)
            return "", 0
        if n:
            log.info("[MEM] %d memória(s) selecionada(s) localmente para esta requisição", n)
        return block, n


class AssistantService(QObject):
    """Roda o AssistantPipeline numa thread própria e expõe os eventos como sinais Qt."""

    processing_started = Signal(str, str)  # (id, rota: "local" | "ia")
    response_ready = Signal(str, str, str, object)  # (id, texto, origem, métricas)
    failed = Signal(str, str, str, bool, bool)  # (id, tipo, mensagem, pode_tentar_de_novo, ambígua)
    notice = Signal(str, str)  # (tipo, mensagem): aviso técnico, fora da conversa
    confirmation_requested = Signal(str, str, float)  # (id, resumo da ação, prazo em segundos)
    confirmation_cleared = Signal(str, str)  # (id, motivo)
    action_result = Signal(str, str, str)  # (id, texto, status): resultado de uma ação decidida pelos botões

    def __init__(self, cfg: AIConfig, ai: AIService, registry: CapabilityRegistry, parent=None,
                 history: HistoryService | None = None, memory: MemoryService | None = None,
                 memory_cfg: MemoryConfig | None = None, actions: ActionService | None = None, integrations=None,
                 routines=None, metrics=None, mode_provider=None) -> None:
        super().__init__(parent)
        self.config, self._thread = cfg, None
        self._pipeline = AssistantPipeline(cfg, ai, registry, self._dispatch, history=history, memory=memory, memory_cfg=memory_cfg,
                                           actions=actions, integrations=integrations, routines=routines, metrics=metrics, mode_provider=mode_provider)

    def _dispatch(self, event: AssistantEvent, *payload) -> None:
        {AssistantEvent.STARTED: self.processing_started, AssistantEvent.RESPONSE: self.response_ready,
         AssistantEvent.FAILED: self.failed, AssistantEvent.NOTICE: self.notice,
         AssistantEvent.CONFIRMATION: self.confirmation_requested, AssistantEvent.CONFIRMATION_CLEARED: self.confirmation_cleared,
         AssistantEvent.ACTION_RESULT: self.action_result}[event].emit(*payload)

    @property
    def stats(self) -> dict:
        return dict(self._pipeline.stats)

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._pipeline.run, name="jarvis-ai", daemon=True)
            self._thread.start()

    def submit(self, text: str, source: str = "texto", submission_id: str | None = None) -> str | None:
        return self._pipeline.submit(text, source, submission_id)

    def retry(self, submission_id: str) -> bool:
        return self._pipeline.retry(submission_id)

    def reset_conversation(self) -> None:
        self._pipeline.reset_conversation()

    def restore_context(self) -> int:
        return self._pipeline.restore_context()

    def apply_ai_config(self, cfg: AIConfig) -> None:
        self.config = cfg
        self._pipeline.apply_ai_config(cfg)

    def confirm_action(self, pending_id: str) -> None:
        self._pipeline.resolve_confirmation(pending_id, True)

    def cancel_action(self, pending_id: str) -> None:
        self._pipeline.resolve_confirmation(pending_id, False)

    def expire_action(self, pending_id: str) -> None:
        self._pipeline.expire_confirmation(pending_id)

    def activate_conversation(self, conversation_id: str) -> int:
        return self._pipeline.activate_conversation(conversation_id)

    def shutdown(self, timeout: float = 2.0) -> None:
        self._pipeline.stop()
        if self._thread is not None:
            self._thread.join(timeout)  # uma requisição HTTP em andamento é abandonada (thread daemon)
        self._pipeline.finalize()  # o banco só fecha depois (main.py): o que ficou em andamento é gravado como não confirmado
