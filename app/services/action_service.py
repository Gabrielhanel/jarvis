"""Executor de ações locais AUTORIZADAS. Recebe uma intenção do usuário, resolve contra a lista de permissões, pede confirmação
quando necessário e só então executa, REVALIDANDO tudo no momento da execução (o que veio do classificador, da IA ou da
confirmação nunca é confiado por já ter passado antes).

Garantias: sem shell; sem comando, caminho de executável ou argumento vindo do usuário/IA; pastas só dentro das raízes
autorizadas; URLs só http/https validadas; fechar só com confirmação, por pedido normal (nunca forçado); resultado estruturado;
sucesso significa "o sistema aceitou o pedido", nunca "a janela apareceu"."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from app.core.action_intents import ActionParse
from app.core.actions import (ActionKind, ActionOutcome, ActionRequest, ActionResult, ActionsConfig, ActionStatus, AppSpec, PendingConfirmation,
                              authorized_dir, build_spotify_url, expand_path, validate_url)
from app.core.textutil import fold, looks_like_secret
from app.services.confirmation import ConfirmationManager
from app.services.system_backend import SystemBackend

log = logging.getLogger("jarvis.actions")
S = ActionStatus
_METACHARS = re.compile(r"[;|&$`<>\"{}()\[\]*?=%#@!^]")
_PATH_LIKE = re.compile(r"[/\\]|^~|\.\.|^[a-z]:")  # separador, ~, .. ou unidade (C:), sempre testado no texto ORIGINAL em minúsculas


class ActionService:
    def __init__(self, cfg: ActionsConfig, backend: SystemBackend | None = None, clock: Callable[[], float] = time.monotonic,
                 env: Mapping[str, str] | None = None, home: Path | None = None) -> None:
        self.cfg, self.backend, self._clock, self._env, self._home = cfg, backend or SystemBackend(), clock, env, home
        self.confirmations = ConfirmationManager(cfg.confirmation_ttl_s, clock)
        self._recent: dict[tuple, float] = {}
        self._lock = threading.Lock()

    # --- estado ---
    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def supported_kinds(self) -> set[ActionKind]:
        """O que este sistema consegue de fato (para o registro de capacidades). Se um item específico está instalado só é
        descoberto ao executar."""
        if not self.enabled:
            return set()
        kinds = {ActionKind.OPEN_APP, ActionKind.OPEN_FOLDER, ActionKind.OPEN_PROJECT, ActionKind.OPEN_URL, ActionKind.SPOTIFY_SEARCH}
        if self.backend.can_close and any(self._can_close(a) for a in self.cfg.apps.values()):
            kinds.add(ActionKind.CLOSE_APP)
        return kinds

    def has_pending(self) -> bool:
        return self.confirmations.current() is not None

    def recently_expired(self) -> bool:
        return self.confirmations.recently_expired()

    def awaiting_decision(self) -> bool:
        """"sim"/"cancelar" faz sentido agora? (há confirmação pendente, ou uma acabou de expirar e vai ser explicada). Não consome nada."""
        return self.confirmations.current() is not None or self.confirmations.recently_expired(consume=False)

    # --- entrada principal (texto/voz): intenção -> resultado ---
    def handle(self, parse: ActionParse, source: str = "texto") -> ActionOutcome:
        try:
            resolved = self._resolve(parse)
            if isinstance(resolved, ActionResult):
                return ActionOutcome(self._log(resolved))
            cleared: list[tuple[str, str]] = []
            if resolved.needs_confirmation or resolved.kind in self.cfg.confirm_kinds:
                question = f"Você deseja fechar o aplicativo {resolved.label}? Verifique se há arquivos não salvos. Confirme ou cancele." \
                    if resolved.kind is ActionKind.CLOSE_APP else f"Você confirma: {self._describe(resolved)}? Confirme ou cancele."
                pending, old = self.confirmations.create(resolved, question, self._describe(resolved))
                if old:
                    cleared.append((old, "superseded"))
                log.info("[ACT] confirmação %s criada para %s (%s)", pending.id[:8], resolved.kind.value, resolved.target)
                return ActionOutcome(ActionResult(S.CONFIRMATION_REQUIRED, question, resolved.kind, resolved.target, resolved.id), pending, tuple(cleared))
            old = self.confirmations.cancel()  # uma ação nova e simples também encerra a confirmação anterior
            if old:
                cleared.append((old, "superseded"))
            return ActionOutcome(self.execute(resolved), None, tuple(cleared))
        except Exception:  # noqa: BLE001 - o controle local nunca derruba o pipeline
            log.exception("[ACT] falha inesperada ao tratar o pedido")
            return ActionOutcome(ActionResult(S.ERROR, "Não consegui executar isso agora. O restante do Jarvis continua funcionando."))

    def handle_decision(self, decision: str) -> ActionOutcome:
        """"sim"/"cancelar" digitado ou falado: só atua sobre a confirmação pendente atual."""
        cur = self.confirmations.current()
        if cur is None:
            if self.confirmations.recently_expired():
                return ActionOutcome(self._log(ActionResult(S.EXPIRED, "Essa solicitação expirou. Peça de novo se ainda quiser.")))
            return ActionOutcome(ActionResult(S.CANCELLED, "Não há nenhuma ação aguardando confirmação."))
        return self.confirm(cur.id) if decision == "confirm" else self.cancel(cur.id)

    def confirm(self, pending_id: str) -> ActionOutcome:
        status, pending = self.confirmations.take(pending_id)
        if status == "expired":
            return ActionOutcome(self._log(ActionResult(S.EXPIRED, "Essa solicitação expirou. Peça de novo se ainda quiser.")), None, ((pending_id, "expired"),))
        if pending is None:
            return ActionOutcome(self._log(ActionResult(S.CANCELLED, "Essa confirmação não vale mais. Peça a ação de novo.")), None, ((pending_id, "stale"),))
        return ActionOutcome(self.execute(pending.request, confirmed=True), None, ((pending_id, "confirmed"),))

    def cancel(self, pending_id: str | None = None) -> ActionOutcome:
        cancelled = self.confirmations.cancel(pending_id)
        if cancelled is None:
            return ActionOutcome(ActionResult(S.CANCELLED, "Não há nenhuma ação aguardando confirmação."))
        return ActionOutcome(self._log(ActionResult(S.CANCELLED, "Ação cancelada. Nada foi feito.")), None, ((cancelled, "cancelled"),))

    def expire(self, pending_id: str) -> ActionOutcome:
        """O prazo da barra de confirmação acabou: se ainda estiver pendente, é cancelada (nunca executa depois do prazo)."""
        self.confirmations.cancel(pending_id)
        return ActionOutcome(self._log(ActionResult(S.EXPIRED, "A confirmação expirou e nada foi feito.")), None, ((pending_id, "expired"),))

    def cancel_pending(self, reason: str) -> tuple[tuple[str, str], ...]:
        cancelled = self.confirmations.cancel()
        if cancelled:
            log.info("[ACT] confirmação %s cancelada (%s)", cancelled[:8], reason)
            return ((cancelled, reason),)
        return ()

    # --- resolução: texto não confiável -> destino autorizado ---
    def _resolve(self, parse: ActionParse) -> ActionRequest | ActionResult:
        cfg = self.cfg
        if not cfg.enabled:
            return ActionResult(S.UNSUPPORTED, "O controle do computador está desativado na configuração.")
        if parse.op == "spotify":
            url, why = build_spotify_url(parse.raw)
            if why:
                return ActionResult(S.INVALID_PARAM, {"empty": "O que devo pesquisar no Spotify?", "long": "Essa pesquisa é longa demais. Use até 100 caracteres.",
                                                      "secret": "Não vou pesquisar isso porque parece conter um segredo."}.get(why, "Não consegui entender esse texto de pesquisa."),
                                    ActionKind.SPOTIFY_SEARCH)
            return ActionRequest(ActionKind.SPOTIFY_SEARCH, "spotify", "Spotify", detail=" ".join(parse.raw.split()))
        if parse.op == "close":
            return self._resolve_close(parse)
        return self._resolve_open(parse)

    def _unknown(self, parse: ActionParse, namespaces: tuple[str, ...], what: str) -> ActionResult:
        low = parse.raw.strip().lower()
        if re.match(r"^[a-z][a-z0-9+.-]+:", low) and not re.match(r"^[a-z]:[\\/]", low):  # file:, javascript:, shell: ... (C:\ é caminho)
            return ActionResult(S.NOT_AUTHORIZED, "Só abro endereços http ou https.")
        if _PATH_LIKE.search(low):
            return ActionResult(S.NOT_AUTHORIZED, "Esse caminho não está na lista de destinos autorizados. Só abro pastas e projetos cadastrados.")
        t = parse.target
        shown = "" if looks_like_secret(parse.raw) or len(t) > 40 else f" “{t}”"
        names = ", ".join(self.cfg.names(namespaces))
        return ActionResult(S.UNKNOWN_TARGET, f"Não encontrei{shown} na lista de {what} autorizados." + (f" Posso: {names}." if names else ""))

    def _suspicious(self, raw: str, kind: ActionKind) -> ActionResult | None:
        """O alias é comparado SEM pontuação; por isso o texto original é conferido antes: "Downloads/../.." ou "spotify; rm -rf /"
        nunca viram "Downloads" ou "spotify" por terem as palavras certas no meio."""
        low = raw.strip().lower()
        if re.match(r"^[a-z][a-z0-9+.-]+:", low) and not re.match(r"^[a-z]:[\\/]", low):
            return ActionResult(S.NOT_AUTHORIZED, "Só abro endereços http ou https.", kind)
        if _PATH_LIKE.search(low):
            return ActionResult(S.NOT_AUTHORIZED, "Esse caminho não está na lista de destinos autorizados. Só abro pastas e projetos cadastrados.", kind)
        if _METACHARS.search(raw):
            return ActionResult(S.INVALID_PARAM, "Não entendi esse nome. Diga só o nome do aplicativo, pasta ou projeto da lista.", kind)
        return None

    def _resolve_open(self, parse: ActionParse) -> ActionRequest | ActionResult:
        cfg, noun, t = self.cfg, parse.noun, parse.target
        if not t and not parse.raw:
            ask = {"pasta": "Qual pasta você quer abrir?", "site": "Qual site você quer abrir?", "projeto": "Qual projeto você quer abrir?",
                   "app": "Qual aplicativo você quer abrir?"}.get(noun, "O que você quer abrir?")
            if noun == "projeto" and len(cfg.projects) == 1:  # "abra meu projeto" com um único projeto cadastrado: sem ambiguidade
                p = next(iter(cfg.projects.values()))
                return ActionRequest(ActionKind.OPEN_PROJECT, p.key, p.label)
            return ActionResult(S.INVALID_PARAM, ask, ActionKind.OPEN_APP)
        if noun == "site" or re.match(r"(?:https?://|www\.)", parse.raw, re.I):
            site = cfg.find("sites", t)
            if site is not None:
                return ActionRequest(ActionKind.OPEN_URL, site.key, site.label, detail=site.url)
            return self._resolve_typed_url(parse)
        bad = self._suspicious(parse.raw, ActionKind.OPEN_APP)
        if bad is not None:
            return bad
        order = {"app": ("apps",), "pasta": ("folders",), "projeto": ("projects",)}.get(noun, ("apps", "folders", "projects", "sites"))
        hits = [(ns, spec) for ns in order if (spec := cfg.find(ns, t)) is not None]
        if noun == "projeto" and not hits:
            if len(cfg.projects) == 1 and t in {"programacao", "codigo", "dev", "desenvolvimento"}:
                p = next(iter(cfg.projects.values()))
                return ActionRequest(ActionKind.OPEN_PROJECT, p.key, p.label)
            if not cfg.projects:
                return ActionResult(S.UNKNOWN_TARGET, "Não há projetos cadastrados. Cadastre em data/actions.json (veja o README).", ActionKind.OPEN_PROJECT)
            return ActionResult(S.UNKNOWN_TARGET, f"Qual projeto? Cadastrados: {', '.join(cfg.names(('projects',)))}.", ActionKind.OPEN_PROJECT)
        if len(hits) > 1:
            return ActionResult(S.INVALID_PARAM, "Isso pode ser mais de uma coisa: " + ", ".join(f"{s.label} ({ns[:-1]})" for ns, s in hits) + ". Diga, por exemplo, “abra a pasta ...”.")
        if not hits:
            return self._unknown(parse, order, "destinos" if len(order) > 1 else {"apps": "aplicativos", "folders": "pastas", "projects": "projetos"}[order[0]])
        ns, spec = hits[0]
        kind = {"apps": ActionKind.OPEN_APP, "folders": ActionKind.OPEN_FOLDER, "projects": ActionKind.OPEN_PROJECT, "sites": ActionKind.OPEN_URL}[ns]
        return ActionRequest(kind, spec.key, spec.label, detail=spec.url if ns == "sites" else "")

    def _resolve_typed_url(self, parse: ActionParse) -> ActionRequest | ActionResult:
        raw = parse.raw.strip()
        if not raw:
            return ActionResult(S.INVALID_PARAM, "Qual site você quer abrir?", ActionKind.OPEN_URL)
        if re.match(r"^(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:[/?#]\S*)?$", raw):
            raw = "https://" + raw  # "www.exemplo.com" digitado: https por padrão
        if not re.match(r"^[a-z][a-z0-9+.-]*:", raw.lower()) and "/" not in raw and not raw.lower().startswith("www."):  # só um nome, sem endereço
            names = ", ".join(self.cfg.names(("sites",)))
            return ActionResult(S.UNKNOWN_TARGET, "Não encontrei esse site na lista. Diga o endereço completo, como https://exemplo.com." + (f" Posso: {names}." if names else ""), ActionKind.OPEN_URL)
        url, why = validate_url(raw, self.cfg.allow_local_urls)
        if why == "scheme":
            return ActionResult(S.NOT_AUTHORIZED, "Só abro endereços http ou https.", ActionKind.OPEN_URL)
        if why:
            msg = {"credentials": "Não abro endereços com usuário e senha embutidos.", "local": "Não abro endereços de rede interna ou do próprio computador.",
                   "secret": "Esse endereço parece conter um segredo; não abri.", "empty": "Qual site você quer abrir?"}.get(why, "Esse endereço não parece válido. Diga o endereço completo, como https://exemplo.com.")
            return ActionResult(S.INVALID_PARAM, msg, ActionKind.OPEN_URL)
        host = urlsplit(url).hostname or url
        return ActionRequest(ActionKind.OPEN_URL, host, host, detail=url)

    def _resolve_close(self, parse: ActionParse) -> ActionRequest | ActionResult:
        if not parse.target:
            return ActionResult(S.INVALID_PARAM, "Qual aplicativo você quer fechar?", ActionKind.CLOSE_APP)
        bad = self._suspicious(parse.raw, ActionKind.CLOSE_APP)
        if bad is not None:
            return bad
        spec = self.cfg.find("apps", parse.target)
        if spec is None:
            if _PATH_LIKE.search(parse.raw.lower()) or re.search(r"\.exe\b|\bpid\b|\bprocesso\b", parse.raw.lower()):
                return ActionResult(S.NOT_AUTHORIZED, "Só fecho aplicativos da lista autorizada, pelo nome.", ActionKind.CLOSE_APP)
            names = ", ".join(a.label for a in self.cfg.apps.values() if a.closable)
            return ActionResult(S.UNKNOWN_TARGET, "Não encontrei esse aplicativo na lista." + (f" Posso fechar: {names}." if names else ""), ActionKind.CLOSE_APP)
        if not spec.closable:
            return ActionResult(S.NOT_AUTHORIZED, f"Não tenho autorização para fechar {spec.label}.", ActionKind.CLOSE_APP, spec.key)
        if not self.backend.can_close or not self._can_close(spec):
            return ActionResult(S.UNSUPPORTED, f"Não consigo fechar {spec.label} com segurança neste sistema.", ActionKind.CLOSE_APP, spec.key)
        return ActionRequest(ActionKind.CLOSE_APP, spec.key, spec.label, needs_confirmation=True)

    def _can_close(self, spec: AppSpec) -> bool:
        return bool(spec.closable and ((self.backend.platform == "windows" and spec.process) or (self.backend.platform == "linux" and spec.wm_class)))

    def _describe(self, r: ActionRequest) -> str:
        return {ActionKind.CLOSE_APP: f"fechar o aplicativo {r.label} (pedido de fechamento normal; nada é forçado)",
                ActionKind.SPOTIFY_SEARCH: f"pesquisar “{r.detail}” no Spotify", ActionKind.OPEN_URL: f"abrir {r.label} no navegador",
                ActionKind.OPEN_FOLDER: f"abrir a pasta {r.label}", ActionKind.OPEN_PROJECT: f"abrir o projeto {r.label}",
                ActionKind.OPEN_APP: f"abrir {r.label}"}[r.kind]

    # --- execução: SEMPRE revalida contra a lista autorizada ---
    def execute(self, request: ActionRequest, confirmed: bool = False) -> ActionResult:
        """`confirmed=True` só é passado por confirm(), depois de consumir uma confirmação válida. Ações sensíveis chamadas sem
        isso NÃO executam, não importa quem chame."""
        try:
            if (request.kind in self.cfg.confirm_kinds or request.needs_confirmation) and not confirmed:
                return self._log(ActionResult(S.CONFIRMATION_REQUIRED, "Essa ação precisa da sua confirmação antes de ser executada.", request.kind, request.target, request.id))
            key = (request.kind, request.target, request.detail)
            now = self._clock()
            with self._lock:
                self._recent = {k: t for k, t in self._recent.items() if now - t < self.cfg.dedupe_window_s}
                if key in self._recent:
                    return self._log(ActionResult(S.DUPLICATE, "Já fiz esse pedido agora há pouco; não repeti.", request.kind, request.target, request.id))
            result = self._dispatch(request)
            if result.ok:
                with self._lock:
                    self._recent[key] = now
            return self._log(result)
        except Exception:  # noqa: BLE001
            log.exception("[ACT] erro inesperado ao executar %s", request.kind.value)
            return self._log(ActionResult(S.ERROR, "Não consegui executar isso agora.", request.kind, request.target, request.id))

    def _dispatch(self, r: ActionRequest) -> ActionResult:
        def res(status: S, msg: str) -> ActionResult:
            return ActionResult(status, msg, r.kind, r.target, r.id)

        if r.kind is ActionKind.OPEN_APP:
            spec = self.cfg.apps.get(r.target)  # a CHAVE precisa existir na lista: um ActionRequest forjado não passa
            return self._open_app(spec, res) if spec else res(S.NOT_AUTHORIZED, "Esse aplicativo não está na lista de destinos autorizados.")
        if r.kind is ActionKind.OPEN_FOLDER:
            spec = self.cfg.folders.get(r.target)
            if not spec:
                return res(S.NOT_AUTHORIZED, "Essa pasta não está na lista de destinos autorizados.")
            path, why = authorized_dir(spec.path, self.cfg.allowed_roots, self._env, self._home)
            if path is None:
                return self._dir_failure(spec.label, why, res)
            out = self.backend.open_folder(str(path))
            return res(S.SUCCESS, f"Abrindo a pasta {spec.label}.") if out.ok else self._open_failure(out, f"Não consegui abrir a pasta {spec.label}.", res)
        if r.kind is ActionKind.OPEN_PROJECT:
            return self._open_project(r, res)
        if r.kind is ActionKind.OPEN_URL:
            site = self.cfg.sites.get(r.target)
            url, why = validate_url(site.url if site else r.detail, self.cfg.allow_local_urls)  # revalida mesmo a URL configurada
            if why:
                return res(S.INVALID_PARAM, "Esse endereço não passou na validação; não abri.")
            label = site.label if site else (urlsplit(url).hostname or "o site")
            return res(S.SUCCESS, f"Abrindo {label} no navegador.") if self.backend.open_url(url) else res(S.ERROR, "Não consegui abrir o endereço solicitado.")
        if r.kind is ActionKind.SPOTIFY_SEARCH:
            url, why = build_spotify_url(r.detail)
            if why:
                return res(S.INVALID_PARAM, "Esse texto de pesquisa não passou na validação; não abri.")
            if not self.backend.open_url(url):
                return res(S.ERROR, "Não consegui abrir o Spotify.")
            return res(S.SUCCESS, f"Abrindo a pesquisa por “{' '.join(r.detail.split())}” no Spotify. Isso só mostra os resultados; não toca nada.")
        if r.kind is ActionKind.CLOSE_APP:
            return self._close_app(r, res)
        return res(S.NOT_AUTHORIZED, "Essa ação não é permitida.")

    def _dir_failure(self, label: str, why: str, res) -> ActionResult:
        if why == "missing":
            return res(S.UNAVAILABLE, f"A pasta {label} não existe neste computador.")
        if why == "outside":
            return res(S.NOT_AUTHORIZED, "Essa pasta não está na lista de destinos autorizados.")
        return res(S.INVALID_PARAM, f"O caminho configurado para {label} não é uma pasta válida.")

    def _open_failure(self, out, text: str, res) -> ActionResult:
        return res(S.UNSUPPORTED, "Este sistema não tem a ferramenta necessária para abrir isso.") if out.unsupported else res(S.ERROR, text)

    def _find_launcher(self, spec: AppSpec) -> tuple[str, str] | None:
        """("exe", caminho) | ("uri", protocolo) — só entre os candidatos CONFIGURADOS e que existem de fato."""
        for cand in spec.exe:
            path = expand_path(cand, self._env, self._home)
            if "%" in path:
                continue  # variável de ambiente que não existe neste computador
            if os.path.isabs(path) or "/" in path or "\\" in path:
                if self.backend.is_executable(path):
                    return "exe", path
            elif (found := self.backend.which(path)):
                return "exe", found
        if spec.uri and self.backend.uri_registered(spec.uri.rstrip(":")):
            return "uri", spec.uri
        return None

    def _open_app(self, spec: AppSpec, res) -> ActionResult:
        if spec.default_browser:
            url, why = validate_url(spec.url or "https://www.google.com")
            return res(S.SUCCESS, f"Abrindo o {spec.label.lower()}.") if not why and self.backend.open_url(url) else res(S.ERROR, f"Não consegui abrir o {spec.label.lower()}.")
        found = self._find_launcher(spec)
        if found is None:
            return res(S.UNAVAILABLE, f"Não encontrei o {spec.label} instalado neste computador.")
        kind, where = found
        out = self.backend.launch(where, list(spec.args)) if kind == "exe" else self.backend.open_uri(where)
        return res(S.SUCCESS, f"Abrindo {spec.label}.") if out.ok else self._open_failure(out, f"Não consegui abrir {spec.label}.", res)

    def _open_project(self, r: ActionRequest, res) -> ActionResult:
        spec = self.cfg.projects.get(r.target)
        if not spec:
            return res(S.NOT_AUTHORIZED, "Esse projeto não está na lista de destinos autorizados.")
        path, why = authorized_dir(spec.path, self.cfg.allowed_roots, self._env, self._home)
        if path is None:  # sem plano B: não procura nem abre outra pasta no lugar
            return self._dir_failure(f"do projeto {spec.label}", why, res) if why != "missing" else res(S.UNAVAILABLE, f"A pasta do projeto {spec.label} não existe mais. Verifique data/actions.json.")
        app = self.cfg.apps.get(spec.app) if spec.app else None
        if app is None or app.default_browser:
            out = self.backend.open_folder(str(path))
            return res(S.SUCCESS, f"Abrindo a pasta do projeto {spec.label}.") if out.ok else self._open_failure(out, f"Não consegui abrir o projeto {spec.label}.", res)
        found = self._find_launcher(app)
        if found is None or found[0] != "exe":
            return res(S.UNAVAILABLE, f"Não encontrei o {app.label} para abrir o projeto {spec.label}.")
        out = self.backend.launch(found[1], [*app.args, str(path)])  # caminho já resolvido e autorizado, como argumento único
        return res(S.SUCCESS, f"Abrindo o projeto {spec.label} no {app.label}.") if out.ok else self._open_failure(out, f"Não consegui abrir o projeto {spec.label}.", res)

    def _close_app(self, r: ActionRequest, res) -> ActionResult:
        spec = self.cfg.apps.get(r.target)
        if not spec or not spec.closable:
            return res(S.NOT_AUTHORIZED, "Esse aplicativo não pode ser fechado por mim.")
        if not self._can_close(spec) or not self.backend.can_close:
            return res(S.UNSUPPORTED, f"Não consigo fechar {spec.label} com segurança neste sistema.")
        out = self.backend.close(spec.process, spec.wm_class)  # apenas pedido de fechamento normal
        if out.status == "requested":
            return res(S.SUCCESS, f"Pedi ao {spec.label} para fechar. Se houver algo não salvo, ele deve perguntar. Não forcei nada.")
        if out.status == "not_running":
            return res(S.UNAVAILABLE, f"O {spec.label} não parece estar aberto.")
        if out.status == "refused":
            return res(S.ERROR, f"O {spec.label} não aceitou o pedido de fechamento normal, ou não está aberto. Não vou forçar o encerramento.")
        if out.status == "unsupported":
            return res(S.UNSUPPORTED, f"Não consigo fechar {spec.label} com segurança neste sistema.")
        return res(S.ERROR, f"Não consegui pedir o fechamento de {spec.label}.")

    def _log(self, result: ActionResult) -> ActionResult:
        log.info("[ACT] %s alvo=%s -> %s", result.kind.value if result.kind else "-", result.target or "-", result.status.name)  # sem texto livre, URL completa ou consulta
        return result
