"""Exécute une demande « Mettre à jour le dossier » avec le Claude Agent SDK (agent local, API Anthropic).

Lancé par l'application web (bouton Run) dans un processus séparé :
    python -m kyc_agent.worker <case_dir> <run_dir>

<run_dir> = cases/<ID>/runs/<horodatage>_pedido/ contient pedido.json (nombre d'agents, éléments en attente).
Le worker y écrit, pendant l'exécution :
    status.json  {"estado": "em_curso"|"concluido"|"erro"|"simulado", "tarefa", "uso", "modelo", "custo_usd", ...}
    log.jsonl    une ligne {"ts", "msg"} par étape (lue par « Agent brain » / le cerveau du tableau de bord)
À la fin d'une exécution réelle réussie : versions/v<N+1>/metricas.json (mesure exacte) et marquage des réponses
et pièces traitées (integrada_em_versao / integrado_em_versao).

Authentification : variable d'environnement KYC_ANTHROPIC_API_KEY (clé propre à l'application, transmise au seul
processus de l'agent ; ANTHROPIC_API_KEY est acceptée en repli). Facturation API. Sans clé, ou avec
KYC_AGENT_SIMULATE=1, le worker fait une SIMULATION : mêmes écrans, aucun appel à l'API, dossier inchangé.
Aucun plafond de dépense n'est fixé (choix du superviseur) ; il se règle avec KYC_AGENT_MAX_USD si besoin.

Remise en état : avant une exécution réelle, l'état de travail (state.json) et work/ sont copiés dans
<run_dir>/reversao/ avec un inventaire du dossier. Si l'exécution échoue — erreur, résultat incomplet, coupure
réseau prolongée — ou si le processus disparaît (ordinateur éteint, veille, arrêt forcé : détecté par l'application
quand le battement de cœur de status.json s'arrête), `reverter` remet le dossier exactement dans l'état d'avant :
version partielle supprimée, state.json et work/ restaurés, marquages « traité » annulés. Les pièces et réponses
ajoutées par les utilisateurs pendant l'exécution sont conservées.

Reprise : avant cette remise en état, le travail de l'agent (work/, state.json, version partielle) est mis de côté
dans <run_dir>/retomada/ avec l'identifiant de sa conversation (session). « Reprendre la mise à jour » crée une
nouvelle exécution qui restaure ce travail et reprend la conversation là où elle s'était arrêtée (si la session est
encore disponible sur ce poste ; sinon l'agent repart du travail restauré). Durée : chaque exécution enregistre sa
durée (duracao_s), cumulée avec celle des exécutions qu'elle reprend.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kyc_agent.journal import NOME_VERSAO, Journal, texto_de_resultado

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = Path(__file__).resolve().parent / "prompts"
MODEL = os.environ.get("KYC_AGENT_MODEL", "claude-opus-5-5")
TAMPAO_MENSAGEM = 64 * 1024 * 1024  # taille maximale d'un message du moteur de l'agent (images de pages scannées)
CAMPOS = ("in", "out", "cw5", "cw1h", "cr")
MAX_SUBAGENTES = int(os.environ.get("KYC_AGENT_MAX_SUBAGENTES", "5"))  # garde-fou de coût : sous-agents simultanés
OUTILS_SUBAGENTE = {"Task", "Agent"}

# Commandes shell refusées : effacement massif, modification du dépôt, envoi d'e-mail.
BASH_INTERDIT = re.compile(
    r"(rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r|Remove-Item\b.*-Recurse|\bdel\s+/[sq]|rmdir\s+/s|format\s+[a-z]:|"
    r"git\s+(push|commit|reset)|sendmail|smtplib|\bmail\s|Send-MailMessage)",
    re.I)
# Envoi de données vers un site (formulaire, POST, API, dépôt de fichier) : autorisé seulement vers la liste blanche
# kyc_agent/dominios_oficiais.json (sites officiels), pour qu'une pièce piégée ne puisse pas faire sortir le dossier.
ENVIO = re.compile(
    r"(\bcurl\b[^|;&]*(\s-X\s*(POST|PUT|PATCH|DELETE)|--data|\s-d\s|\s-F\s|--form|--upload-file|\s-T\s|--json)|"
    r"\bwget\b[^|;&]*--post|Invoke-(WebRequest|RestMethod)\b[^|;&]*(-Method\s+(Post|Put|Patch|Delete)|-Body|-InFile)|"
    r"\b\w+\.(post|put|patch|delete)\s*\(|\.request\(\s*[\"'](POST|PUT|PATCH|DELETE)|"
    r"urlopen\([^)]*data\s*=|Request\([^)]*data\s*=|\bftplib\b|\bparamiko\b|socket\.connect)",
    re.I)
HOST = re.compile(r"https?://([A-Za-z0-9.-]+)", re.I)
DOMINIOS_OFICIAIS = Path(__file__).resolve().parent / "dominios_oficiais.json"


def carregar_lista_branca() -> tuple[set[str], tuple[str, ...]]:
    d = json.loads(DOMINIOS_OFICIAIS.read_text(encoding="utf-8")) if DOMINIOS_OFICIAIS.exists() else {}
    return ({x["dominio"].lower() for x in d.get("dominios", [])}, tuple(x["sufixo"].lower().lstrip(".") for x in d.get("sufixos", [])))


def sites_recusados(texto: str, lista: tuple[set[str], tuple[str, ...]]) -> list[str] | None:
    """None si `texto` (commande ou script) n'envoie rien hors de la liste blanche ; sinon la liste des sites refusés
    ([] = envoi sans adresse explicite, invérifiable)."""
    if not ENVIO.search(texto or ""):
        return None
    hosts = {h.lower().rstrip(".") for h in HOST.findall(texto)}
    if not hosts:
        return []
    dominios, sufixos = lista
    fora = sorted(h for h in hosts if h not in dominios and not any(h == x or h.endswith("." + x) for x in sufixos))
    return fora or None


def envio_nao_autorizado(texto: str, lista: tuple[set[str], tuple[str, ...]]) -> str | None:
    """Motif du refus si `texto` envoie des données hors de la liste blanche, sinon None."""
    fora = sites_recusados(texto, lista)
    if fora is None:
        return None
    if not fora:
        return "envoi de données sans URL explicite : écris l'adresse officielle en clair (https://…) dans la commande ou le script"
    return f"envoi de données vers un site hors de la liste blanche des sites officiels : {', '.join(fora)}"


def registrar_envio_recusado(reg: "Registro", sites: list[str], ferramenta: str, trecho: str, agente=None):
    """Garde la trace d'un envoi refusé (status.json « envios_recusados ») pour le signaler au superviseur en fin
    d'analyse : il décide s'il faut ajouter le site à la liste blanche."""
    lst = reg.st.setdefault("envios_recusados", [])
    lst.append({"em": agora(), "sites": sites or ["(adresse non explicite)"], "ferramenta": ferramenta,
                "agente": agente or "principal", "trecho": " ".join(str(trecho).split())[:400]})
    reg._salvar()
LEITURA_LOCAL = {"Read", "Glob", "Grep", "LS"}           # lecture de fichiers : limitée au périmètre du dossier
LIVRES = {"WebSearch", "WebFetch", "TodoWrite", "TaskOutput"}  # sans accès aux fichiers locaux
LEITURA = LEITURA_LOCAL | LIVRES | {"Task", "Agent"}
# références communes autorisées en lecture, en plus du dossier traité
REFERENCIAS = ("data/job_guidelines_v2.pdf", "data/job_guidelines.pdf", "data/format_output.txt", "kyc_agent")
# code propre à un dossier particulier : jamais lu par l'agent d'un autre dossier
CODIGO_DE_DOSSIE = re.compile(r"cases_[a-z0-9]+_v\d+\.py$", re.I)
# commandes shell qui modifient des fichiers (utilisé pour protéger les pièces)
BASH_ESCRITA = re.compile(r"(\b(rm|mv|del|erase|move|ren|rename|unlink|truncate|chmod|attrib)\b|Remove-Item|Move-Item|Rename-Item|"
                          r"Set-Content|Add-Content|Out-File|Clear-Content|sed\s+-i|>{1,2})", re.I)
VIGIA_S = 2               # fréquence de vérification d'une demande d'arrêt
ESCRITA = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# parties du dossier gérées par l'application (pièces, réponses, métadonnées, exécutions) : jamais écrites par l'agent
PROTEGIDOS = ("documentos", "respostas", "runs", "metricas", "dossie.json", "documentos.json", "requete.json", "conversa")
BATIMENTO_S = 15          # le worker signale qu'il est vivant toutes les 15 s (status.json « vivo_em »)
INTERROMPIDO_S = 5 * 60   # sans signe de vie depuis 5 min : processus considéré comme arrêté → remise en état
GRACA_S = 20              # tour principal fini, plus rien ne tourne : délai pour une relance automatique du moteur
SILENCIO_MAX_S = 45 * 60  # sous-agents en arrière-plan sans aucune nouvelle pendant 45 min : erreur explicite
MAX_RELANCES = 10         # relances de l'agent principal après la fin de ses sous-agents
MAX_RELANCES_VERSAO = 2   # relances de l'agent principal qui rend la main sans avoir écrit la version


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Registro:
    """status.json + log.jsonl d'une exécution, écrits au fil de l'eau."""

    def __init__(self, run_dir: Path, base: dict):
        self.dir = run_dir
        self.st = {"estado": "em_curso", "inicio": agora(), "atualizado_em": agora(), "vivo_em": agora(), "pid": os.getpid(),
                   "modelo": MODEL, "uso": dict.fromkeys(CAMPOS, 0), "tarefa": "", **base}
        self.dono_reversao = False
        self.journal = Journal(run_dir)
        self._salvar()

    def revertido_por_terceiro(self) -> bool:
        return (self.dir / "reversao.lock").exists() and not self.dono_reversao

    def batimento(self):
        self.st["vivo_em"] = agora()
        self._salvar()

    def _salvar(self):
        if self.revertido_por_terceiro():  # l'application a déjà remis le dossier en état : ne plus rien écrire
            return
        self.st["vivo_em"] = agora()
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps(self.st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "status.json")

    def log(self, msg: str):
        msg = " ".join(str(msg).split())[:300]
        if not msg:
            return
        with (self.dir / "log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": agora(), "msg": msg}, ensure_ascii=False) + "\n")
        self.journal.ev("etapa", texto=msg)
        self.st["tarefa"] = msg
        self.st["atualizado_em"] = agora()
        self._salvar()

    def uso(self, uso: dict, custo: float | None = None):
        self.st["uso"] = uso
        if custo is not None:
            self.st["custo_usd"] = custo
        self.st["atualizado_em"] = agora()
        self._salvar()

    def plano(self, todos):
        """Plan de travail de l'agent chef (TodoWrite) : progression = tâches faites / total. Le total peut augmenter
        (plan révisé) : on compte les révisions, la progression peut alors reculer."""
        if not isinstance(todos, list):
            return
        todos = [t for t in todos if isinstance(t, dict)]
        if not todos:
            return
        ant = self.st.get("plano") or {}
        total = len(todos)
        feitas = sum(1 for t in todos if t.get("status") == "completed")
        atual = next((str(t.get("activeForm") or t.get("content") or "") for t in todos if t.get("status") == "in_progress"), "")
        self.st["plano"] = {"total": total, "feitas": feitas, "pct": round(100 * feitas / total), "em_curso": atual[:200],
                            "revisoes": (ant.get("revisoes") or 0) + (1 if ant and total > (ant.get("total") or 0) else 0),
                            "atualizado_em": agora()}
        self.st["atualizado_em"] = agora()
        self._salvar()

    def fim(self, estado: str, **extra):
        self.st.update(estado=estado, fim=agora(), atualizado_em=agora(), **extra)
        self.st["duracao_s"] = duracao_total(self.st)
        self.st["duracao_execucao_s"] = duracao_execucao(self.st)
        self._salvar()


def duracao_total(st: dict) -> int:
    """Durée de travail de l'agent pour cette mise à jour : cette exécution + celles qu'elle a reprises."""
    try:
        fim = datetime.fromisoformat(st.get("fim") or agora())
        seg = max(0, (fim - datetime.fromisoformat(st["inicio"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        seg = 0
    return int(round(seg + (st.get("duracao_anterior_s") or 0)))


def duracao_execucao(st: dict) -> int:
    """Durée de cette seule exécution (sans celles qu'elle a reprises)."""
    return duracao_total({**st, "duracao_anterior_s": 0})


SINAL_VIDA_S = 60  # battement toutes les 15 s : au-delà, le processus ne travaillait plus


def fim_trabalho(rd: Path, st: dict) -> str:
    """Heure de fin à enregistrer quand on clôt une exécution. Si le processus a disparu (ordinateur éteint, veille,
    arrêt forcé), c'est son dernier signe de vie, et non l'heure où l'on s'en aperçoit (jusqu'à INTERROMPIDO_S plus tard)."""
    marcas = []
    for k in ("vivo_em", "atualizado_em"):  # vivo_em : battement toutes les 15 s, même quand l'agent attend en silence
        try:
            marcas.append(datetime.fromisoformat(st[k]).timestamp())
        except (KeyError, TypeError, ValueError):
            pass
    if (rd / "log.jsonl").exists():
        marcas.append((rd / "log.jsonl").stat().st_mtime)
    if marcas and time.time() - max(marcas) > SINAL_VIDA_S:
        return datetime.fromtimestamp(max(marcas), timezone.utc).isoformat(timespec="seconds")
    return agora()


def uso_de(u: dict | None) -> dict:
    """Convertit un bloc usage de l'API (snake_case) ou du SDK (camelCase) vers {in,out,cw5,cw1h,cr}."""
    u = u or {}
    g = lambda *ks: next((u[k] for k in ks if u.get(k) is not None), 0) or 0
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens") or 0, cc.get("ephemeral_1h_input_tokens") or 0
    cw = g("cache_creation_input_tokens", "cacheCreationInputTokens")
    return {"in": g("input_tokens", "inputTokens"), "out": g("output_tokens", "outputTokens"),
            "cr": g("cache_read_input_tokens", "cacheReadInputTokens"),
            "cw5": w5 + max(0, cw - w5 - w1), "cw1h": w1}


def texto_guia() -> str:
    """Texte intégral du guide métier, inclus dans les instructions de l'agent (mis en cache par l'API d'un tour à
    l'autre) : l'agent ne peut pas se contenter d'extraits. Source : data/job_guidelines_v2.pdf, extrait une fois dans
    kyc_agent/prompts/guia_v2.txt (réextrait si le PDF est plus récent)."""
    txt, pdf = PROMPTS / "guia_v2.txt", ROOT / "data" / "job_guidelines_v2.pdf"
    if pdf.exists() and (not txt.exists() or txt.stat().st_mtime < pdf.stat().st_mtime):
        import pymupdf
        with pymupdf.open(pdf) as d:
            txt.write_text("\n".join(f"--- page {i + 1} ---\n" + re.sub(r"[ \t]+\n", "\n", p.get_text()) for i, p in enumerate(d)),
                           encoding="utf-8")
    corpo = txt.read_text(encoding="utf-8") if txt.exists() else "(guide introuvable : lis data/job_guidelines_v2.pdf en entier)"
    return "# GUIDE MÉTIER — texte intégral (data/job_guidelines_v2.pdf)\n\n" + corpo


def resumo_pendentes(case: Path, ped: dict) -> str:
    linhas = []
    for d in ped["pendentes"].get("respostas", []):
        p = case / "respostas" / d / "resposta.json"
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        arqs = ", ".join(a["nome"] for a in r.get("arquivos", [])) or "aucun fichier"
        if r.get("dispensa"):
            quais = f" — pendances écartées : {', '.join(r['pendencias_dispensadas'])}" if r.get("pendencias_dispensadas") else ""
            linhas.append(f"- DEMANDE ÉCARTÉE PAR LE SUPERVISEUR : {r['item']}{quais} — par {r.get('autor')}, le {r['recebida_em']} — "
                          f"motif : « {' '.join((r.get('texto') or '').split())[:600]} » (dossier `respostas/{d}/`)")
            continue
        linhas.append(f"- Réponse à {r['item']} ({r.get('rotulo') or r['acao']}), reçue le {r['recebida_em']} — "
                      f"dossier `respostas/{d}/` — texte : {len(r.get('texto') or '')} caractères — fichiers : {arqs}"
                      + (" — confirmation cochée" if r.get("confirmacao") else ""))
    for c in ped["pendentes"].get("pecas", []):
        linhas.append(f"- Nouvelle pièce du dossier : `documentos/{c}`")
    if len(linhas) > 400:  # premier dossier volumineux : le détail reste dans documentos.json
        linhas = linhas[:400] + [f"- … et {len(linhas) - 400} autres (voir documentos.json, integrado_em_versao = null)"]
    return "\n".join(linhas) or "- (aucun élément)"


def versoes(case: Path) -> list[int]:
    d = case / "versions"
    return sorted(int(p.name[1:]) for p in d.glob("v*") if p.name[1:].isdigit()) if d.is_dir() else []


# ------------------------------------------------------------------ simulation (sans clé API)
async def simular(reg: Registro, ped: dict, vnext: int):
    if vnext == 1:  # première analyse
        etapas = ["Lecture de data/format_output.txt, du guide et du schéma", "Extraction du texte des pièces (kyc_agent.extract)",
                  "Identification des parties et des flux", "Téléchargement des listes officielles (kyc_agent.fetch_sources)",
                  "Screening OFAC / ONU / PEP / CEIS / CNEP", "Recherche média adverse", "Rédaction de state.json (ciclo_v1.py)",
                  "Génération des livrables v1 (kyc_agent.build)", "Traduction française (i18n_fr.json)", "Résumé pour le superviseur"]
    else:
        etapas = ["Lecture de data/format_output.txt et de l'état de la version actuelle",
              *[f"Lecture de l'élément en attente : {x}" for x in (ped["pendentes"]["respostas"] + ped["pendentes"]["pecas"])[:6]],
              "Rapprochement avec les pendances ouvertes de la matrice externe",
              f"Rédaction du script de cycle ciclo_v{vnext}.py", f"Génération des livrables v{vnext} (kyc_agent.build)",
              "Traduction française des nouveaux textes (i18n_fr.json)", "Résumé pour le superviseur"]
    uso = dict.fromkeys(CAMPOS, 0)
    ag = Agentes(reg)
    n = len(etapas)
    reg.journal.ev("inicio", modelo="SIMULATION (aucun appel à l'API)", versao_origem=vnext - 1, versao_alvo=vnext, pedido="[SIMULATION] " + " ; ".join(etapas))
    for i, e in enumerate(etapas, 1):
        reg.plano([{"content": x, "status": "completed" if j < i else ("in_progress" if j == i else "pending"), "activeForm": x}
                   for j, x in enumerate(etapas, 1)])
        if i == 3:
            ag.reservar("sim1", "[SIMULATION] lecture des pièces — lot 1")
            ag.reservar("sim2", "[SIMULATION] lecture des pièces — lot 2")
        if i == n - 3:
            ag.terminou(tool_use_id="sim1")
            ag.terminou(tool_use_id="sim2")
        reg.log(f"[SIMULATION] {e}")
        reg.journal.ev("texto", agente="subagente: [SIMULATION] lecture des pièces — lot 1" if 3 <= i < n - 3 and i % 2 else None,
                       texto=f"[SIMULATION] {e}")
        uso["cw1h"] += 9000 if i == 1 else 1500
        uso["cr"] += 30000 + 4000 * i
        uso["in"] += 300
        uso["out"] += 900
        reg.uso(dict(uso))
        for _ in range(int(float(os.environ.get("KYC_AGENT_SIM_PASSO", "2.5")) * 10)):
            if parada_pedida(reg.dir):
                raise Parado("Simulation arrêtée par l'utilisateur.")
            await asyncio.sleep(0.1)
    reg.plano([{"content": x, "status": "completed"} for x in etapas])
    ag.fechar()
    reg.log("[SIMULATION] Terminé — aucun appel à l'API, aucune version créée, dossier inchangé.")
    reg.journal.ev("fim", estado="simulado")
    reg.fim("simulado")


# ------------------------------------------------------------------ exécution réelle (Claude Agent SDK)
async def executar(case: Path, run_dir: Path, reg: Registro, ped: dict, vn: int, vnext: int):
    from claude_agent_sdk import (AgentDefinition, AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                                  PermissionResultAllow, PermissionResultDeny, ResultMessage, TaskNotificationMessage,
                                  TaskStartedMessage, TaskUpdatedMessage, TextBlock, ThinkingBlock, ToolResultBlock,
                                  ToolUseBlock, UserMessage)

    case_r = case.resolve()
    ag = Agentes(reg)

    modo = modo_execucao(ped)
    # le dossier peut vivre hors du dépôt (DATA_DIR) : on donne à l'agent son chemin réel
    try:
        rel = case_r.relative_to(ROOT).as_posix()
    except ValueError:
        rel = case_r.as_posix()
    sistema = (PROMPTS / "sistema.md").read_text(encoding="utf-8").replace("cases/<ID>", rel).replace("<ID>", case.name)
    sistema += "\n\n" + (PROMPTS / "metodo.md").read_text(encoding="utf-8").replace("cases/<ID>", rel).replace("<ID>", case.name)
    sistema += "\n\n" + texto_guia()

    permissao = restringir_subagentes(criar_permissao(case, vn, reg, ag), modo, reg)
    if modo == MODO_MULTI:
        agents = definir_subagentes(sistema, rel, AgentDefinition)
        multi = (PROMPTS / "multi_agentes.md").read_text(encoding="utf-8").replace("{max}", str(MAX_SUBAGENTES)) + "\n"
    else:  # agent libre : comportement d'origine, l'agent décide seul s'il lance des sous-agents
        agents = {"analista-kyc": AgentDefinition(
            description="Analyste KYC qui traite un sous-ensemble des éléments (lecture des pièces, contrôles, screening, recherches) et rend ses constats.",
            prompt=(PROMPTS / "sistema.md").read_text(encoding="utf-8") +
            "\n\nTu es un sous-agent : tu analyses uniquement les éléments qu'on te confie et tu rends tes constats "
            "(faits, sources, effets sur les pendances). Tu n'écris ni state.json ni livrables.",
            tools=["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch"], model="inherit")}
        multi = (f"Tu peux, si le volume ou la diversité des éléments le justifie, confier la lecture et l'analyse de lots d'éléments à des "
                 f"sous-agents « analista-kyc » lancés en parallèle (au maximum {MAX_SUBAGENTES} simultanément), puis consolider toi-même "
                 f"l'état et les livrables. Pour peu d'éléments, travaille seul.\n")
    multi += ("Sous-agents ou commandes lancés en arrière-plan : tu peux terminer ton tour pour attendre leurs résultats ; "
              "tu seras relancé à leur fin. Ton travail n'est terminé que lorsque la version et ses livrables sont écrits.\n")
    multi += ("Plan de travail (sert à afficher la progression au compliance) : dès le début, avant toute autre action, établis avec "
              "l'outil TodoWrite ton plan COMPLET de la mise à jour (environ 15 à 30 tâches concrètes, jusqu'à l'écriture des "
              "livrables et de la traduction), puis tiens-le à jour tout au long du travail : passe chaque tâche « in_progress » "
              "quand tu la commences et « completed » dès qu'elle est faite ; ajoute les tâches nouvelles dès que tu les découvres. "
              "Ne marque jamais « completed » une tâche non faite.\n")

    modelo_prompt = "atualizacao.md" if vn else "analise_inicial.md"  # même bouton : première analyse ou mise à jour
    prompt = (PROMPTS / modelo_prompt).read_text(encoding="utf-8").format(
        cid=case.name, vn=vn, vnext=vnext, hoje=datetime.now().strftime("%Y-%m-%d"),
        pendentes=resumo_pendentes(case, ped), multi=multi)
    prompt = prompt.replace(f"cases/{case.name}", rel).replace("cases/<ID>", rel)
    # passé par fichier : en ligne de commande, le guide complet dépasse la limite de Windows (~32 000 caractères, WinError 206)
    arq_sistema = run_dir / "prompt_sistema.md"
    arq_sistema.write_text(sistema, encoding="utf-8")
    opts = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(arq_sistema)},
        model=MODEL, cwd=str(ROOT), setting_sources=[], can_use_tool=permissao, agents=agents,
        allowed_tools=sorted(LIVRES), max_budget_usd=float(os.environ["KYC_AGENT_MAX_USD"]) if os.environ.get("KYC_AGENT_MAX_USD") else None,
        env={"PYTHONIOENCODING": "utf-8", "ANTHROPIC_API_KEY": chave_api()}, stderr=lambda s: _anexar(run_dir / "agent_stderr.log", s),
        # une page scannée lue comme image arrive en base64 dans un seul message : la limite par défaut (1 Mo) est trop basse
        max_buffer_size=TAMPAO_MENSAGEM, session_id=reg.st.get("sessao"))
    if ped.get("reconsultas"):
        linhas = "\n".join(f"- {', '.join(e['sites'])} ({e['ferramenta']}, {e['em']}) : {e['trecho'][:300]}" for e in ped["reconsultas"])
        prompt += ("\n\nCONSULTATIONS À REFAIRE : lors d'une exécution précédente, ces envois de données ont été refusés car le site "
                   "n'était pas dans la liste blanche. Le superviseur a depuis autorisé ces sites. Refais ces consultations "
                   "(en n'envoyant que le strict nécessaire), conserve les preuves, mets à jour les contrôles concernés, clôture ou "
                   "ajuste les tâches humaines correspondantes du registre technique et explique les changements dans "
                   f"l'historique et la Nota_FR. Si aucune autre pièce n'est en attente, limite la mise à jour à ces consultations.\n{linhas}")
    if (ped.get("requete") or {}).get("texto"):
        rq = ped["requete"]
        prompt += ("\n\nREQUÊTE DE MISE À JOUR DU SUPERVISEUR (" + str(rq.get("por") or "superviseur") + ", " + str(rq.get("em") or "") + ") :\n"
                   "<<<\n" + rq["texto"] + "\n>>>\n"
                   "Traite cette requête dans cette mise à jour (avec les pièces en attente s'il y en a ; sinon, limite-toi à la requête). "
                   "Elle ne lève aucune des règles ci-dessus (envois, contrôles non effectués, validation humaine finale, guide). "
                   "Si elle demande de trancher un point sans preuve suffisante, ne présente pas cela comme ton constat : inscris-le comme "
                   "« décision du superviseur » (auteur, date, motif) et signale l'écart avec le guide. Si elle est ambiguë, fais au mieux et "
                   "dis ce que tu as compris. Dans l'historique de la version et la Nota_FR, ajoute un paragraphe « Requête du superviseur » : "
                   "le texte, ce que tu as fait, ce que tu n'as pas fait et pourquoi.")
    ret = ped.get("retomada") or {}
    if ret:
        anterior = ret.get("sessao")
        if anterior and sessao_disponivel(anterior):
            # la conversation précédente est rechargée, copiée sous le nouvel identifiant (fork)
            opts.resume, opts.fork_session = anterior, True
            prompt = (PROMPTS / "retomada.md").read_text(encoding="utf-8").format(
                motivo=ret.get("motivo") or "?", vn=vn, vnext=vnext, pendentes=resumo_pendentes(case, ped)).replace(f"cases/{case.name}", rel)
            reg.log("Reprise de la conversation de l'agent là où elle s'était arrêtée")
        else:
            prompt += ("\n\nREPRISE : une exécution précédente de cette mise à jour a été interrompue "
                       f"({ret.get('motivo') or '?'}). Son travail partiel a été restauré dans {rel}/work/ (et, s'ils existent, "
                       f"state.json et versions/v{vnext}/). Commence par l'examiner : réutilise ce qui est fait et correct, "
                       "ne refais pas les lectures et conversions déjà faites, puis termine la mise à jour.")
            reg.log("Conversation précédente indisponible : l'agent repart du travail restauré")

    vistos, uso, resultado = {}, dict.fromkeys(CAMPOS, 0), None
    t0 = time.time()
    J = reg.journal
    J.ev("inicio", limite=400_000, modelo=MODEL, versao_origem=vn, versao_alvo=vnext, ferramentas_livres=sorted(LIVRES),
         sessao=reg.st.get("sessao"), retomada=ret or None, sessao_retomada=opts.resume,
         subagentes_max=MAX_SUBAGENTES if agents else 0, orcamento_usd=opts.max_budget_usd, prompt_sistema=sistema, pedido=prompt,
         modo=modo, subagentes={k: {"descricao": a.description, "ferramentas": a.tools} for k, a in (agents or {}).items()})
    missao = {}  # tool_use_id d'un outil Agent/Task -> mission du sous-agent
    fundo = set()  # task_id des commandes lancées en arrière-plan (pas des sous-agents)

    def quem(parent):
        return f"subagente: {missao.get(parent, parent)}" if parent else None

    # Suivi du tour de l'agent principal et des tâches en arrière-plan (sous-agents, commandes). L'agent principal
    # peut terminer son tour (« j'attends leurs résultats ») alors que des sous-agents travaillent encore : ce n'est
    # pas la fin de l'exécution. On attend leur fin ; le moteur relance normalement de lui-même l'agent principal avec
    # leurs résultats, sinon on le relance nous-mêmes.
    seg = {"seq": 0, "fim": -1, "res": -1, "ocioso": False, "relances": 0, "relances_versao": 0}
    estado_alvo = case / "versions" / f"v{vnext}" / f"state_v{vnext}.json"
    TEXTO_SEM_VERSAO = (
        f"Ton tour est terminé mais la version v{vnext} n'est pas écrite (versions/v{vnext}/state_v{vnext}.json absent). "
        "Si tu attendais des sous-agents : vérifie s'ils ont écrit leur fichier de résultat (work/lots/) ; s'ils se sont "
        "arrêtés sans le faire, relance-les ou fais leur travail toi-même. Puis mène le travail jusqu'au bout : state, "
        "livrables et autocontrôle de la version.")
    fundo_ativos, notifs, resultados = set(), [], []

    def pendentes() -> int:
        return ag.n_ativos() + len(fundo_ativos)

    def texto_relance() -> str:
        linhas = [f"- {n['descricao']} ({n['status']}) : {n['resumo'] or '—'}" + (f" — sortie : {n['saida']}" if n["saida"] else "")
                  for n in notifs] or ["- (aucun détail transmis par le moteur)"]
        return ("Les sous-agents / tâches lancés en arrière-plan sont terminés :\n" + "\n".join(linhas) + "\n\n"
                "Reprends maintenant : lis leurs résultats (fichiers de résultat sous work/lots/ et sorties ci-dessus), "
                "consolide, et mène le travail jusqu'au bout (state, livrables et autocontrôle de la version "
                f"v{vnext}). Si tu relances des sous-agents, tu seras de nouveau relancé à leur fin.")

    async def conversa():
        async with ClaudeSDKClient(options=opts) as client:
            novo = asyncio.Event()  # un message vient d'être traité (ou le flux s'est fermé)

            async def bomba():  # traite chaque message dans l'ordre, dès son arrivée
                try:
                    async for m in client.receive_messages():
                        seg["seq"] += 1
                        antes = pendentes()
                        tratar(m)
                        if pendentes() < antes:
                            seg["fim"] = seg["seq"]
                        if isinstance(m, ResultMessage) and pendentes():
                            reg.log(f"L'agent principal attend la fin de {pendentes()} sous-agent(s)/tâche(s) en arrière-plan.")
                        novo.set()
                finally:
                    seg["fechado"] = True
                    novo.set()

            await client.query(prompt)
            tb = asyncio.create_task(bomba())
            try:
                while True:
                    fim_possivel = seg["ocioso"] and not pendentes()
                    try:
                        await asyncio.wait_for(novo.wait(), GRACA_S if fim_possivel else SILENCIO_MAX_S)
                    except asyncio.TimeoutError:
                        if not fim_possivel:
                            raise RuntimeError(
                                f"Aucune nouvelle depuis {SILENCIO_MAX_S // 60} min alors que {pendentes()} sous-agent(s)/tâche(s) "
                                f"tournent encore ({', '.join(ag.ativos.values()) or 'commandes en arrière-plan'}).")
                        if seg["fim"] > seg["res"]:  # tâches finies après le dernier tour, et le moteur n'a pas relancé
                            if seg["relances"] >= MAX_RELANCES:
                                raise RuntimeError(f"L'agent principal a été relancé {MAX_RELANCES} fois sans terminer.")
                            seg["relances"] += 1
                            reg.log("Sous-agents terminés : relance de l'agent principal pour la suite du travail.")
                            J.ev("relance", motivo="sous-agents terminés après la fin du tour", texto=texto_relance())
                            seg["ocioso"] = False
                            await client.query(texto_relance())
                            notifs.clear()
                            continue
                        if not estado_alvo.exists() and seg["relances_versao"] < MAX_RELANCES_VERSAO:
                            # tour fini, rien ne tourne en apparence, et pas de version : on ne conclut pas trop vite
                            seg["relances_versao"] += 1
                            reg.log(f"Tour terminé sans version v{vnext} : relance de l'agent principal "
                                    f"({seg['relances_versao']}/{MAX_RELANCES_VERSAO}).")
                            J.ev("relance", motivo="tour terminé sans version", texto=TEXTO_SEM_VERSAO)
                            seg["ocioso"] = False
                            await client.query(TEXTO_SEM_VERSAO)
                            continue
                        return  # tour terminé, plus rien ne tourne : fin normale
                    novo.clear()
                    if seg.get("fechado"):
                        await tb  # relève une éventuelle erreur du flux
                        return
            finally:
                tb.cancel()
                await asyncio.gather(tb, return_exceptions=True)

    def tratar(m):
        nonlocal uso, resultado
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None:
            seg["ocioso"] = False
            notifs.clear()  # l'agent principal a repris : il a reçu les résultats
        if isinstance(m, AssistantMessage):
            if m.usage and m.message_id:
                vistos[m.message_id] = uso_de(m.usage)
                uso = {k: sum(u[k] for u in vistos.values()) for k in CAMPOS}
                reg.uso(uso)
            for b in m.content:
                if isinstance(b, ToolUseBlock) and b.name in OUTILS_SUBAGENTE:
                    ag.reservar(b.id, b.input.get("description") or "sous-agent")
                    missao[b.id] = b.input.get("description") or b.input.get("subagent_type") or b.id
                quem_m = quem(m.parent_tool_use_id)
                if isinstance(b, ToolUseBlock):
                    J.ev("ferramenta", agente=quem_m, id=b.id, nome=b.name, parametros=b.input)
                    if b.name == "TodoWrite" and m.parent_tool_use_id is None:  # plan de l'agent chef → progression
                        reg.plano(b.input.get("todos"))
                elif isinstance(b, TextBlock) and b.text.strip():
                    J.ev("texto", agente=quem_m, texto=b.text)
                elif isinstance(b, ThinkingBlock) and b.thinking.strip():
                    J.ev("raciocinio", agente=quem_m, texto=b.thinking)
                if isinstance(b, ToolUseBlock):
                    alvo = b.input.get("file_path") or b.input.get("command") or b.input.get("pattern") or b.input.get("url") or b.input.get("description") or ""
                    reg.log(f"{b.name} · {str(alvo)[:160]}")
                elif isinstance(b, TextBlock) and b.text.strip():
                    reg.log(b.text.strip().splitlines()[0][:200])
        elif isinstance(m, TaskStartedMessage):
            ag.iniciou(m.task_id, m.tool_use_id, m.description, m.task_type)
            # le moteur signale aussi par ce message les commandes lancées en arrière-plan : ce ne sont pas des sous-agents
            sub = m.tool_use_id in missao or bool(m.task_type and "agent" in m.task_type.lower())
            if not sub:
                fundo.add(m.task_id)
                fundo_ativos.add(m.task_id)
            J.ev("subagente_inicio" if sub else "tarefa_inicio", agente=quem(m.tool_use_id) if sub else None,
                 tarefa=m.task_id, descricao=m.description, **({} if sub else {"ferramenta_id": m.tool_use_id, "tipo_tarefa": m.task_type}))
        elif isinstance(m, TaskNotificationMessage):
            ag.terminou(task_id=m.task_id, tool_use_id=m.tool_use_id)
            fundo_ativos.discard(m.task_id)
            sub = m.task_id not in fundo
            notifs.append({"descricao": missao.get(m.tool_use_id) or m.task_id, "status": getattr(m, "status", None),
                           "resumo": (getattr(m, "summary", None) or "")[:600], "saida": getattr(m, "output_file", None)})
            J.ev("subagente_fim" if sub else "tarefa_fim", agente=quem(m.tool_use_id) if sub else None, tarefa=m.task_id,
                 status=getattr(m, "status", None), resumo=getattr(m, "summary", None))
        elif isinstance(m, TaskUpdatedMessage):
            if m.status in ("completed", "failed", "killed"):
                ag.terminou(task_id=m.task_id)
                fundo_ativos.discard(m.task_id)
        elif isinstance(m, UserMessage) and isinstance(m.content, list):
            for b in m.content:  # résultat de l'outil Agent/Task = fin du sous-agent (filet de sécurité)
                if isinstance(b, ToolResultBlock):
                    # un sous-agent lancé en arrière-plan rend aussitôt « Async agent launched » : il n'est pas fini,
                    # sa fin arrive par TaskNotification / TaskUpdated (sinon le compteur retombait à 1 en 20 ms)
                    # seulement pour l'outil qui lance un sous-agent (Agent/Task) : le résultat d'une reprise par
                    # SendMessage (« Resuming agent ») arrive aussitôt, alors que le sous-agent repart à peine (TEST3)
                    if b.tool_use_id in missao and not lancado_em_fundo(b, ag):
                        ag.terminou(tool_use_id=b.tool_use_id)
                    J.ev("resultado_ferramenta", agente=quem(m.parent_tool_use_id), id=b.tool_use_id, erro=bool(b.is_error),
                         conteudo=texto_de_resultado(b.content))
                elif isinstance(b, TextBlock) and b.text.strip():
                    J.ev("texto", agente=quem(m.parent_tool_use_id), origem="mensagem recebida", texto=b.text)
        elif isinstance(m, ResultMessage):
            resultado = m
            resultados.append(m)
            seg["ocioso"], seg["res"] = True, seg["seq"]
            J.ev("resultado", subtipo=m.subtype, erro=m.is_error, custo_usd=m.total_cost_usd, turnos=m.num_turns,
                 duracao_ms=m.duration_ms, texto=m.result or "")

    async def vigia():  # battement de cœur, demande d'arrêt, remise en état faite par l'application
        n = 0
        while True:
            await asyncio.sleep(VIGIA_S)
            if reg.revertido_por_terceiro():
                raise Interrompido("Exécution abandonnée : le dossier a déjà été remis en état par l'application.")
            if parada_pedida(run_dir):
                raise Parado("Mise à jour arrêtée par l'utilisateur.")
            n += VIGIA_S
            if n >= BATIMENTO_S:
                n = 0
                reg.batimento()

    tc, tv = asyncio.create_task(conversa()), asyncio.create_task(vigia())
    feito, _ = await asyncio.wait({tc, tv}, return_when=asyncio.FIRST_COMPLETED)
    for t in (tc, tv):
        if t not in feito:
            t.cancel()
    await asyncio.gather(tc, tv, return_exceptions=True)
    for t in feito:
        if t.exception():
            raise t.exception()

    if resultado is None:
        raise RuntimeError("L'agent s'est arrêté sans résultat.")
    # plusieurs tours (relances après les sous-agents) : coût et usage cumulés sur la session, ou par tour ?
    cumul = cumulativos(resultados)
    base = resultados[-1:] if cumul else resultados
    if all(r.model_usage for r in base):  # total exact (agent principal + sous-agents)
        uso = {k: 0 for k in CAMPOS}
        for r in base:
            for mu in r.model_usage.values():
                for k, x in uso_de(mu).items():
                    uso[k] += x
    elif all(r.usage for r in base):
        uso = {k: sum(uso_de(r.usage)[k] for r in base) for k in CAMPOS}
    custos = [r.total_cost_usd for r in base if r.total_cost_usd is not None]
    custo = sum(custos) if custos else None
    if len(resultados) > 1:
        J.ev("consolidacao_custo", tours=len(resultados), cumulativo=cumul,
             custos=[r.total_cost_usd for r in resultados], custo_retido=custo)
    reg.uso(uso, custo)
    (run_dir / "resultado.md").write_text(resultado.result or "", encoding="utf-8")
    if resultado.is_error:
        raise RuntimeError(f"Exécution en erreur ({resultado.subtype}) : {(resultado.result or '')[:300]}")

    man_p = run_dir / "reversao" / "manifesto.json"
    if man_p.exists():
        alteradas = pecas_alteradas(case, json.loads(man_p.read_text(encoding="utf-8")))
        if alteradas:
            raise RuntimeError(f"Pièces du dossier modifiées pendant l'exécution (interdit) : {', '.join(alteradas[:5])}")
    vdir = case / "versions" / f"v{vnext}"
    if not (vdir / f"state_v{vnext}.json").exists():
        raise RuntimeError(f"L'agent a terminé sans produire versions/v{vnext}/state_v{vnext}.json.")
    (vdir / "metricas.json").write_text(json.dumps({
        "medido": True, "modelo": MODEL, "uso": uso, "custo_usd": custo,
        "duracao_s": round(time.time() - t0 + (reg.st.get("duracao_anterior_s") or 0)),
        "duracao_execucao_s": round(time.time() - t0),  # cette tentative seule (duracao_s inclut les tentatives reprises)
        **({"retomada_de": ret.get("de"), "custo_execucoes_anteriores_usd": ret.get("custo_anterior_usd")} if ret else {}),
        "inicio": reg.st["inicio"], "fim": agora(), "turnos": resultado.num_turns,
        "agentes": {"pico_simultaneos": ag.pico, "subagentes_lancados": ag.lancados}, "modo": modo,
        "envios_recusados": reg.st.get("envios_recusados", []),
        "fonte": "Claude Agent SDK — ResultMessage (model_usage, total_cost_usd)", "execucao": run_dir.name},
        ensure_ascii=False, indent=2), encoding="utf-8")
    marcar_integrados(case, ped, vnext)
    reg.log(f"Version v{vnext} produite.")
    ag.fechar()
    J.ev("fim", estado="concluido", versao_produzida=vnext, custo_usd=custo)
    shutil.copyfile(J.p, vdir / NOME_VERSAO)  # copie figée : le journal accompagne la version qu'il a produite
    reg.fim("concluido", versao_produzida=vnext, custo_usd=custo, resumo=(resultado.result or "")[:4000])


