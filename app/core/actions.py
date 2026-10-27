"""Controle seguro do computador (Etapa 7): tipos, lista de permissões e validações PURAS.

Nada aqui executa algo no sistema. A IA nunca cria nem altera uma ação: os pedidos vêm da mensagem do usuário, são
resolvidos contra a lista autorizada (esta configuração) e REVALIDADOS pelo executor antes de qualquer efeito."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, urlsplit

from app.core.textutil import fold, looks_like_secret, norm_alias

log = logging.getLogger("jarvis.actions")


class ActionKind(Enum):
    OPEN_APP = "open_app"
    OPEN_FOLDER = "open_folder"
    OPEN_PROJECT = "open_project"
    OPEN_URL = "open_url"
    SPOTIFY_SEARCH = "spotify_search"
    CLOSE_APP = "close_app"


class ActionStatus(Enum):
    SUCCESS = "sucesso"  # o sistema ACEITOU o pedido (não prova que uma janela apareceu)
    NOT_AUTHORIZED = "ação não autorizada"
    UNKNOWN_TARGET = "destino desconhecido"
    INVALID_PARAM = "parâmetro inválido"
    UNAVAILABLE = "aplicativo ou pasta indisponível"
    CONFIRMATION_REQUIRED = "confirmação necessária"
    CANCELLED = "ação cancelada"
    EXPIRED = "solicitação expirada"
    ERROR = "erro durante a execução"
    UNSUPPORTED = "não suportado neste sistema"
    DUPLICATE = "pedido repetido ignorado"


@dataclass(frozen=True)
class ActionRequest:
    """Uma ação já RESOLVIDA contra a lista autorizada. Não carrega comando, caminho de executável nem argumentos livres:
    `target` é a CHAVE de um destino configurado; `detail` só existe para URL final validada ou texto de pesquisa validado."""
    kind: ActionKind
    target: str
    label: str
    needs_confirmation: bool = False
    detail: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class ActionResult:
    status: ActionStatus
    message: str  # curto, para a tela e para a voz; nunca traz stack trace
    kind: ActionKind | None = None
    target: str = ""
    request_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ActionStatus.SUCCESS


@dataclass(frozen=True)
class PendingConfirmation:
    id: str
    request: ActionRequest
    question: str  # o que o Jarvis pergunta (tela e voz)
    summary: str  # a ação exata e o destino, mostrados ao lado dos botões
    ttl_s: float
    expires_at: float  # relógio monotônico do ConfirmationManager


@dataclass(frozen=True)
class ActionOutcome:
    result: ActionResult
    pending: PendingConfirmation | None = None
    cleared: tuple[tuple[str, str], ...] = ()  # (id de confirmação encerrada, motivo): a UI esconde os botões


# --- lista de permissões ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AppSpec:
    key: str
    label: str
    aliases: tuple[str, ...] = ()
    exe: tuple[str, ...] = ()  # candidatos AUTORIZADOS (caminho ou nome no PATH); o primeiro que existir é usado
    args: tuple[str, ...] = ()  # argumentos FIXOS definidos aqui; nunca vêm do usuário nem da IA
    uri: str = ""  # alternativa: protocolo registrado (ex.: "spotify:"), só se estiver registrado no sistema
    url: str = ""  # navegador padrão: página aberta ao pedir "o navegador"
    default_browser: bool = False
    closable: bool = False
    process: str = ""  # Windows: nome da imagem (ex.: Spotify.exe) usado no pedido de fechamento normal
    wm_class: str = ""  # Linux: classe da janela (wmctrl -x -c)


@dataclass(frozen=True)
class FolderSpec:
    key: str
    label: str
    path: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectSpec:
    key: str
    label: str
    path: str
    aliases: tuple[str, ...] = ()
    app: str = ""  # chave de um AppSpec; vazio = abrir a pasta no gerenciador de arquivos


@dataclass(frozen=True)
class SiteSpec:
    key: str
    label: str
    url: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionsConfig:
    enabled: bool = True
    apps: Mapping[str, AppSpec] = field(default_factory=dict)
    folders: Mapping[str, FolderSpec] = field(default_factory=dict)
    projects: Mapping[str, ProjectSpec] = field(default_factory=dict)
    sites: Mapping[str, SiteSpec] = field(default_factory=dict)
    allowed_roots: tuple[str, ...] = ("~",)  # pastas e projetos precisam resolver para DENTRO de uma destas raízes
    confirm_kinds: frozenset[ActionKind] = frozenset({ActionKind.CLOSE_APP})
    confirmation_ttl_s: float = 30.0
    dedupe_window_s: float = 3.0  # o mesmo pedido repetido neste intervalo (eco de voz/evento duplicado) não executa de novo
    allow_local_urls: bool = False
    errors: tuple[str, ...] = ()  # problemas encontrados em data/actions.json (a entrada inválida é ignorada)

    def find(self, namespace: str, text: str):
        """Procura por alias normalizado (igualdade exata, sem aproximação): destino que não está na lista não existe."""
        wanted = norm_alias(text)
        if not wanted:
            return None
        for spec in getattr(self, namespace).values():
            if wanted == norm_alias(spec.key) or wanted == norm_alias(spec.label) or wanted in {norm_alias(a) for a in spec.aliases}:
                return spec
        return None

    def names(self, namespaces: tuple[str, ...] = ("apps", "folders", "projects", "sites"), limit: int = 8) -> list[str]:
        out = [spec.label for ns in namespaces for spec in getattr(self, ns).values()]
        return out[:limit]


# --- caminhos, URLs e pesquisa --------------------------------------------------------------------------------------
def expand_path(raw: str, env: Mapping[str, str] | None = None, home: Path | None = None) -> str:
    """~ e variáveis (%APPDATA%, $HOME) de forma independente do sistema. Variável desconhecida continua na string
    (e o caminho então não existe)."""
    env = os.environ if env is None else env
    text = re.sub(r"%([A-Za-z0-9_()]+)%", lambda m: env.get(m.group(1), m.group(0)), raw)
    text = re.sub(r"\$(?:\{([A-Za-z0-9_]+)\}|([A-Za-z0-9_]+))", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), text)
    if text == "~" or text.startswith(("~/", "~\\")):
        text = str(home or Path.home()) + text[1:]
    return text


def authorized_dir(raw: str, roots: tuple[str, ...], env: Mapping[str, str] | None = None,
                   home: Path | None = None) -> tuple[Path | None, str]:
    """(pasta resolvida, "") ou (None, motivo). A pasta é resolvida COM links simbólicos e `..` aplicados e só vale se o
    destino FINAL ficar dentro de uma raiz autorizada. Motivos: invalid | missing | not_dir | outside."""
    if not raw or "\0" in raw or any(unicodedata.category(c) == "Cc" for c in raw):
        return None, "invalid"
    path = Path(expand_path(raw, env, home))
    if not path.is_absolute():
        return None, "invalid"  # caminho relativo dependeria do diretório de trabalho
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, RuntimeError):  # laço de links simbólicos, permissão, nome inválido
        return None, "invalid"
    if not resolved.is_dir():
        return None, "not_dir"
    for root in roots:
        try:
            root_path = Path(expand_path(root, env, home)).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == root_path or root_path in resolved.parents:
            return resolved, ""
    return None, "outside"


_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_URL_FORBIDDEN = re.compile(r"[\s<>\"\\`]")
_ALLOWED_SCHEMES = ("http", "https")


def validate_url(raw: str, allow_local: bool = False) -> tuple[str, str]:
    """(url, "") ou ("", motivo). Só http/https, sem credenciais embutidas, sem espaço/controle, host válido; segredos recusados.
    Motivos: empty | long | chars | scheme | malformed | credentials | host | local | secret."""
    url = (raw or "").strip()
    if not url:
        return "", "empty"
    if len(url) > 2048:
        return "", "long"
    if _URL_FORBIDDEN.search(url) or any(unicodedata.category(c) == "Cc" for c in url):
        return "", "chars"
    try:
        parts = urlsplit(url)
        port = parts.port  # ValueError se inválida
    except ValueError:
        return "", "malformed"
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return "", "scheme"
    if not parts.netloc or not parts.hostname:
        return "", "malformed"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "", "credentials"
    if port is not None and not 1 <= port <= 65535:
        return "", "malformed"
    if looks_like_secret(url):  # depois das checagens estruturais, para o motivo ser o mais específico
        return "", "secret"
    host = parts.hostname.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not allow_local and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
            return "", "local"
        return url, ""
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return "", "host"
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or not all(_HOST_LABEL.fullmatch(lab) for lab in labels):
        return "", "host"
    if not allow_local and (len(labels) < 2 or labels[-1] in {"local", "localhost", "internal", "lan", "home", "corp"}):
        return "", "local"  # "localhost" e nomes de rede interna não são sites públicos
    return url, ""


SPOTIFY_SEARCH_BASE = "https://open.spotify.com/search/"


def build_spotify_url(query: str) -> tuple[str, str]:
    """(url, "") ou ("", motivo: empty | long | chars | secret | malformed). O texto é codificado por inteiro: não consegue
    mudar esquema, host nem estrutura do endereço."""
    q = " ".join((query or "").split())
    if not q:
        return "", "empty"
    if len(q) > 100:
        return "", "long"
    if any(unicodedata.category(c).startswith("C") for c in q):
        return "", "chars"
    if looks_like_secret(q):
        return "", "secret"
    url = SPOTIFY_SEARCH_BASE + quote(q, safe="")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "open.spotify.com" or not parts.path.startswith("/search/") or parts.query or parts.fragment:
        return "", "malformed"
    return url, ""


# --- configuração (padrões + data/actions.json) --------------------------------------------------------------------
_KEY = re.compile(r"^[a-z0-9_-]{1,40}$")
_PROCESS = re.compile(r"^[A-Za-z0-9._-]{1,64}\.exe$")
_WM_CLASS = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_URI = re.compile(r"^[a-z][a-z0-9+.-]{1,31}:$")
_BLOCKED_URI_SCHEMES = {"file", "javascript", "data", "shell", "vbscript", "http", "https", "ftp", "ms-msdt", "search-ms", "ms-officecmd",
                        "ms-appinstaller", "ms-cxh", "ms-cxh-full", "mhtml", "jar", "blob", "about"}


def detect_platform(sys_platform: str | None = None) -> str:
    import sys
    p = sys_platform or sys.platform
    return "windows" if p.startswith("win") else "macos" if p == "darwin" else "linux" if p.startswith("linux") else p


def default_actions_config(platform: str = "linux") -> ActionsConfig:
    """Só o essencial e sem caminhos pessoais: tudo parte de variáveis do sistema ou do diretório do usuário. Qualquer outra
    coisa é cadastrada pelo usuário em data/actions.json."""
    win, mac = platform == "windows", platform == "macos"
    apps = {
        "spotify": AppSpec("spotify", "Spotify", ("spotify",),
                           exe=(("%APPDATA%\\Spotify\\Spotify.exe", "%LOCALAPPDATA%\\Microsoft\\WindowsApps\\Spotify.exe") if win
                                else ("/Applications/Spotify.app/Contents/MacOS/Spotify",) if mac else ("spotify",)),
                           uri="spotify:", closable=True, process="Spotify.exe", wm_class="spotify.Spotify"),
        "navegador": AppSpec("navegador", "Navegador", ("navegador", "browser", "navegador padrao", "internet"),
                             default_browser=True, url="https://www.google.com"),
        "chrome": AppSpec("chrome", "Google Chrome", ("chrome", "google chrome"),
                          exe=(("%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe", "%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe",
                                "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe") if win
                               else ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",) if mac
                               else ("google-chrome", "google-chrome-stable", "chromium")),
                          closable=True, process="chrome.exe", wm_class="google-chrome.Google-chrome"),
        "vscode": AppSpec("vscode", "Visual Studio Code", ("vscode", "vs code", "visual studio code", "code"),
                          exe=(("%LOCALAPPDATA%\\Programs\\Microsoft VS Code\\Code.exe",) if win
                               else ("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",) if mac else ("code",)),
                          closable=True, process="Code.exe", wm_class="code.Code"),
    }
    folders = {
        "downloads": FolderSpec("downloads", "Downloads", "~/Downloads", ("downloads", "download")),
        "documentos": FolderSpec("documentos", "Documentos", "~/Documents", ("documentos", "meus documentos", "documents")),
    }
    sites = {
        "github": SiteSpec("github", "GitHub", "https://github.com", ("github",)),
        "youtube": SiteSpec("youtube", "YouTube", "https://www.youtube.com", ("youtube",)),
        "google": SiteSpec("google", "Google", "https://www.google.com", ("google",)),
    }
    return ActionsConfig(apps=apps, folders=folders, projects={}, sites=sites)


def _strs(value, field_name: str, errors: list[str], limit: int = 20) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(v, str) and v and "\0" not in v and "\n" not in v for v in value):
        errors.append(f"{field_name}: esperava uma lista de textos simples (até {limit}).")
        return ()
    return tuple(value)


def load_actions_config(path: Path | None, platform: str = "linux", allow_missing: bool = True) -> ActionsConfig:
    """Padrões + data/actions.json. Entrada inválida é IGNORADA e descrita em `errors`; nunca derruba o aplicativo."""
    base = default_actions_config(platform)
    errors: list[str] = []
    data: dict = {}
    if path is not None and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("a raiz precisa ser um objeto JSON")
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name} inválido ({exc}); usando só os padrões.")
            data = {}
    apps, folders, projects, sites = dict(base.apps), dict(base.folders), dict(base.projects), dict(base.sites)
    for key in data.get("remove", []) if isinstance(data.get("remove"), list) else []:
        for table in (apps, folders, projects, sites):
            table.pop(str(key).lower(), None)

    def entries(name: str):
        raw = data.get(name, {})
        if not isinstance(raw, dict):
            errors.append(f"{name}: esperava um objeto.")
            return
        for key, body in raw.items():
            k = str(key).lower()
            if not _KEY.fullmatch(k) or not isinstance(body, dict):
                errors.append(f"{name}.{key}: nome (a-z, 0-9, _ ou -) ou conteúdo inválido; ignorado.")
                continue
            yield k, body

    for k, b in entries("apps"):
        e: list[str] = []
        label = b.get("label", k)
        exe, args, aliases = _strs(b.get("exe"), f"apps.{k}.exe", e), _strs(b.get("args"), f"apps.{k}.args", e), _strs(b.get("aliases"), f"apps.{k}.aliases", e)
        uri, process, wm = str(b.get("uri", "") or ""), str(b.get("process", "") or ""), str(b.get("wm_class", "") or "")
        url = str(b.get("url", "") or "")
        if not isinstance(label, str) or not label.strip():
            e.append(f"apps.{k}.label inválido.")
        if uri and (not _URI.fullmatch(uri) or uri[:-1] in _BLOCKED_URI_SCHEMES):
            e.append(f"apps.{k}.uri: protocolo não permitido.")
        if process and not _PROCESS.fullmatch(process):
            e.append(f"apps.{k}.process: use só o nome do executável (ex.: Discord.exe).")
        if wm and not _WM_CLASS.fullmatch(wm):
            e.append(f"apps.{k}.wm_class inválido.")
        if url and validate_url(url)[1]:
            e.append(f"apps.{k}.url: precisa ser um endereço http/https válido.")
        if not (exe or uri or b.get("default_browser")):
            e.append(f"apps.{k}: informe exe, uri ou default_browser.")
        if e:
            errors += [f"{x} Entrada ignorada." for x in e]
            continue
        apps[k] = AppSpec(k, label.strip(), aliases, exe, args, uri, url or ("https://www.google.com" if b.get("default_browser") else ""),
                          bool(b.get("default_browser")), bool(b.get("closable")), process, wm)
    for k, b in entries("folders"):
        e = []
        p, label = b.get("path"), b.get("label", k)
        aliases = _strs(b.get("aliases"), f"folders.{k}.aliases", e)
        if not isinstance(p, str) or not p.strip() or not isinstance(label, str):
            e.append(f"folders.{k}: informe label e path.")
        if e:
            errors += [f"{x} Entrada ignorada." for x in e]
            continue
        folders[k] = FolderSpec(k, label.strip(), p.strip(), aliases)
    for k, b in entries("sites"):
        e = []
        u, label = b.get("url"), b.get("label", k)
        aliases = _strs(b.get("aliases"), f"sites.{k}.aliases", e)
        if not isinstance(u, str) or validate_url(u, bool(data.get("allow_local_urls")))[1]:
            e.append(f"sites.{k}.url: precisa ser um endereço http/https válido.")
        if e:
            errors += [f"{x} Entrada ignorada." for x in e]
            continue
        sites[k] = SiteSpec(k, str(label).strip(), u.strip(), aliases)
    for k, b in entries("projects"):
        e = []
        p, label, app = b.get("path"), b.get("label", k), str(b.get("app", "") or "").lower()
        aliases = _strs(b.get("aliases"), f"projects.{k}.aliases", e)
        if not isinstance(p, str) or not p.strip() or not isinstance(label, str):
            e.append(f"projects.{k}: informe label e path.")
        if app and app not in apps:
            e.append(f"projects.{k}.app: '{app}' não está na lista de aplicativos.")
        if e:
            errors += [f"{x} Entrada ignorada." for x in e]
            continue
        projects[k] = ProjectSpec(k, label.strip(), p.strip(), aliases, app)

    roots = data.get("allowed_roots")
    allowed = base.allowed_roots + tuple(r for r in _strs(roots, "allowed_roots", errors)) if roots else base.allowed_roots
    kinds = {ActionKind.CLOSE_APP}  # fechar SEMPRE exige confirmação; o usuário pode exigir também para outras ações
    for name in data.get("confirm", []) if isinstance(data.get("confirm"), list) else []:
        try:
            kinds.add(ActionKind(name))
        except ValueError:
            errors.append(f"confirm: ação desconhecida '{name}'.")
    ttl = data.get("confirmation_ttl_s", base.confirmation_ttl_s)
    ttl = float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and 5 <= ttl <= 300 else base.confirmation_ttl_s
    return ActionsConfig(enabled=bool(data.get("enabled", True)), apps=apps, folders=folders, projects=projects, sites=sites,
                         allowed_roots=allowed, confirm_kinds=frozenset(kinds), confirmation_ttl_s=ttl,
                         allow_local_urls=bool(data.get("allow_local_urls", False)), errors=tuple(errors))
