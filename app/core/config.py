"""Configuração: caminhos, preferências (data/settings.json) e leitura de segredos.

A API key NUNCA é gravada no código nem no settings.json. Ela vem da variável de
ambiente ANTHROPIC_API_KEY ou do arquivo .env (ignorado pelo Git).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env"
MODELS_DIR = BASE_DIR / "assets" / "models"
MODES = ("ECONÔMICO", "EQUILIBRADO", "MÁXIMO")


def _defaults() -> dict[str, Any]:
    return {
        "model": "claude-sonnet-5-5",
        "mode": "EQUILIBRADO",
        "voice": {"profile": "MORDOMO CLÁSSICO", "rate": 1.0, "pitch": 1.0, "pause_ms": 240},
        "ui": {"accent": "#38d0ff"},
        "ai": {},  # sobrescreve campos de AIConfig (ex.: {"model": "claude-sonnet-5-5"})
        "tts": {},  # sobrescreve campos de TTSConfig (ex.: {"voice": "pm_santa", "volume": 0.7})
        "listening": {},  # sobrescreve campos de VoiceConfig (ex.: {"sensitivity": 0.7})
        "memory": {},  # sobrescreve campos de MemoryConfig (ex.: {"max_items": 3})
        "integrations": {},  # agenda, e-mail, notícias, clima (SEM credenciais: elas ficam no cofre do sistema)
    }


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class VoiceConfig:
    """Entrada de voz (Etapa 3). Tempos em ms/s; sensibilidade de 0 (rígido) a 1 (sensível)."""

    enabled: bool = True
    device: int | str | None = None  # None = microfone padrão; int = índice; str = parte do nome
    sample_rate: int = 16000
    frame_ms: int = 30
    input_gain: float = 1.0  # volume de entrada (amplitude)
    sensitivity: float = 0.5
    min_level_db: float = -60.0  # nível mínimo (dB relativo) para considerar fala
    speech_band_ratio: float = 0.30  # fração mínima da energia em 200-3400 Hz (rejeita hum grave)
    min_speech_ms: int = 210  # duração mínima para a fala começar (ignora toques de tecla)
    silence_ms: int = 800  # silêncio que encerra a fala (tolera pausas curtas)
    preroll_ms: int = 300  # áudio guardado antes do início detectado
    wake_window_s: float = 6.0  # maior trecho analisado em busca da wake word
    command_timeout_s: float = 8.0  # espera pelo comando depois da wake word
    max_utterance_s: float = 15.0
    stall_timeout_s: float = 3.0  # sem áudio por este tempo = dispositivo perdido
    retry_s: float = 3.0  # intervalo para tentar reabrir o microfone
    wake_allow_name_only: bool = False  # aceitar só "Jarvis" (sem "Ei")
    language: str = "pt"
    stt_model: str = "small"  # tiny | base | small | medium. small: mais robusto que base (ver README); use "base" se a CPU for lenta
    stt_prompt: str | None = None  # opcional: vocabulário para viesar o decodificador (ex.: "Jarvis, Spotify, Visual Studio Code.")
    stt_model_dir: str | None = None  # None = assets/models/faster-whisper-<stt_model>
    stt_beam_size: int = 5  # padrão do faster-whisper; comandos curtos se beneficiam de mais hipóteses
    tail_keep_ms: int = 300  # silêncio final mantido ao enviar o trecho ao STT (o resto é descartado)
    debug: bool = False  # diagnóstico do STT no log (níveis, duração, texto, tempos); NUNCA grava áudio
    debug_audio_dir: str | None = None  # opt-in: salva os últimos trechos .wav aqui (ferramenta de desenvolvimento)
    stt_threads: int = 0  # 0 = padrão do CTranslate2

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def start_frames(self) -> int:
        return max(1, -(-self.min_speech_ms // self.frame_ms))

    @property
    def end_frames(self) -> int:
        return max(1, -(-self.silence_ms // self.frame_ms))

    @property
    def preroll_frames(self) -> int:
        return max(0, self.preroll_ms // self.frame_ms)

    @property
    def tail_keep_frames(self) -> int:
        return max(0, self.tail_keep_ms // self.frame_ms)

    @property
    def stt_model_path(self) -> Path:
        path = Path(self.stt_model_dir) if self.stt_model_dir else MODELS_DIR / f"faster-whisper-{self.stt_model}"
        return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class TTSConfig:
    """Saída de voz (Etapa 4, Kokoro/ONNX local). `rate` multiplica a velocidade natural; `volume` vai de 0 a 1."""

    enabled: bool = True
    model_path: str | None = None  # None = assets/models/kokoro/kokoro-v1.0.int8.onnx
    voices_path: str | None = None  # None = assets/models/kokoro/voices-v1.0.bin
    voice: str = "pm_alex"  # pt-BR: pm_alex, pm_santa (masculinas), pf_dora (feminina)
    lang: str = "pt-br"
    rate: float = 0.95  # ritmo moderado
    volume: float = 0.9
    pause_ms: int = 220  # silêncio entre trechos (o Kokoro corta o silêncio das pontas)
    stream_playback: bool = True  # False = sintetiza tudo antes de falar (evita pausas em CPUs lentas)
    max_segment_chars: int = 120  # trechos curtos: o 1º áudio sai antes e parar/encerrar é mais rápido
    max_text_chars: int = 3000
    max_queue: int = 8
    output_device: int | str | None = None
    resume_holdoff_ms: int = 600  # tempo extra sem ouvir depois da fala (eco da sala)
    test_phrase: str = "Olá, senhor Gabriel. Estou pronto para ajudar. O sistema de voz está funcionando corretamente."

    @property
    def model_file(self) -> Path:
        return _resolve(self.model_path, MODELS_DIR / "kokoro" / "kokoro-v1.0.int8.onnx")

    @property
    def voices_file(self) -> Path:
        return _resolve(self.voices_path, MODELS_DIR / "kokoro" / "voices-v1.0.bin")


def _resolve(value: str | None, default: Path) -> Path:
    path = Path(value) if value else default
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class AIConfig:
    """Integração com a Anthropic (Etapa 5). Tudo que afeta custo e comportamento fica aqui."""

    enabled: bool = True
    model: str = "claude-haiku-5-5"  # o mais barato da documentação oficial; troque em data/settings.json (chave "ai")
    max_output_tokens: int = 400  # respostas curtas (também são lidas em voz alta)
    context_token_budget: int = 3000  # entrada total ESTIMADA: sistema + histórico + mensagem atual
    max_history_messages: int = 12  # 6 trocas
    max_message_chars: int = 4000
    chars_per_token: float = 2.5  # estimativa conservadora (o tokenizador do Haiku 5.5 gera ~30% mais tokens que o do 4.5)
    timeout_s: float = 30.0
    effort: str | None = "low"  # output_config.effort (modelos que suportam); None = não enviar
    disable_thinking: bool = True  # thinking={"type": "disabled"}: pensar gasta tokens de saída; use False em modelos que exigem pensamento
    max_pending: int = 4  # mensagens aguardando na fila (processamento é serial)
    max_manual_retries: int = 3  # tentativas explícitas pelo usuário por mensagem
    speak_responses: bool = True  # fala as respostas com o TTS, quando disponível


@dataclass(frozen=True)
class MemoryConfig:
    """Histórico persistente e memória permanente (Etapa 6). Os limites de memória são INDEPENDENTES do orçamento da conversa,
    mas o que é enviado à IA (sistema + memórias + histórico + mensagem) continua dentro de `AIConfig.context_token_budget`."""

    enabled: bool = True
    max_items: int = 4  # memórias enviadas por requisição, no máximo
    token_budget: int = 400  # orçamento ESTIMADO só para as memórias (inclui o cabeçalho do bloco)
    max_item_chars: int = 300  # cada memória é cortada neste tamanho ao ser enviada
    min_score: int = 2  # relevância mínima (título = 3 pontos, palavra no conteúdo = 1)
    style_max: int = 2  # preferências de comunicação ("respostas curtas") sempre elegíveis, no máximo este número
    max_content_chars: int = 500  # tamanho máximo de uma memória
    max_memories: int = 500  # total de memórias guardadas
    similar_threshold: float = 0.5  # parecida com outra existente: pede esclarecimento em vez de duplicar
    duplicate_threshold: float = 0.9  # praticamente igual: não grava de novo
    history_turns: int = 20  # trocas COMPLETAS lidas do banco ao reconstruir o contexto (o orçamento ainda corta)
    restore_messages: int = 100  # mensagens mostradas na tela ao reiniciar


class Config:
    """Preferências com acesso por caminho, ex.: config.get("voice.rate")."""

    def __init__(self, data_dir: Path = DATA_DIR, env_file: Path = ENV_FILE) -> None:
        self.data_dir = Path(data_dir)
        self.env_file = Path(env_file)
        self.settings_path = self.data_dir / "settings.json"
        self.db_path = self.data_dir / "jarvis.db"
        self._settings: dict[str, Any] = _defaults()

    @classmethod
    def load(cls, **kwargs: Any) -> "Config":
        config = cls(**kwargs)
        config.reload()
        return config

    def reload(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            stored = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            stored = {}
        self._settings = _merge(_defaults(), stored if isinstance(stored, dict) else {})

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(self._settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._settings
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        *parents, last = path.split(".")
        node = self._settings
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value

    def voice_config(self) -> VoiceConfig:
        known = {f.name for f in fields(VoiceConfig)}
        return VoiceConfig(**{k: v for k, v in self.get("listening", {}).items() if k in known})

    def tts_config(self) -> TTSConfig:
        known = {f.name for f in fields(TTSConfig)}
        return TTSConfig(**{k: v for k, v in self.get("tts", {}).items() if k in known})

    def ai_config(self) -> AIConfig:
        known = {f.name for f in fields(AIConfig)}
        return AIConfig(**{k: v for k, v in self.get("ai", {}).items() if k in known})

    def memory_config(self) -> MemoryConfig:
        known = {f.name for f in fields(MemoryConfig)}
        return MemoryConfig(**{k: v for k, v in self.get("memory", {}).items() if k in known})

    @property
    def actions_path(self) -> Path:
        return self.data_dir / "actions.json"  # lista de permissões do controle do computador (ignorada pelo Git)

    def actions_config(self):
        """Padrões + data/actions.json. Entradas inválidas são ignoradas e descritas em `.errors`."""
        from app.core.actions import detect_platform, load_actions_config  # import tardio: config.py não depende do controle local
        return load_actions_config(self.actions_path, detect_platform())

    def integrations_config(self):
        from app.integrations.config import parse_integrations  # import tardio: config.py não depende das integrações
        return parse_integrations(self.get("integrations", {}))

    def save_integrations(self, cfg) -> None:
        """Grava só a parte NÃO sensível (nomes, cidade, assuntos, preferências) em data/settings.json."""
        self.set("integrations", cfg.to_dict())
        self.save()

    def save_tts(self, **values: Any) -> None:
        """Persiste preferências de voz (ex.: volume=0.7) em data/settings.json."""
        for key, value in values.items():
            self.set(f"tts.{key}", value)
        self.save()

    @property
    def api_key(self) -> str | None:
        """Lê a chave do ambiente ou do .env. Nunca é salva em settings.json."""
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return key.strip()
        try:
            for line in self.env_file.read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "ANTHROPIC_API_KEY" and value.strip():
                    return value.strip().strip("\"'")
        except FileNotFoundError:
            pass
        return None
