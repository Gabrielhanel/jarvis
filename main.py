"""Ponto de entrada do Jarvis desktop. Só monta as peças; a lógica fica em app/."""
import logging
import sys

if sys.version_info < (3, 12):
    sys.exit("O Jarvis exige Python 3.12 ou superior.")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.core.actions import ActionKind
from app.core.capabilities import Capability, CapabilityRegistry
from app.core.config import Config
from app.database import Database, DatabaseError
from app.database.repositories import ConversationRepository, MemoryRepository
from app.integrations.cache import IntegrationCache
from app.core.modes import effective_ai_config, profile as mode_profile
from app.integrations.config import ai_allowed_by_mode
from app.integrations.manager import IntegrationManager
from app.integrations.secrets import KeyringSecretStore
from app.services.action_service import ActionService
from app.services.ai_service import AIService
from app.services.assistant_service import AssistantService
from app.services.history_service import HistoryService
from app.services.hotkey_service import HotkeyService, HotkeyState, detect_backend
from app.services.metrics_service import MetricsService
from app.services.routine_service import RoutineRepository, RoutineService
from app.services.settings_service import SettingsService
from app.services.integration_service import IntegrationService
from app.services.memory_service import MemoryService
from app.services.tts_service import TTSService
from app.services.voice_service import VoiceService
from app.ui.demo import install_demo
from app.ui.assistant_bridge import AssistantBridge
from app.ui.components import MessageRole
from app.ui.conversation_controller import ConversationController
from app.ui.history_restore import restore_history
from app.ui.hotkey_bridge import HotkeyBridge
from app.ui.metrics_dialog import MetricsDialog
from app.ui.routines_dialog import RoutinesDialog
from app.ui.settings_dialog import SettingsDialog
from app.ui.integrations_bridge import IntegrationsBridge
from app.ui.main_window import MainWindow
from app.services.system_backend import SystemBackend
from app.ui.speech_bridge import SpeechBridge
from app.ui.voice_bridge import VoiceBridge

log = logging.getLogger("jarvis")