def base_pecas(case: Path) -> Path:
    """Dossier où sont rangées les pièces du dossier (documentos.json « base », par défaut cases/<ID>/documentos)."""
    try:
        base = json.loads((case / "documentos.json").read_text(encoding="utf-8")).get("base") or "documentos"
    except (OSError, ValueError):
        base = "documentos"
    p = Path(base)
    if p.is_absolute():
        return p.resolve()
    return (case / p).resolve() if (case / p).exists() or base == "documentos" else (case.parents[1] / p).resolve()


def zonas_leitura(case: Path) -> list[Path]:
    case_r = case.resolve()
    return [case_r, base_pecas(case_r), *[(ROOT / r).resolve() for r in REFERENCIAS]]


def _dentro(p: Path, zonas) -> bool:
    return any(p == z or z in p.parents for z in zonas)


def _caminho(inp: dict, *chaves) -> Path | None:
    for k in chaves:
        if inp.get(k):
            p = Path(inp[k])
            return (ROOT / p).resolve() if not p.is_absolute() else p.resolve()
    return None


def criar_permissao(case: Path, vn: int, reg: "Registro", ag: "Agentes"):
    """Règles d'accès de l'agent (callback can_use_tool du SDK) :
    - lecture de fichiers : uniquement le dossier traité, ses pièces et les références communes (guide, format, outils) ;
    - écriture : uniquement l'état de travail, work/ et la nouvelle version du dossier ;
    - pièces du dossier : jamais modifiées, déplacées ni supprimées ;
    - shell : pas d'effacement massif, d'envoi externe, de référence à un autre dossier ni d'écriture sur les pièces."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    case_r = case.resolve()
    zonas = zonas_leitura(case_r)
    pecas = base_pecas(case_r)
    casos = case_r.parent
    outros = [p.name for p in casos.iterdir() if p.is_dir() and p.name != case_r.name] if casos.is_dir() else []
    # nom EXACT d'un autre dossier (pas un préfixe : « TC-187 » ne doit pas bloquer « TC-187-TEST »)
    lista_branca = carregar_lista_branca()  # lue une fois : l'agent ne peut pas l'élargir pendant son travail
    re_outros = re.compile(r"(cases[\\/](%s)(?![A-Za-z0-9._-]))" % "|".join(map(re.escape, outros)), re.I) if outros else None
    def nega(motivo, log):
        reg.log(log)
        reg.journal.ev("recusa", agente=getattr(permissao, "_agente", None), ferramenta=permissao._tool, parametros=permissao._inp,
                       motivo=motivo)
        return PermissionResultDeny(message=motivo)

    async def permissao(tool, inp, ctx):
        permissao._tool, permissao._inp = tool, inp
        if tool in ("Task", "Agent"):  # sous-agent : l'agent principal décide, le worker plafonne
            if ag.n_ativos() >= MAX_SUBAGENTES:
                return nega(f"Maximum {MAX_SUBAGENTES} sous-agents simultanés : attends qu'un sous-agent termine.",
                            f"Sous-agent refusé : déjà {MAX_SUBAGENTES} en parallèle")
            ag.reservar(getattr(ctx, "tool_use_id", None), inp.get("description") or inp.get("subagent_type") or "sous-agent")
            return PermissionResultAllow()
        if tool in LIVRES:
            return PermissionResultAllow()
        if tool in LEITURA_LOCAL:
            alvo = _caminho(inp, "file_path", "path", "notebook_path")
            if alvo is None:
                return nega(f"Indique explicitement le dossier à parcourir (paramètre path), dans {case_r} ou ses pièces.",
                            f"{tool} refusé : chemin non précisé")
            if not _dentro(alvo, zonas) or CODIGO_DE_DOSSIE.search(alvo.name):
                return nega(f"Lecture interdite hors du dossier {case_r.name} : seules ses pièces, son état et les références "
                            f"communes (guide, format_output, outils kyc_agent) sont accessibles.",
                            f"Lecture refusée hors du dossier : {alvo}")
            return PermissionResultAllow()
        if tool in ESCRITA:
            alvo = _caminho(inp, "file_path", "notebook_path") or Path("")
            conteudo = "\n".join(str(inp.get(k) or "") for k in ("content", "new_string", "new_source"))
            conteudo += "\n".join(str(e.get("new_string") or "") for e in inp.get("edits") or [] if isinstance(e, dict))
            motivo_envio = envio_nao_autorizado(conteudo, lista_branca)
            if motivo_envio:
                registrar_envio_recusado(reg, sites_recusados(conteudo, lista_branca), tool, f"{alvo.name} : {conteudo[:300]}",
                                         getattr(permissao, "_agente", None))
                return nega(f"Script refusé : {motivo_envio}. Les envois de données ne sont permis que vers les sites officiels "
                            f"de kyc_agent/dominios_oficiais.json ; sinon, crée une tâche humaine.", f"Script refusé ({motivo_envio[:80]}) : {alvo}")
            congeladas = [case_r / "versions" / f"v{x}" for x in range(1, vn + 1)] + [case_r / p for p in PROTEGIDOS] + [pecas]
            if case_r in alvo.parents and not any(c == alvo or c in alvo.parents for c in congeladas):
                return PermissionResultAllow()
            return nega(f"Écriture interdite : seuls state.json, work/ et la nouvelle version de cases/{case_r.name}/ peuvent être "
                        f"écrits ; les pièces du dossier sont en lecture seule, les versions antérieures sont figées.",
                        f"Écriture refusée : {alvo}")
        if tool == "Bash":
            cmd = inp.get("command", "")
            if BASH_INTERDIT.search(cmd):
                return nega("Commande interdite (effacement massif, git ou envoi d'e-mail).", f"Commande refusée : {cmd[:120]}")
            motivo_envio = envio_nao_autorizado(cmd, lista_branca)
            if motivo_envio:
                registrar_envio_recusado(reg, sites_recusados(cmd, lista_branca), tool, cmd, getattr(permissao, "_agente", None))
                return nega(f"Commande refusée : {motivo_envio}. Les envois de données ne sont permis que vers les sites officiels "
                            f"de kyc_agent/dominios_oficiais.json ; sinon, crée une tâche humaine.", f"Commande refusée ({motivo_envio[:80]}) : {cmd[:120]}")
            if re_outros and re_outros.search(cmd):
                return nega(f"Commande refusée : elle vise un autre dossier que {case_r.name}.", f"Commande refusée (autre dossier) : {cmd[:120]}")
            if CODIGO_DE_DOSSIE.search(cmd):
                return nega("Commande refusée : script propre à un autre dossier.", f"Commande refusée : {cmd[:120]}")
            if escreve_nas_pecas(cmd, pecas, case_r):
                return nega("Commande refusée : les pièces du dossier sont en lecture seule (écris tes extraits dans work/).",
                            f"Commande refusée (écriture sur les pièces) : {cmd[:120]}")
            return PermissionResultAllow()
        return nega(f"Outil {tool} non autorisé pour cet agent.", f"Outil refusé : {tool}")

    return permissao


def escreve_nas_pecas(cmd: str, pecas: Path, case_r: Path) -> bool:
    """Heuristique : la commande modifie-t-elle un fichier situé dans le dossier des pièces ?
    (verbe d'effacement/déplacement/modification + chemin des pièces, ou redirection « > » vers ce chemin)."""
    c = cmd.replace("\\", "/")
    formas = {pecas.as_posix()}
    try:
        formas.add(pecas.relative_to(ROOT).as_posix())
    except ValueError:
        pass
    try:
        formas.add(pecas.relative_to(case_r).as_posix() + "/")
    except ValueError:
        pass
    formas.add("documentos/")
    alvo_pecas = lambda txt: any(fm.lower() in txt.lower() for fm in formas)
    for m in re.finditer(r">{1,2}\s*[\"']?([^\s\"'|;&]+)", c):  # redirections
        if alvo_pecas(m.group(1)):
            return True
    verbos = BASH_ESCRITA.pattern.replace("|>{1,2}", "")
    return bool(re.search(verbos, c, re.I)) and alvo_pecas(c)


def proteger_pecas(case: Path, rd: Path, ativar: bool):
    """Pièces du dossier en lecture seule (attribut système) pendant l'exécution ; la liste est gardée dans le run
    pour pouvoir lever la protection même si le worker disparaît."""
    lista = rd / "pecas_protegidas.json"
    if ativar:
        base = base_pecas(case)
        arqs = [str(p) for p in base.rglob("*") if p.is_file()] if base.is_dir() else []
        lista.write_text(json.dumps(arqs, ensure_ascii=False), encoding="utf-8")
        for a in arqs:
            try:
                os.chmod(a, stat.S_IREAD)
            except OSError:
                pass
        return len(arqs)
    try:
        arqs = json.loads(lista.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    for a in arqs:
        try:
            os.chmod(a, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
    lista.unlink(missing_ok=True)
    return len(arqs)


def pecas_alteradas(case: Path, man: dict) -> list[str]:
    """Pièces existantes avant l'exécution dont la taille ou la date a changé (ne devrait jamais arriver)."""
    out = []
    for k, (tam, mt) in man.get("pecas", {}).items():
        p = Path(k)
        if not p.exists() or p.stat().st_size != tam or p.stat().st_mtime_ns != mt:
            out.append(k)
    return out


def parada_pedida(rd: Path) -> bool:
    return (rd / "parar.json").exists()


def _anexar(p: Path, s: str):
    with p.open("a", encoding="utf-8") as f:
        f.write(s + "\n")


class Agentes:
    """Compte les agents au travail : l'agent principal + les sous-agents qu'il lance (outil Agent/Task).
    Un sous-agent est identifié par l'id de l'appel d'outil qui le lance ; sa fin arrive par task_notification,
    task_updated (statut terminal) ou, à défaut, par le résultat de l'appel d'outil."""

    def __init__(self, reg: Registro):
        self.reg, self.ativos, self.task_tool, self.pico, self.lancados, self.fim = reg, {}, {}, 1, 0, set()
        self._salvar()

    def n_ativos(self) -> int:
        return len(self.ativos)

    def reservar(self, tool_use_id, descricao):
        if tool_use_id and tool_use_id not in self.ativos and tool_use_id not in self.fim:
            self.ativos[tool_use_id] = str(descricao)[:120]
            self.lancados += 1
            self.pico = max(self.pico, 1 + len(self.ativos))
            self.reg.log(f"Sous-agent lancé : {descricao}")
            self._salvar()

    def iniciou(self, task_id, tool_use_id, descricao, task_type):
        if tool_use_id in self.ativos:
            self.task_tool[task_id] = tool_use_id
        elif task_type and "agent" in task_type.lower():
            self.reservar(tool_use_id or task_id, descricao)
            self.task_tool[task_id] = tool_use_id or task_id

    def terminou(self, task_id=None, tool_use_id=None):
        chave = tool_use_id if tool_use_id in self.ativos else self.task_tool.get(task_id)
        if chave in self.ativos:
            desc = self.ativos.pop(chave)
            self.fim.add(chave)
            self.reg.log(f"Sous-agent terminé : {desc}")
            self._salvar()

    def fechar(self):
        self.ativos.clear()
        self._salvar()

    def _salvar(self):
        self.reg.st["agentes"] = {"ativos": 1 + len(self.ativos), "subagentes": list(self.ativos.values()),
                                  "pico_simultaneos": self.pico, "subagentes_lancados": self.lancados}
        self.reg._salvar()


def cumulativos(resultados: list) -> bool:
    """Les résultats successifs d'une même session portent-ils un coût cumulé (chaque tour >= le précédent) ?
    Sinon, ce sont des coûts par tour, à additionner."""
    cs = [r.total_cost_usd for r in resultados if r.total_cost_usd is not None]
    return len(cs) < 2 or all(b >= a for a, b in zip(cs, cs[1:]))


def lancado_em_fundo(b, ag: "Agentes") -> bool:
    """Résultat d'un outil Agent/Task qui signale un lancement asynchrone (le sous-agent continue de travailler)."""
    if b.is_error:
        return False
    txt = texto_de_resultado(b.content) if b.content is not None else ""
    # seuls ces messages sont sûrs : un sous-agent synchrone, lui, se termine bien avec le résultat de l'outil
    t = str(txt).lower()
    return "async agent launched" in t or "resuming agent" in t or "resumedagentid" in t


MODO_AGENTE, MODO_MULTI = "agente", "multi"
MODOS = (MODO_AGENTE, MODO_MULTI)

# sous-agents spécialisés du mode multi-agents : chacun reçoit les règles, la méthode (principe de prudence) et le guide
SUBAGENTES = {
    "leitor-pecas": (
        "Lit un lot de pièces du dossier (texte, scans, images) et en extrait tous les faits utiles au KYC. À utiliser pour "
        "la lecture des pièces, par lot cohérent, en parallèle.",
        ["Read", "Glob", "Grep", "Bash", "Write"],
        "Tu lis intégralement les pièces de ton lot (chaque page ; image si le texte manque) et tu relèves tous les faits utiles : "
        "identités, numéros (CPF, CNPJ, matrícula, CIB/CCIR), surfaces, montants, dates, signataires, mentions manuscrites, "
        "ratures, incohérences avec les autres données connues, pièces étrangères à l'opération."),
    "screening-parte": (
        "Screening d'UNE partie (sanctions, PEP, listes, processus, média adverse) selon la méthode fixée. À utiliser une fois "
        "par partie, en parallèle.",
        ["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch", "Write"],
        "Tu fais le screening complet de la partie confiée : listes de sanctions (fichiers officiels et OFAC Sanctions List "
        "Search), PEP, processus judiciaires (DataJud), média adverse en portugais (media_log). Méthode fixée avant les "
        "résultats ; chaque candidat résolu un par un avec identifiants comparés ; captures conservées dans work/."),
    "verificador-autenticidade": (
        "Vérifie l'authenticité de pièces sur UN portail officiel (codes de vérification, selos, CND, CCIR, OAB…). À utiliser "
        "une fois par portail, en parallèle.",
        ["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch", "Write"],
        "Tu vérifies, sur le portail officiel confié uniquement, les codes ou identifiants listés dans ta mission ; tu conserves "
        "la preuve (capture/PDF dans work/) ; captcha, connexion ou blocage : tu ne contournes pas, tu proposes une tâche humaine "
        "avec le lien, les codes et le résultat attendu."),
    "analista-kyc": (
        "Analyste KYC pour une tâche indépendante qui n'entre dans aucune autre catégorie.",
        ["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch", "Write"],
        "Tu traites uniquement la tâche confiée."),
}


def modo_execucao(ped: dict) -> str:
    return ped.get("modo") if ped.get("modo") in MODOS else MODO_AGENTE


def definir_subagentes(sistema: str, rel: str, AgentDefinition) -> dict:
    regra = ("\n\n## TON RÔLE : SOUS-AGENT ({nome})\n{papel}\n"
             "Tu travailles uniquement sur la mission confiée. Tu n'écris **que** ton fichier de résultat (chemin donné dans la "
             "mission, sous " + rel + "/work/lots/) et, si besoin, tes preuves sous " + rel + "/work/ ; jamais state.json, "
             "les versions ni les livrables. Format du fichier : celui indiqué dans la mission (constatacoes, controles, "
             "pendencias_sugeridas, tarefas_humanas_sugeridas, nao_tratado, duvidas). Le principe de prudence s'applique : "
             "signale tout ce qui pourrait compter, même incertain. Termine par un résumé de 5 lignes.")
    return {nome: AgentDefinition(description=desc, tools=ferr, model="inherit",
                                  prompt=sistema + regra.format(nome=nome, papel=papel))
            for nome, (desc, ferr, papel) in SUBAGENTES.items()}


def restringir_subagentes(permissao, modo: str, reg: "Registro"):
    """Agent libre : aucune restriction (comportement d'origine). Multi-agents : seulement les sous-agents spécialisés
    (qui ont le guide) — pas le sous-agent générique du moteur, qui n'a ni le guide ni la méthode."""
    from claude_agent_sdk import PermissionResultDeny

    async def p(tool, inp, ctx):
        if tool in OUTILS_SUBAGENTE and modo == MODO_MULTI:
            tipo = inp.get("subagent_type")
            if tipo not in SUBAGENTES:
                motivo = f"Type de sous-agent non autorisé ({tipo}) : utilise {', '.join(SUBAGENTES)}."
            else:
                return await permissao(tool, inp, ctx)
            reg.log(f"Sous-agent refusé : {motivo}")
            reg.journal.ev("recusa", ferramenta=tool, parametros=inp, motivo=motivo)
            return PermissionResultDeny(message=motivo)
        return await permissao(tool, inp, ctx)
    return p


class Interrompido(Exception):
    pass


class Parado(Exception):
    """Arrêt demandé par l'utilisateur (bouton « Arrêter les agents »)."""


RMTREE_TENTATIVAS, RMTREE_PAUSA_S = 20, 0.5  # ~10 s : un fichier encore ouvert par un processus qui se termine (Windows)


def _rmtree(p: Path, tolerante: bool = False) -> list[str]:
    """Supprime une arborescence. Fichiers en lecture seule : forcés. Fichiers verrouillés (WinError 32, processus
    qui se termine) : nouvelles tentatives pendant ~10 s. Ensuite : exception, ou — si tolerante — la liste des
    fichiers restés en place (le reste est supprimé)."""
    restos = []

    def _forcar(func, caminho, _exc):
        for i in range(RMTREE_TENTATIVAS):
            try:
                try:
                    os.chmod(caminho, stat.S_IWRITE)
                except OSError:
                    pass
                func(caminho)
                return
            except FileNotFoundError:
                return
            except OSError as e:
                # dossier non vide parce qu'un fichier verrouillé y est resté : pas la peine d'insister
                if i == RMTREE_TENTATIVAS - 1 or not isinstance(e, PermissionError):
                    if tolerante:
                        restos.append(str(caminho))
                        return
                    raise
                time.sleep(RMTREE_PAUSA_S)
    if p.exists():
        shutil.rmtree(p, onexc=_forcar) if sys.version_info >= (3, 12) else shutil.rmtree(p, onerror=lambda f, c, e: _forcar(f, c, e))
    return restos


def _inventario(case: Path, raiz: str) -> dict:
    base = case / raiz
    if base.is_file():
        st = base.stat()
        return {raiz: [st.st_size, st.st_mtime_ns]}
    return {p.relative_to(case).as_posix(): [p.stat().st_size, p.stat().st_mtime_ns]
            for p in base.rglob("*") if p.is_file()} if base.is_dir() else {}


def preparar_reversao(case: Path, rd: Path, vn: int, ped: dict):
    """Copie de ce que l'agent peut modifier (state.json, work/) + inventaire des parties protégées."""
    b = rd / "reversao"
    _rmtree(b)
    b.mkdir(parents=True)
    if (case / "state.json").exists():
        shutil.copy2(case / "state.json", b / "state.json")
    if (case / "work").is_dir():
        shutil.copytree(case / "work", b / "work")
    autres = {p.name for p in case.iterdir()} - {"state.json", "work", "versions", *PROTEGIDOS}
    for nome in autres:  # tout autre élément à la racine du dossier (rare) est aussi sauvegardé
        src = case / nome
        shutil.copytree(src, b / "outros" / nome) if src.is_dir() else (b / "outros").mkdir(exist_ok=True) or shutil.copy2(src, b / "outros" / nome)
    for d in ped.get("pendentes", {}).get("respostas", []):  # réponses traitées : restaurées à l'octet près en cas d'échec
        r = case / "respostas" / d / "resposta.json"
        if r.exists():
            (b / "respostas" / d).mkdir(parents=True, exist_ok=True)
            shutil.copy2(r, b / "respostas" / d / "resposta.json")
    protegidos = {}
    for raiz in PROTEGIDOS:
        if raiz != "runs":
            protegidos.update(_inventario(case, raiz))
    (b / "manifesto.json").write_text(json.dumps({
        "criado_em": agora(), "versoes": versoes(case), "versao_alvo": vn + 1, "tinha_state": (b / "state.json").exists(),
        "tinha_work": (b / "work").is_dir(), "outros": sorted(autres), "protegidos": protegidos,
        "pecas": {str(p): [p.stat().st_size, p.stat().st_mtime_ns] for p in base_pecas(case).rglob("*") if p.is_file()}
                 if base_pecas(case).is_dir() else {},
        "pendentes": ped.get("pendentes", {})}, ensure_ascii=False, indent=2), encoding="utf-8")


def reverter(case: Path, rd: Path, motivo: str, reg: "Registro | None" = None, estado: str = "erro") -> dict | None:
    """Remet le dossier dans l'état d'avant l'exécution. Appelée par le worker (échec) ou par l'application
    (processus disparu). Un verrou garantit qu'elle ne s'exécute qu'une fois. Retourne le compte rendu."""
    b = rd / "reversao"
    try:
        fd = os.open(rd / "reversao.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        _reversao_abandonada(rd, motivo, estado)
        return None
    if reg:
        reg.dono_reversao = True
    rel = {"motivo": motivo, "em": agora(), "acoes": [], "alteracoes_fora_do_perimetro": []}
    try:
        _restaurar(case, rd, b, rel, motivo)
    except Exception as e:  # la remise en état ne doit jamais laisser le statut bloqué sur « en cours »
        rel["incompleta"] = True
        rel["acoes"].append(f"REMISE EN ÉTAT INCOMPLÈTE ({type(e).__name__} : {e}) — vérifier le dossier avant de relancer.")
    return _fechar_reversao(rd, motivo, estado, rel, reg)


def _reversao_abandonada(rd: Path, motivo: str, estado: str):
    """Verrou présent mais statut resté « en cours » depuis longtemps : la remise en état précédente a été interrompue
    (processus mort en plein milieu). On le dit au lieu de laisser l'exécution « en cours » pour toujours."""
    try:
        st = json.loads((rd / "status.json").read_text(encoding="utf-8"))
        idade = time.time() - (rd / "reversao.lock").stat().st_mtime
    except (OSError, ValueError):
        return
    if st.get("estado") != "em_curso" or idade < INTERROMPIDO_S:
        return
    _fechar_reversao(rd, motivo, estado, {
        "motivo": motivo, "em": agora(), "incompleta": True, "alteracoes_fora_do_perimetro": [],
        "acoes": ["REMISE EN ÉTAT INCOMPLÈTE : elle a été interrompue en cours de route — vérifier le dossier avant de relancer."]}, None)


def _restaurar(case: Path, rd: Path, b: Path, rel: dict, motivo: str):
    proteger_pecas(case, rd, False)
    if not (b / "manifesto.json").exists():
        rel["acoes"].append("Aucune sauvegarde (exécution arrêtée avant de commencer) : rien à restaurer.")
    else:
        man = json.loads((b / "manifesto.json").read_text(encoding="utf-8"))
        try:
            if guardar_retomada(case, rd, man, motivo):
                rel["acoes"].append("travail de l'agent mis de côté (reprise possible)")
        except Exception as e:  # la mise de côté ne doit jamais empêcher la remise en état
            rel["acoes"].append(f"travail de l'agent non mis de côté : {e}")
        for v in versoes(case):  # version partielle ou complète produite pendant l'exécution
            if v not in man["versoes"]:
                _rmtree(case / "versions" / f"v{v}")
                rel["acoes"].append(f"versions/v{v} supprimée")
        if man["tinha_state"]:
            shutil.copy2(b / "state.json", case / "state.json")
            rel["acoes"].append("state.json restauré")
        elif (case / "state.json").exists():
            (case / "state.json").unlink()
            rel["acoes"].append("state.json supprimé (n'existait pas avant)")
        restos = _rmtree(case / "work", tolerante=True)
        if restos:
            rel["acoes"].append(f"work/ : {len(restos)} fichier(s) verrouillé(s) laissé(s) en place : " + ", ".join(restos[:5]))
        if man["tinha_work"]:
            shutil.copytree(b / "work", case / "work", dirs_exist_ok=True)
            rel["acoes"].append("work/ restauré")
        for nome in {p.name for p in case.iterdir()} - {"state.json", "work", "versions", *PROTEGIDOS} - set(man["outros"]):
            (_rmtree if (case / nome).is_dir() else Path.unlink)(case / nome)
            rel["acoes"].append(f"{nome} supprimé (créé pendant l'exécution)")
        for nome in man["outros"]:
            src = b / "outros" / nome
            if src.is_dir():
                _rmtree(case / nome)
                shutil.copytree(src, case / nome)
            elif src.exists():
                shutil.copy2(src, case / nome)
        desmarcar_integrados(case, man["pendentes"], man["versao_alvo"])
        for r in (b / "respostas").glob("*/resposta.json") if (b / "respostas").is_dir() else []:
            shutil.copy2(r, case / "respostas" / r.parent.name / "resposta.json")
        rel["acoes"].append("marquages « traité » de cette exécution annulés")
        rel["alteracoes_fora_do_perimetro"] += pecas_alteradas(case, man)
        # pièces/réponses existantes modifiées pendant l'exécution (hors ajouts de l'application) : signalées, non écrasées
        agora_inv = {}
        for raiz in PROTEGIDOS:
            if raiz not in ("runs", "documentos.json", "dossie.json", "metricas"):
                agora_inv.update(_inventario(case, raiz))
        for k, (tam, _mt) in man["protegidos"].items():
            if k.startswith(("documentos/", "respostas/")) and not k.endswith("resposta.json"):
                atual = agora_inv.get(k)
                if atual is None or atual[0] != tam:
                    rel["alteracoes_fora_do_perimetro"].append(k)
        _rmtree(b, tolerante=True)


def _fechar_reversao(rd: Path, motivo: str, estado: str, rel: dict, reg: "Registro | None") -> dict:
    if rel.get("incompleta"):
        motivo = f"REMISE EN ÉTAT INCOMPLÈTE (vérifier le dossier avant de relancer) — {motivo}"
    (rd / "reversao.json").write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
    # statut final (écrit même si le worker a disparu)
    sp = rd / "status.json"
    try:
        st = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    fim = fim_trabalho(rd, st)
    st.update(estado=estado, fim=fim, atualizado_em=agora(), erro=motivo[:500], revertido=True, reversao=rel)
    st["duracao_s"] = duracao_total(st)
    st["duracao_execucao_s"] = duracao_execucao(st)
    if reg:
        reg.st.update(st)
    tmp = rd / "status.json.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(sp)
    with (rd / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": agora(), "msg": "Remise en état INCOMPLÈTE : vérifier le dossier avant de relancer."
                             if rel.get("incompleta") else "Dossier remis dans l'état d'avant l'exécution."}, ensure_ascii=False) + "\n")
    pp = rd / "pedido.json"
    try:
        ped = json.loads(pp.read_text(encoding="utf-8"))
        ped["estado"] = estado
        pp.write_text(json.dumps(ped, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return rel


RETOMADA = "retomada"


def guardar_retomada(case: Path, rd: Path, man: dict, motivo: str) -> bool:
    """Met de côté le travail de l'agent (work/, state.json, version partielle) et l'identifiant de sa conversation,
    pour « Reprendre la mise à jour ». Rien n'est mis de côté si l'agent n'a encore rien produit ni de conversation."""
    try:
        st = json.loads((rd / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    alvo = man["versao_alvo"]
    r = rd / RETOMADA
    _rmtree(r)
    r.mkdir(parents=True)
    copiado = []
    if (case / "work").is_dir():
        shutil.copytree(case / "work", r / "work")
        copiado.append("work")
    if (case / "state.json").exists():
        shutil.copy2(case / "state.json", r / "state.json")
        copiado.append("state.json")
    if alvo not in man["versoes"] and (case / "versions" / f"v{alvo}").is_dir():
        shutil.copytree(case / "versions" / f"v{alvo}", r / "versao")
        copiado.append(f"versions/v{alvo}")
    if not copiado and not st.get("sessao"):
        _rmtree(r)
        return False
    (r / "retomada.json").write_text(json.dumps({
        "em": agora(), "motivo": motivo[:500], "sessao": st.get("sessao"), "versao_origem": alvo - 1, "versao_alvo": alvo,
        "copiado": copiado, "duracao_s": duracao_total({**st, "fim": fim_trabalho(rd, st)}), "custo_usd": st.get("custo_usd"),
        "uso": st.get("uso")}, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def restaurar_retomada(case: Path, origem: Path, vnext: int) -> list[str]:
    """Remet dans le dossier le travail mis de côté par une exécution interrompue (même version cible)."""
    r = origem / RETOMADA
    info = json.loads((r / "retomada.json").read_text(encoding="utf-8"))
    if info.get("versao_alvo") != vnext:
        return []
    feito = []
    if (r / "work").is_dir():
        _rmtree(case / "work")
        shutil.copytree(r / "work", case / "work")
        feito.append("work/")
    if (r / "state.json").exists():
        shutil.copy2(r / "state.json", case / "state.json")
        feito.append("state.json")
    if (r / "versao").is_dir():
        _rmtree(case / "versions" / f"v{vnext}")
        shutil.copytree(r / "versao", case / "versions" / f"v{vnext}")
        feito.append(f"versions/v{vnext}/")
    return feito


def sessao_disponivel(sid: str) -> bool:
    """La conversation de l'agent est-elle encore enregistrée sur ce poste (nécessaire pour la reprendre) ?"""
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return bool(sid) and any((cfg / "projects").glob(f"*/{sid}.jsonl"))


def verificar_interrupcao(case: Path, rd: Path) -> bool:
    """Appelée par l'application : une exécution « em_curso » sans signe de vie depuis INTERROMPIDO_S
    (ordinateur éteint, veille, processus tué) est déclarée interrompue et le dossier remis en état."""
    try:
        st = json.loads((rd / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if st.get("estado") != "em_curso":
        return False
    ultimo = max((rd / x).stat().st_mtime for x in ("status.json", "log.jsonl") if (rd / x).exists())
    if time.time() - ultimo < INTERROMPIDO_S:
        return False
    reverter(case, rd, "Exécution interrompue sans fin normale (ordinateur éteint ou en veille, perte de connexion "
                       "prolongée ou arrêt forcé du processus).")
    return True


def desmarcar_integrados(case: Path, pend: dict, v: int):
    for d in pend.get("respostas", []):
        p = case / "respostas" / d / "resposta.json"
        if p.exists():
            r = json.loads(p.read_text(encoding="utf-8"))
            if r.get("integrada_em_versao") == v:
                r.pop("integrada_em_versao", None)
                r["status"] = "recebida"
                p.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    inv_p = case / "documentos.json"
    if inv_p.exists() and pend.get("pecas"):
        inv = json.loads(inv_p.read_text(encoding="utf-8"))
        alvo = set(pend["pecas"])
        for fx in inv["arquivos"]:
            if fx["caminho"] in alvo and fx.get("integrado_em_versao") == v:
                fx["integrado_em_versao"] = None
        inv_p.write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")


def marcar_integrados(case: Path, ped: dict, v: int):
    for d in ped["pendentes"].get("respostas", []):
        p = case / "respostas" / d / "resposta.json"
        if p.exists():
            r = json.loads(p.read_text(encoding="utf-8"))
            r.update(integrada_em_versao=v, status="integrada")
            p.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    inv_p = case / "documentos.json"
    if inv_p.exists() and ped["pendentes"].get("pecas"):
        inv = json.loads(inv_p.read_text(encoding="utf-8"))
        alvo = set(ped["pendentes"]["pecas"])
        for f in inv["arquivos"]:
            if f["caminho"] in alvo:
                f["integrado_em_versao"] = v
        inv_p.write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")


def chave_api() -> str:
    """Clé API de l'application. On évite ANTHROPIC_API_KEY au niveau de Windows : Claude Code (l'outil de développement)
    la lit aussi et la ferait passer avant la connexion claude.ai de l'utilisateur. KYC_ANTHROPIC_API_KEY n'est lue que
    par l'application, qui la passe à l'agent sous le nom attendu par le SDK."""
    from kyc_agent.cle import cle_registre  # clé enregistrée sur la machine (fenêtre « clé API » de l'application)
    return (os.environ.get("KYC_ANTHROPIC_API_KEY") or cle_registre() or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def modo_simulacao() -> bool:
    return os.environ.get("KYC_AGENT_SIMULATE") == "1" or not chave_api()


def limpar_requete(case: Path, ped: dict):
    """Requête de mise à jour traitée : elle disparaît du tableau de bord (elle reste dans pedido.json de l'exécution)."""
    rq, p = ped.get("requete"), case / "requete.json"
    try:
        if rq and p.exists() and json.loads(p.read_text(encoding="utf-8")).get("em") == rq.get("em"):
            p.unlink()
    except (OSError, ValueError):
        pass


def main(case_dir: str, run_dir: str):
    case, rd = Path(case_dir).resolve(), Path(run_dir).resolve()
    ped = json.loads((rd / "pedido.json").read_text(encoding="utf-8"))
    vs = versoes(case)
    vn = vs[-1] if vs else 0
    sim = modo_simulacao()
    ret = ped.get("retomada") or {}
    reg = Registro(rd, {"versao_alvo": vn + 1, "simulacao": sim, "sessao": str(uuid.uuid4()),
                        "duracao_anterior_s": int(ret.get("duracao_anterior_s") or 0), "retomada_de": ret.get("de")})
    if parada_pedida(rd):  # arrêt demandé avant que le worker ait commencé : rien n'a été touché
        reg.log("Mise à jour arrêtée par l'utilisateur avant le démarrage.")
        reg.fim("parado", erro="Mise à jour arrêtée par l'utilisateur avant le démarrage.")
        ped["estado"] = "parado"
        (rd / "pedido.json").write_text(json.dumps(ped, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    ped["estado"] = "em_execucao"
    (rd / "pedido.json").write_text(json.dumps(ped, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if sim:
            reg.log("Aucune clé API (KYC_ANTHROPIC_API_KEY) : exécution en mode SIMULATION.")
            asyncio.run(simular(reg, ped, vn + 1))
            ped["estado"] = "simulado"
            limpar_requete(case, ped)
        else:
            reg.log(f"Démarrage de l'agent ({MODEL}) — "
                    + (f"mise à jour v{vn} → v{vn + 1}" if vn else "première analyse → v1"))
            reg.log("Sauvegarde du dossier avant exécution (remise en état possible en cas d'échec)")
            preparar_reversao(case, rd, vn, ped)
            n = proteger_pecas(case, rd, True)
            reg.log(f"{n} pièce(s) du dossier passées en lecture seule pendant l'exécution")
            origem = rd.parent / ret["de"] if ret.get("de") else None
            if origem and (origem / RETOMADA / "retomada.json").exists():
                feito = restaurar_retomada(case, origem, vn + 1)
                reg.log("Travail de l'exécution interrompue restauré : " + (", ".join(feito) or "rien à restaurer"))
            asyncio.run(executar(case, rd, reg, ped, vn, vn + 1))
            proteger_pecas(case, rd, False)
            _rmtree(rd / "reversao")  # succès : la sauvegarde n'est plus utile
            if ret.get("de"):
                _rmtree(rd.parent / ret["de"] / RETOMADA)  # le travail repris est désormais dans la version produite
            ped["estado"] = "concluido"
            limpar_requete(case, ped)
    except BaseException as e:  # erreur, Ctrl+C, arrêt : le dossier est remis dans l'état d'avant
        (rd / "erro.txt").write_text(traceback.format_exc(), encoding="utf-8")
        if reg.revertido_por_terceiro():
            return  # l'application a déjà remis le dossier en état
        parado = isinstance(e, Parado)
        estado = "parado" if parado else "erro"
        reg.log(str(e) if parado else f"Erreur : {e}")
        reg.journal.ev("fim", estado=estado, erro=str(e)[:2000], rastreamento=traceback.format_exc()[-8000:],
                       remise_en_etat=not sim)
        if sim:
            reg.fim(estado, erro=str(e)[:500])
        else:
            try:
                reverter(case, rd, str(e) if parado else f"Échec de l'exécution : {e}", reg, estado)
            except Exception as e2:  # dernier filet : jamais de statut bloqué sur « en cours »
                _anexar(rd / "erro.txt", "\n--- remise en état ---\n" + traceback.format_exc())
                reg.fim(estado, erro=f"REMISE EN ÉTAT INCOMPLÈTE ({e2}) — {e}"[:500])
        ped["estado"] = estado
        if not isinstance(e, Exception):
            raise
    (rd / "pedido.json").write_text(json.dumps(ped, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
