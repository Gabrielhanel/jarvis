"""Estados do Jarvis compartilhados pela interface (sem dependência do Qt)."""
from __future__ import annotations

from enum import Enum


class JarvisState(Enum):
    IDLE = "Pronto"
    LISTENING = "Ouvindo"
    THINKING = "Processando"
    SPEAKING = "Falando"
    ERROR = "Erro"
    OFFLINE = "Offline"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def coerce(cls, value: "JarvisState | str") -> "JarvisState":
        """Aceita o Enum ou o nome ("LISTENING"), como em orb.set_state("LISTENING")."""
        if isinstance(value, cls):
            return value
        try:
            return cls[str(value).upper()]
        except KeyError:
            raise ValueError(f"Estado inválido: {value!r}. Use um de {[s.name for s in cls]}") from None
