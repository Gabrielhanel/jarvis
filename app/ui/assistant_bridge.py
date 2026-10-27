"""Liga o AssistantService à interface, ao TTS e ao registro de capacidades. A UI não conhece o SDK da Anthropic."""
from __future__ import annotations

from PySide6.QtCore import QObject, Slot

from app.core.capabilities import Capability, CapabilityRegistry
from app.services.assistant_service import AssistantService
from app.services.tts_service import TTSService
from app.services.voice_service import VoiceService
from app.ui.components import MessageRole
from app.ui.main_window import MainWindow
from app.ui.states import JarvisState
from app.ui.voice_bridge import VoiceBridge


class AssistantBridge(QObject):
    def __init__(self, window: MainWindow, assistant: AssistantService, registry: CapabilityRegistry,
                 tts: TTSService | None = None, voice: VoiceService | None = None,
                 voice_bridge: VoiceBridge | None = None, speak: bool = True) -> None:
        super().__init__(window)
        self._window, self._assistant, self._registry = window, assistant, registry
        self._tts, self._voice_bridge, self._speak = tts, voice_bridge, speak
        self._spoken: set[str] = set()  # uma resposta é falada uma única vez
        self._failed_id: str | None = None
        window.message_submitted.connect(self._on_message)
        window.retry_requested.connect(self._on_retry)
        assistant.processing_started.connect(self._on_started)
        assistant.response_ready.connect(self._on_response)
        assistant.failed.connect(self._on_failed)
        assistant.notice.connect(self._on_notice)
        self._sub_conv: dict[str, str | None] = {}
        assistant.confirmation_requested.connect(self._on_confirmation)
        assistant.confirmation_cleared.connect(self._on_confirmation_cleared)
        assistant.action_result.connect(self._on_action_result)
        window.action_confirmed.connect(assistant.confirm_action)  # a UI só repassa o ID; quem valida e executa é o serviço
        window.action_cancelled.connect(assistant.cancel_action)
        window.action_expired.connect(assistant.expire_action)
        if voice is not None:  # capacidades dependem do estado REAL dos serviços
            voice.model_ready.connect(self._voice_in_on)
            voice.stt_unavailable.connect(self._voice_in_off)
            voice.microphone_unavailable.connect(self._voice_in_off)
        if tts is not None:
            tts.model_ready.connect(self._voice_out_on)
            tts.unavailable.connect(self._voice_out_off)

    @Slot()
    def _voice_in_on(self) -> None: self._registry.set_available(Capability.VOICE_INPUT, True)
    @Slot(str)
    def _voice_in_off(self, _msg: str) -> None: self._registry.set_available(Capability.VOICE_INPUT, False)
    @Slot()
    def _voice_out_on(self) -> None: self._registry.set_available(Capability.VOICE_OUTPUT, True)
    @Slot(str)
    def _voice_out_off(self, _msg: str) -> None: self._registry.set_available(Capability.VOICE_OUTPUT, False)

    @Slot(str, str, str)
    def _on_message(self, submission_id: str, text: str, source: str) -> None:
        self._sub_conv[submission_id] = self._window.current_conversation_id  # a requisição fica ligada à conversa onde nasceu
        if len(self._sub_conv) > 512:
            self._sub_conv.pop(next(iter(self._sub_conv)))
        self._window.show_retry(False)
        self._failed_id = None
        if self._tts is not None:
            self._tts.stop()  # uma nova mensagem interrompe a fala anterior
        self._assistant.submit(text, source, submission_id)

    @Slot(str, str)
    def _on_started(self, _sid: str, route: str) -> None:
        if not self._is_current(_sid):
            return
        if route == "ia":
            self._window.ai_busy = True
            self._window.set_state(JarvisState.THINKING)
            self._window.set_status_message("Pensando...")

    @Slot(str, str, str, object)
    def _on_response(self, sid: str, text: str, origin: str, meta: dict) -> None:
        if not self._is_current(sid) or (meta or {}).get("conversation_id", self._window.current_conversation_id) != self._window.current_conversation_id:
            return  # resposta de OUTRA conversa: já foi gravada lá; não aparece (nem é falada) nesta
        self._finish_processing()
        self._window.add_message(MessageRole.JARVIS, text)
        if origin == "ia":
            note = " (resposta cortada pelo limite de saída)" if meta.get("truncated") else ""
            self._window.set_status_message(f"IA: {meta.get('input_tokens')} tokens de entrada, "
                                            f"{meta.get('output_tokens')} de saída, {meta.get('duration_s', 0):.1f}s{note}")
        else:
            self._window.set_status_message("Resposta local (sem chamada à IA)")
        if self._speak and self._tts is not None and self._tts.config.enabled and sid not in self._spoken:
            self._spoken.add(sid)
            self._tts.speak(text)  # se o TTS falhar, a resposta escrita já está na tela

    @Slot(str, str, str, bool, bool)
    def _on_failed(self, sid: str, kind: str, message: str, retryable: bool, _ambiguous: bool) -> None:
        if not self._is_current(sid):
            return
        self._finish_processing()
        self._window.add_message(MessageRole.SYSTEM, message)
        self._window.set_status_message(message)
        if retryable:
            self._failed_id = sid
            self._window.show_retry(True)

    def _is_current(self, sid: str) -> bool:
        return self._sub_conv.get(sid, self._window.current_conversation_id) == self._window.current_conversation_id

    def set_speak(self, value: bool) -> None:
        """Preferência "falar as respostas": vale na hora, sem reiniciar."""
        self._speak = bool(value)

    def reset_for_conversation_switch(self) -> None:
        """Trocou de conversa: o estado de "pensando" da anterior não pode ficar preso na tela da nova."""
        self._window.ai_busy = False
        self._window.set_state(JarvisState.IDLE)
        self._window.set_status_message("Pronto")

    @Slot(str, str)
    def _on_notice(self, _kind: str, message: str) -> None:
        self._window.add_message(MessageRole.SYSTEM, message)  # aviso técnico, só visual (não entra no histórico)
        self._window.set_status_message(message)

    @Slot(str, str, float)
    def _on_confirmation(self, pending_id: str, summary: str, ttl_s: float) -> None:
        self._window.show_confirmation(pending_id, summary, ttl_s)
        self._window.set_status_message("Aguardando sua confirmação")

    @Slot(str, str)
    def _on_confirmation_cleared(self, pending_id: str, _reason: str) -> None:
        self._window.hide_confirmation(pending_id)

    @Slot(str, str, str)
    def _on_action_result(self, pending_id: str, text: str, _status: str) -> None:
        """Resultado de uma decisão tomada pelos botões. Aparece como fala do Jarvis, mas NÃO vai para o histórico (não é uma
        troca de mensagens: não houve mensagem do usuário)."""
        self._window.hide_confirmation(pending_id)
        self._window.add_message(MessageRole.JARVIS, text)
        self._window.set_status_message("Ação local (sem chamada à IA)")
        key = f"action:{pending_id}"
        if self._speak and self._tts is not None and self._tts.config.enabled and key not in self._spoken:
            self._spoken.add(key)
            self._tts.speak(text)

    @Slot()
    def _on_retry(self) -> None:
        if self._failed_id and self._assistant.retry(self._failed_id):
            self._window.show_retry(False)
            self._failed_id = None

    def _finish_processing(self) -> None:
        self._window.ai_busy = False
        if self._window.state is JarvisState.THINKING:
            self._window.set_state(JarvisState.IDLE)
        if self._voice_bridge is not None:
            self._voice_bridge.refresh()