def open_persistence(config: Config, restore_last: bool = True):
    """Abre o banco, recupera a conversa ativa e prepara a memória. Falha de banco NÃO derruba o aplicativo:
    devolve o que for possível e uma mensagem honesta para o usuário."""
    try:
        database = Database(config.db_path).open()
    except DatabaseError as exc:
        log.error("[DB] %s", exc)
        return None, None, None, f"Persistência indisponível: {exc} Nada será salvo nesta sessão."
    memory_cfg = config.memory_config()
    memory = MemoryService(MemoryRepository(database), memory_cfg) if memory_cfg.enabled else None
    history = HistoryService(ConversationRepository(database))
    try:
        history.mark_interrupted_on_start()  # o que ficou sem resposta é marcado; NADA é reenviado
        history.ensure_active()
        if not restore_last:  # preferência: começar numa conversa nova (reaproveita a ativa se ela já estiver vazia)
            history.new_conversation()
    except DatabaseError as exc:
        log.error("[HIST] %s", exc)
        return database, None, memory, f"Histórico indisponível: {exc} As conversas não serão salvas nesta sessão."
    return database, history, memory, None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.load()
    settings = SettingsService(config)  # preferências validadas (mesmo settings.json de sempre)
    settings.sanitize()  # valor salvo inválido -> padrão seguro, registrado sem expor o valor
    database, history, memory, persistence_problem = open_persistence(config, settings.value("history.restore_on_start"))
    app = QApplication(sys.argv)

    window = MainWindow(config)
    window.set_sidebar_visible(settings.value("ui.sidebar_visible"))
    restored = 0
    if history is not None and history.active_id is not None:
        try:
            restored = restore_history(window, history.load_messages(history.active_id, config.memory_config().restore_messages))
        except DatabaseError as exc:
            log.error("[HIST] Não consegui carregar o histórico: %s", exc)
            persistence_problem = f"Não consegui carregar o histórico: {exc}"
    install_demo(window, show_hint=restored == 0)  # provisório da Etapa 2: só visual, nunca gravado
    if persistence_problem:
        window.add_message(MessageRole.SYSTEM, persistence_problem)
        window.set_status_message("Banco indisponível")
    elif database is not None:
        window.set_status_message(f"Banco: {database.path.name} (v{database.schema_version})")
    voice = VoiceService(config.voice_config())
    voice_bridge = VoiceBridge(window, voice)
    tts = TTSService(config.tts_config())
    SpeechBridge(window, tts, voice, voice_bridge)  # fala <-> orb <-> microfone
    registry = CapabilityRegistry()
    registry.set_available(Capability.MEMORY, memory is not None and memory.available)
    actions_cfg = config.actions_config()  # padrões + data/actions.json (lista de permissões do controle do computador)
    actions = ActionService(actions_cfg) if actions_cfg.enabled else None
    if actions is not None:
        kinds = actions.supported_kinds()  # o que ESTE sistema consegue de fato
        registry.set_available(Capability.OPEN_APPS, ActionKind.OPEN_APP in kinds)
        registry.set_available(Capability.CLOSE_APPS, ActionKind.CLOSE_APP in kinds)
        registry.set_available(Capability.OPEN_PLACES, bool(kinds & {ActionKind.OPEN_FOLDER, ActionKind.OPEN_PROJECT, ActionKind.OPEN_URL}))
        registry.set_available(Capability.SPOTIFY_SEARCH, ActionKind.SPOTIFY_SEARCH in kinds)
    for problem in actions_cfg.errors:
        window.add_message(MessageRole.SYSTEM, f"Configuração de ações: {problem}")
    mode_now = lambda: settings.value("mode")  # noqa: E731 - lido a cada uso: trocar de modo vale na hora
    ai_config = effective_ai_config(config.ai_config(), mode_now())  # as escolhas do usuário + os tetos do modo (nunca troca o modelo)
    ai = AIService(ai_config, key_provider=lambda: config.api_key)
    metrics = MetricsService(database, prices=lambda: (config.get("metrics", {}) or {}).get("prices", {})) if database is not None else None
    window.set_header_info(mode_now(), ai_config.model)  # a chave nunca é guardada: lida do .env/ambiente a cada uso
    integrations = integration_service = None
    if database is not None:  # o cache das integrações mora no SQLite; sem banco elas ficam desligadas (com aviso), o resto segue
        integrations = IntegrationManager(config.integrations_config(), config.save_integrations, KeyringSecretStore(), IntegrationCache(database),
                                          ai=ai, ai_allowed=lambda: ai_allowed_by_mode(mode_now()), memory=memory,
                                          url_opener=(actions.backend if actions is not None else SystemBackend()).open_url,
                                          network_allowed=lambda: mode_profile(mode_now()).network, metrics=metrics)
        for cap in (Capability.CALENDAR, Capability.EMAIL, Capability.NEWS, Capability.WEATHER):
            registry.set_available(cap, True)  # o recurso existe; se falta configuração, a resposta explica como configurar
        integration_service = IntegrationService(integrations, app)
    else:
        window.add_message(MessageRole.SYSTEM, "Agenda, e-mails, notícias e clima estão indisponíveis porque o banco de dados não abriu.")
    routine_service = RoutineService(RoutineRepository(database), actions, integrations) if database is not None else None
    assistant = AssistantService(ai_config, ai, registry, history=history, memory=memory, memory_cfg=config.memory_config(),
                                 actions=actions, integrations=integrations, routines=routine_service, metrics=metrics, mode_provider=mode_now)
    integrations_bridge = IntegrationsBridge(window, integration_service, tts) if integration_service is not None else None
    assistant_bridge = AssistantBridge(window, assistant, registry, tts, voice, voice_bridge, speak=ai_config.speak_responses)
    if history is not None:
        ConversationController(window, window.sidebar, history, assistant, assistant_bridge, config.memory_config().restore_messages)
    else:
        window.sidebar.setVisible(False)  # sem banco não há histórico para listar

    # --- atalho global, janelas de configuração e aplicação das preferências ao vivo ---
    hotkey_service = HotkeyService(detect_backend(), lambda: None)
    hotkey_bridge = HotkeyBridge(window, hotkey_service, voice, tts, ai_busy=lambda: bool(getattr(window, "ai_busy", False)))
    dialogs: dict[str, object] = {}

    def show(name, factory):
        if name not in dialogs:
            dialogs[name] = factory()
        dialogs[name].show()
        dialogs[name].raise_()
        dialogs[name].activateWindow()

    def open_routines():
        if routine_service is None:
            window.set_status_message("Rotinas indisponíveis: o banco de dados não abriu.")
            return
        def make():
            d = RoutinesDialog(routine_service, lambda: settings.value("routines.confirm_before_run"), window)
            d.run_requested.connect(lambda name: window.submit_text(f"Execute a rotina {name}", source="texto"))  # mesmo caminho de uma mensagem do usuário
            return d
        show("routines", make)

    def open_metrics():
        if metrics is None:
            window.set_status_message("Métricas indisponíveis: o banco de dados não abriu.")
            return
        show("metrics", lambda: MetricsDialog(metrics, settings.value("metrics.default_period"), window))

    def open_settings():
        show("settings", lambda: SettingsDialog(settings, window, ai_configured=lambda: ai.is_configured, hotkey_status=hotkey_bridge.status_text, integrations=integrations,
                                                openers={"routines": open_routines, "metrics": open_metrics, **({"panels": integrations_bridge._show_panels} if integrations_bridge else {})}))

    window.settings_clicked.connect(open_settings)

    def apply_ai(_key, _value):
        cfg = effective_ai_config(config.ai_config(), mode_now())
        assistant.apply_ai_config(cfg)
        window.set_header_info(mode_now(), cfg.model)

    settings.subscribe("mode", apply_ai)
    settings.subscribe("ai.model", apply_ai)
    settings.subscribe("ai.max_output_tokens", apply_ai)
    settings.subscribe("ai.speak_responses", lambda _k, v: assistant_bridge.set_speak(v))
    settings.subscribe("tts.volume", lambda _k, v: tts.set_volume(v))
    settings.subscribe("tts.rate", lambda _k, v: tts.set_rate(v))
    settings.subscribe("shortcut.", lambda _k, _v: hotkey_bridge.apply(settings.value("shortcut.enabled"), settings.value("shortcut.combo")))
    settings.subscribe("ui.sidebar_visible", lambda _k, v: window.set_sidebar_visible(v))
    assistant.restore_context()  # contexto reconstruído do banco; nenhuma chamada à IA
    # Ordem de encerramento: primeiro os serviços (que podem gravar), por último o banco.
    app.aboutToQuit.connect(hotkey_bridge.shutdown)  # libera o atalho global primeiro
    app.aboutToQuit.connect(assistant.shutdown)
    if integration_service is not None:
        app.aboutToQuit.connect(integration_service.shutdown)
    app.aboutToQuit.connect(tts.shutdown)
    app.aboutToQuit.connect(voice.shutdown)
    if database is not None:
        app.aboutToQuit.connect(database.close)
    assistant.start()  # só inicia a thread: nenhuma chamada à API acontece sem uma mensagem do usuário
    if not ai.is_configured:
        window.add_message(MessageRole.SYSTEM, "A IA ainda não está configurada: defina ANTHROPIC_API_KEY no arquivo .env "
                                               "(veja .env.example). Comandos locais, memória e voz continuam disponíveis.")
    window.show()
    hk = hotkey_bridge.apply(settings.value("shortcut.enabled"), settings.value("shortcut.combo"))
    if settings.value("shortcut.enabled") and hk.state is not HotkeyState.ACTIVE:
        window.set_status_message(hotkey_bridge.status_text())  # dito com honestidade; o resto do Jarvis segue normal
    if integration_service is not None:
        integration_service.start()  # primeira atualização só depois da janela abrir; a interface nunca espera a rede
        QTimer.singleShot(8000, integration_service.maybe_auto_morning)  # só age se o usuário ligou o resumo automático
    if voice.config.enabled:
        voice.start()  # microfone e modelo carregam na thread de voz
    if tts.config.enabled:
        tts.start()  # o modelo de voz carrega em segundo plano
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
