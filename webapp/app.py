"""Prototype d'interface web pour les dossiers KYC produits par kyc_agent.

Lit directement les livrables versionnés : <DATA_DIR>/cases/<CASE>/versions/v<N>/
  state_v<N>.json, <CASE>_Relatorio_KYC_v<N>.pdf, <CASE>_Matriz_v<N>.xlsx, mensagens/*.md, <CASE>_Evidencias_v<N>.zip, Nota_FR_v<N>.md
Aucune donnée n'est recalculée : l'écran montre exactement ce que contiennent les livrables.

Lancement local : uvicorn webapp.app:app --reload        (DATA_DIR = racine du projet par défaut)
Variables : DATA_DIR, APP_USER / APP_PASSWORD (authentification Basic activée si définies).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import csv
import html
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from openpyxl import load_workbook
from kyc_agent import worker as _worker
from kyc_agent.worker import (MODO_AGENTE, MODOS, INTERROMPIDO_S, OUTILS_SUBAGENTE, RETOMADA, carregar_lista_branca,
                              duracao_total, reverter, verificar_interrupcao)
from kyc_agent.respostas import DEFAULT_ACEITA, acoes, acoes_mensagem, listar as listar_respostas
from kyc_agent import journal as jrn
from kyc_agent import sauvegarde as bkp
from kyc_agent.metricas import custo as custo_uso, custo_versao, resumo as resumo_metricas, totais as totais_uso
from webapp.i18n import LANGS, dico, tr_data, ui
from webapp.graficos import barras_custo, linhas_tokens

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE.parent)).resolve()
CASES = DATA_DIR / "cases"
USER, PWD = os.environ.get("APP_USER"), os.environ.get("APP_PASSWORD")
EM_LINHA = os.environ.get("RENDER") == "true"  # variable posée par Render sur ses serveurs

app = FastAPI(title="KYC Compliance — dossiers", docs_url=None, redoc_url=None)
tpl = Jinja2Templates(directory=str(BASE / "templates"))
tpl.env.globals["em_simulacao"] = lambda: _worker.modo_simulacao()  # simulation forcée ou aucune clé API : bandeau


def cle_local(request: Request) -> bool:
    """La saisie de la clé API n'est proposée que sur l'ordinateur de l'utilisateur (application lancée en local
    sous Windows), jamais sur un serveur en ligne."""
    return sys.platform == "win32" and (request.client.host if request.client else "") in ("127.0.0.1", "::1", "localhost")


tpl.env.globals["pedir_cle"] = lambda request: cle_local(request) and not _worker.chave_api() \
    and os.environ.get("KYC_AGENT_SIMULATE") != "1"
TABS = [("painel", "Painel"), ("rapport", "Relatório"), ("matriz", "Matriz"), ("verificacoes", "Verificações"), ("mensagens", "Mensagens e anexos"), ("provas", "ZIP de provas")]


# ---------------------------------------------------------------- sécurité minimale
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if EM_LINHA and not (USER and PWD) and request.url.path != "/healthz":
        # en ligne (Render), jamais d'accès sans identifiant : les dossiers contiennent des données personnelles
        return Response("Aplicação bloqueada: defina APP_USER e APP_PASSWORD no painel Render.", 503)
    if USER and PWD and request.url.path != "/healthz":
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, USER) and secrets.compare_digest(p, PWD)
            except Exception:
                ok = False
        if not ok:
            return Response("Autenticação necessária", 401, {"WWW-Authenticate": 'Basic realm="KYC"'})
    resp = await call_next(request)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True}  # sans authentification : ne rien révéler des dossiers


# ---------------------------------------------------------------- accès aux données
SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def list_cases():
    """Un dossier = cases/<ID>/ avec des versions (déjà analysé) ou un dossie.json (créé depuis l'app)."""
    if not CASES.is_dir():
        return []
    return sorted(p.name for p in CASES.iterdir()
                  if p.is_dir() and SAFE.match(p.name) and not p.name.startswith(".") and ((p / "versions").is_dir() or (p / "dossie.json").exists()))


# ---------------------------------------------------------------- suivi des dossiers
STATUS = {"preparacao": "Em preparação", "andamento": "Em instrução", "tratado": "Concluído"}


def case_dir(cid):
    if not SAFE.match(cid) or not (CASES / cid).is_dir():
        raise HTTPException(404, "Dossiê inexistente")
    return CASES / cid


def case_meta(cid) -> dict:
    p = CASES / cid / "dossie.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_meta(cid, m: dict):
    (CASES / cid / "dossie.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def case_status(cid, vs=None, meta=None) -> str:
    """Tratado = validação humana registrada ; em instrução = ao menos uma versão produzida pelo agente ;
    em preparação = documentos importados, análise ainda não produzida."""
    meta = case_meta(cid) if meta is None else meta
    if meta.get("tratado_em"):
        return "tratado"
    return "andamento" if (versions(cid) if vs is None else vs) else "preparacao"


def inventario(cid) -> dict | None:
    p = CASES / cid / "documentos.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def proximo_id() -> str:
    nums, pref = [], "TC"
    for c in list_cases():
        m = re.match(r"^([A-Za-z]+)-(\d+)$", c)
        if m:
            pref = m.group(1)
            nums.append((int(m.group(2)), len(m.group(2))))
    if not nums:
        return "TC-001"
    n, w = max(nums)
    return f"{pref}-{n + 1:0{w}d}"


def versions(cid):
    vdir = CASES / cid / "versions"
    if not vdir.is_dir():
        return []
    return sorted((int(p.name[1:]) for p in vdir.glob("v*") if p.name[1:].isdigit()), reverse=True)


def vdir(cid, v):
    if not SAFE.match(cid):
        raise HTTPException(404)
    d = CASES / cid / "versions" / f"v{v}"
    if not d.is_dir():
        raise HTTPException(404, "Versão inexistente")
    return d


@lru_cache(maxsize=32)
def _state(path: str, mtime: float):
    st = json.loads(Path(path).read_text(encoding="utf-8"))
    for it in st.get("registro_tecnico") or []:  # vérification avec boutons de réponse = action humaine attendue
        if "acao_humana" not in it:
            it["acao_humana"] = bool(it.get("respostas"))
    return st


def load_state(cid, v):
    p = vdir(cid, v) / f"state_v{v}.json"
    return _state(str(p), p.stat().st_mtime)


def deliverables(cid, v):
    d = vdir(cid, v)
    f = {"relatorio": d / f"{cid}_Relatorio_KYC_v{v}.pdf", "matriz": d / f"{cid}_Matriz_v{v}.xlsx",
         "zip": d / f"{cid}_Evidencias_v{v}.zip", "nota": d / f"Nota_FR_v{v}.md"}
    return {k: p for k, p in f.items() if p.exists()}


@lru_cache(maxsize=32)
def _xlsx(path: str, mtime: float):
    wb = load_workbook(path, read_only=True, data_only=True)
    try:  # read_only garde le fichier ouvert jusqu'à close() : sous Windows il resterait verrouillé (suppression du dossier impossible)
        out = []
        for ws in wb.worksheets:
            rows = [["" if c is None else str(c) for c in r] for r in ws.iter_rows(values_only=True)]
            if rows:
                out.append({"nome": ws.title, "cab": rows[0], "linhas": rows[1:]})
        return out
    finally:
        wb.close()


@lru_cache(maxsize=32)
def _zip_index(path: str, mtime: float):
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        idx_name = next((n for n in names if n.endswith("/INDICE.csv")), None)
        rows = []
        if idx_name:
            txt = z.read(idx_name).decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(txt), delimiter=";"))
        for r in rows:
            r["pasta"] = r.get("pasta") or r.get("categoria", "")
            r["existe"] = r.get("arquivo_no_zip") in names
            r["tamanho"] = z.getinfo(r["arquivo_no_zip"]).file_size if r["existe"] else 0
        return rows, sorted(names)


VIA_BDR = re.compile(r"\b(?:via|pel[ao]|por\s+(?:meio\s+d[ao]|intermédio\s+d[ao])|par(?:\s+l[ea])?|through)\s+(?:o\s+|a\s+)?BDR\b", re.I)


def envio_intermediado(m: dict) -> bool:
    """Message destiné au client (ou à transmettre par le BDR) : guide v2 — le client est contacté via le BDR.
    Dans ce cas l'application ne propose pas de bouton e-mail (il ouvrirait un brouillon adressé directement au client)."""
    dest = (m.get("destinatario") or "").strip().lower()
    return dest in ("cliente", "client") or bool(VIA_BDR.search(m.get("para") or ""))


def zip_index(cid, v):
    p = deliverables(cid, v).get("zip")
    if not p:
        return [], []
    return _zip_index(str(p), p.stat().st_mtime)


def messages(cid, v, state):
    d = vdir(cid, v) / "mensagens"
    files = {p.stem.split("_")[0]: p.name for p in d.glob("*.md")} if d.is_dir() else {}
    idx, _ = zip_index(cid, v)
    out = []
    for m in state.get("mensagens", []):
        anexos = []
        for a in m.get("anexos", []):
            ids = re.findall(r"\bE\d{2}\b", a)
            links = []
            for eid in ids:
                for r in idx:
                    if r["id"] == eid or r["id"].startswith(eid + "."):
                        links.append({"nome": r["arquivo_no_zip"].split("/")[-1], "href": f"zip/item?path={quote(r['arquivo_no_zip'])}"})
            if not ids and re.search(r"Relat[óo]rio|Rapport", a):
                links.append({"nome": f"{cid}_Relatorio_KYC_v{v}.pdf", "href": f"file/relatorio"})
            mref = re.findall(r"\bM\d\b", a)
            anexos.append({"texto": a, "links": links, "mensagem": mref[0] if mref else None,
                           "manual": not links and not mref})
        to_email = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", m.get("para", ""))
        mailto, via_bdr = None, envio_intermediado(m)
        if to_email and not via_bdr:  # message à transmettre par le BDR : pas de brouillon adressé directement au client
            mailto = f"mailto:{to_email[0]}?subject={quote(m['assunto'])}&body={quote(m['corpo'])}"
        out.append({**m, "anexos_r": anexos, "md": files.get(m["id"]), "mailto": mailto, "via_bdr": via_bdr})
    return out


def md_to_html(md: str) -> str:
    out, in_ul = [], False
    for line in md.splitlines():
        s = html.escape(line)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<em>\1</em>", s)
        if line.startswith("- "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{s[2:]}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        m = re.match(r"^(#{1,3}) (.*)", line)
        if m:
            n = len(m.group(1)) + 1
            out.append(f"<h{n}>{re.sub(r'^#+ ', '', s)}</h{n}>")
        elif re.match(r"^\d+\. ", line):
            out.append(f"<p class='num'>{s}</p>")
        elif line.strip():
            out.append(f"<p>{s}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


# ---------------------------------------------------------------- langue (pt par défaut ; ?lang=fr|pt mémorisé en cookie)
def get_lang(request: Request) -> str:
    q = request.query_params.get("lang")
    if q in LANGS:
        return q
    c = request.cookies.get("lang")
    return c if c in LANGS else "pt"


@app.exception_handler(HTTPException)
async def erro_traduzido(request: Request, exc: HTTPException):
    """Messages d'erreur du serveur dans la langue de l'interface (texte exact, ou préfixe avant « : »)."""
    from fastapi.exception_handlers import http_exception_handler
    if isinstance(exc.detail, str) and get_lang(request) == "fr":
        t = ui("fr")
        d = t(exc.detail)
        if d == exc.detail and ": " in d:
            pre, resto = d.split(": ", 1)
            d = t(pre) + " : " + resto if t(pre) != pre else d
        exc = HTTPException(exc.status_code, d, headers=exc.headers)
    return await http_exception_handler(request, exc)


def render(request: Request, name: str, ctx: dict):
    lang = ctx.setdefault("lang", get_lang(request))
    ctx.setdefault("t", ui(lang))
    resp = tpl.TemplateResponse(request, name, ctx)
    if request.query_params.get("lang") in LANGS:
        resp.set_cookie("lang", lang, max_age=365 * 86400, samesite="lax")
    return resp


# ---------------------------------------------------------------- tableau de bord
def formatadores(lang):
    sep = "\u202f" if lang == "fr" else "."

    def fmt_int(n):  # tokens : séparateur de milliers « , » dans les deux langues (ex. 1,234,567)
        return f"{int(n or 0):,}"

    def fmt_usd(x):
        s = f"{x or 0:,.2f}"
        s = s.replace(",", "X").replace(".", ",").replace("X", sep)
        return f"{s} $" if lang == "fr" else f"US$ {s}"

    def fmt_crono(sec):  # durée de travail de l'agent : 4 min 07 s · 1 h 02 min 05 s
        if not isinstance(sec, (int, float)):
            return "—"
        sec = int(sec)
        h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
        return f"{h} h {m:02d} min {s:02d} s" if h else (f"{m} min {s:02d} s" if m else f"{s} s")

    def fmt_dur(sec):
        sec = int(sec or 0)
        d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
        if d:
            return f"{d} {'j' if lang == 'fr' else 'd'} {h} h"
        return f"{h} h {m:02d} min" if h else f"{m} min"

    def fmt_data(iso):
        if not iso or not isinstance(iso, str):
            return "—"
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return iso
        return dt.strftime("%d/%m/%Y %H:%M")

    return {"fmt_int": fmt_int, "fmt_usd": fmt_usd, "fmt_dur": fmt_dur, "fmt_data": fmt_data, "fmt_crono": fmt_crono}


def ler_runs(cid):
    """Journaux d'exécution de l'agent (cases/<ID>/runs/<run>/log.jsonl) — écrits par le futur worker Agent SDK."""
    out = []
    d = CASES / cid / "runs"
    if d.is_dir():
        for p in sorted(d.glob("*/log.jsonl")):
            linhas = []
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    linhas.append(json.loads(line))
                except ValueError:
                    continue
            out.append({"id": p.parent.name, "linhas": linhas})
    return out


def contagem_acoes(cid, v, st):
    """(vérifications que l'agent n'a pas pu faire, sans réponse ; messages dont la réponse n'est pas encore enregistrée)."""
    resp = {r["item"] for r in listar_respostas(CASES / cid) if r.get("versao") == v}
    return (sum(1 for it in st.get("registro_tecnico", []) if it.get("acao_humana") and it["id"] not in resp),
            sum(1 for m in st.get("mensagens", []) if m["id"] not in resp))


RUN_STALE_S = INTERROMPIDO_S  # sans signe de vie (battement toutes les 15 s) depuis 5 min : exécution interrompue → remise en état


def run_atual(cid) -> dict | None:
    """Exécution de l'agent en cours (écrite par le worker de mise à jour) :
    cases/<ID>/runs/<run>/status.json  {"estado": "em_curso"|"concluido"|"erro", "inicio": ISO, "atualizado_em": ISO,
                                         "tarefa": "...", "modelo": "claude-opus-5-5", "uso": {in,out,cw5,cw1h,cr}}
    cases/<ID>/runs/<run>/log.jsonl    {"ts": ISO, "msg": "..."} par ligne."""
    d = CASES / cid / "runs"
    if not d.is_dir():
        return None
    for rd in sorted((x for x in d.iterdir() if x.is_dir()), reverse=True):
        sp = rd / "status.json"
        if not sp.exists():
            continue
        try:
            stt = json.loads(sp.read_text(encoding="utf-8"))
        except ValueError:
            return None
        if stt.get("estado") != "em_curso":
            return None
        vivo = max(sp.stat().st_mtime, (rd / "log.jsonl").stat().st_mtime if (rd / "log.jsonl").exists() else 0)
        if datetime.now().timestamp() - vivo > RUN_STALE_S:
            verificar_interrupcao(CASES / cid, rd)  # remet le dossier dans l'état d'avant l'exécution
            return None
        tarefa = stt.get("tarefa")
        lp = rd / "log.jsonl"
        if lp.exists():
            for line in reversed(lp.read_text(encoding="utf-8").splitlines()):
                try:
                    tarefa = json.loads(line).get("msg") or tarefa
                    break
                except ValueError:
                    continue
        return {"id": rd.name, "tarefa": tarefa or "", "uso": stt.get("uso") or {}, "modelo": stt.get("modelo", "claude-opus-5-5"),
                "decorrido_s": duracao_total({**stt, "fim": None}),
                "parada_pedida": (rd / "parar.json").exists(), "plano": stt.get("plano") or {},
                "agentes": stt.get("agentes") or {"ativos": 1, "subagentes": []}}
    return None


def agentes_ativos(cid) -> int:
    """Nombre d'agents au travail sur le dossier (0 = aucune exécution en cours) — pastille « agent au travail »."""
    run = run_atual(cid)
    return int((run["agentes"] or {}).get("ativos") or 1) if run else 0


@app.get("/agentes.json")
def agentes_json():
    """Dossiers avec une exécution en cours → nombre d'agents (pastilles du tableau de pilotage et de la barre supérieure)."""
    return {cid: n for cid in list_cases() if (n := agentes_ativos(cid))}


LOG_MAX = 60  # caractères de la ligne de log affichée sous le cerveau (le reste est dans le journal)


def log_curta(s) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= LOG_MAX else s[:LOG_MAX - 1].rstrip() + "…"


def texto_agentes(ag: dict, lang) -> str:
    """« 3 agents au travail (1 principal + 2 sous-agents) » — compté par le worker à partir des événements du SDK."""
    t = ui(lang)
    n = ag.get("ativos") or 1
    if n <= 1:
        return t("1 agente trabalhando")
    return f"{n} {t('agentes trabalhando')} (1 {t('principal')} + {n - 1} {t('subagente') if n == 2 else t('subagentes')})"


def estado_agente(cid, lang) -> dict:
    """Matrice « cerveau » : exécution en cours + cumul du projet (versions mesurées + exécution en cours)."""
    f = formatadores(lang)
    met = resumo_metricas(CASES / cid, versions(cid))
    run = run_atual(cid)
    uso = dict(met["uso"])
    custo_proj = met["custo"]
    serie = []
    for a in met["tentativas"]:  # une entrée par tentative, y compris celles qui n'ont pas produit de version
        serie.append({"versao": a["versao"], "falha": a["falha"], "rotulo": f"v{a['versao']}" + (" ✕" if a["falha"] else ""),
                      "inicio": a["inicio"], "duracao_s": a["duracao_s"], **totais_uso(a["uso"]), "custo": a["custo"]})
    out = {"ativo": bool(run), "serie": serie}
    if run:
        tr = totais_uso(run["uso"])
        cr = custo_uso(run["uso"], run["modelo"]) or 0
        for k, x in run["uso"].items():
            uso[k] = (uso.get(k) or 0) + (x or 0)
        custo_proj += cr
        pl, t = run["plano"], ui(lang)
        out.update(progresso=f"{pl['pct']} %" if pl.get("total") else "",
                   progresso_det=(f"{pl['feitas']} / {pl['total']} {t('tarefas do plano do agente')}"
                                  + (f" · {t('em curso')} : {pl['em_curso']}" if pl.get("em_curso") else "")) if pl.get("total") else "")
        out.update(parada_pedida=run["parada_pedida"], agentes=texto_agentes(run["agentes"], lang), tarefa=log_curta(run["tarefa"]), decorrido_s=run["decorrido_s"], run_in=f["fmt_int"](tr["entrada"]), run_out=f["fmt_int"](tr["saida"]), run_usd=f["fmt_usd"](cr))
    tp = totais_uso(uso)
    medido = bool(met["medidas"]) or bool(met["falhas"]) or bool(run) or not versions(cid)  # sans version : rien n'a encore été consommé (0 exact)
    out.update(medido=medido, nao_medidas=met["nao_medidas"],
               p_in=f["fmt_int"](tp["entrada"]) if medido else "—", p_out=f["fmt_int"](tp["saida"]) if medido else "—",
               p_tot=f["fmt_int"](tp["total"]) if medido else "—", p_usd=f["fmt_usd"](custo_proj) if medido else "—")
    return out


def graficos(agente, lang):
    t, f = ui(lang), formatadores(lang)
    falha = t("tentativa sem versão")
    rot = {"titulo": t("Tokens por atualização"), "entrada": t("Entrada"), "saida": t("Saída"), "total": t("Total"), "falha": falha}
    s = agente["serie"]
    return {"serie_versoes": s, "graf_tokens": linhas_tokens(s, rot, f["fmt_int"], w=900, h=300),
            "graf_custo": barras_custo(s, {"titulo": t("Fatura por atualização"), "falha": falha}, f["fmt_usd"])}


@app.get("/c/{cid}/agente.json")
def agente_json(request: Request, cid: str):
    case_dir(cid)
    r = estado_agente(cid, get_lang(request))
    r.pop("serie", None)
    return r


def pendentes_agente(cid) -> dict:
    """Ce que l'agent n'a pas encore traité : pièces du dossier non intégrées + réponses non intégrées
    (une réponse compte pour ses fichiers, ou 1 si elle n'a que du texte / une confirmation)."""
    inv = inventario(cid) or {"arquivos": []}
    docs = [f for f in inv["arquivos"] if not f.get("integrado_em_versao")]
    resp = [r for r in listar_respostas(CASES / cid) if not r.get("integrada_em_versao")]
    peso = lambda r: max(1, len(r.get("arquivos") or []))
    n_ver = sum(peso(r) for r in resp if not r["item"].startswith("M"))
    n_msg = sum(peso(r) for r in resp if r["item"].startswith("M"))
    return {"docs": docs, "n_docs": len(docs), "n_ver": n_ver, "n_msg": n_msg, "respostas": resp,
            "total": len(docs) + n_ver + n_msg}


def n_acoes(cid, v, st):
    """Actions à prendre = vérifications en attente + réponses aux messages en attente (version courante)."""
    return sum(contagem_acoes(cid, v, st))


def historico_versoes(cid, vs, lang):
    """Versions du dossier pour la fenêtre « historique » de l'en-tête (date, PDF, résumé de l'agent)."""
    out = []
    for x in vs:
        sx = tr_data(load_state(cid, x), dico(vdir(cid, x), lang))
        h = (sx.get("historico") or [{}])[-1]
        dlx = deliverables(cid, x)
        nota = ""
        if "nota" in dlx:
            linhas = dlx["nota"].read_text(encoding="utf-8").splitlines()
            nota = md_to_html("\n".join(linhas[1:] if linhas and linhas[0].startswith("# ") else linhas))
        data = h.get("data", sx.get("data_analise", ""))
        data_fmt = "/".join(reversed(data.split("-"))) if re.match(r"^\d{4}-\d{2}-\d{2}$", data or "") else data
        mp = vdir(cid, x) / "metricas.json"
        try:
            mx = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
        except ValueError:
            mx = {}
        agentes = mx.get("agentes")
        out.append({"versao": x, "data_fmt": data_fmt, "evento": h.get("evento", ""), "mudancas": h.get("mudancas", []),
                    "pdf": "relatorio" in dlx, "nota_html": nota, "agentes": agentes, "duracao_s": mx.get("duracao_s"),
                    "journal": (vdir(cid, x) / jrn.NOME_VERSAO).exists()})
    return out


def hdr_ctx(cid, lang) -> dict:
    """Bandeau bleu : nom du dossier + pastille de statut + version (avec fenêtres associées)."""
    vs, meta = versions(cid), case_meta(cid)
    st = tr_data(load_state(cid, vs[0]), dico(vdir(cid, vs[0]), lang)) if vs else {}
    return {"STATUS": STATUS, "hdr": {
        "id": cid, "nome": meta.get("nome") or st.get("caso", {}).get("nome", ""), "status": case_status(cid, vs, meta),
        "versao": vs[0] if vs else None, "hist": historico_versoes(cid, vs, lang), "meta": meta,
        "recomendacao": st.get("recomendacao", {}).get("status", ""), "agentes_ativos": agentes_ativos(cid)}}


def painel(cid, v, st, d, lang):
    vs = versions(cid)
    met = resumo_metricas(CASES / cid, vs)
    todas = listar_respostas(CASES / cid)
    pend = [r for r in todas if not r.get("integrada_em_versao")]
    if d:
        pend = [{**r, "rotulo": d.get(r.get("rotulo"), r.get("rotulo"))} for r in pend]
    respondidos = {r["item"] for r in todas if r.get("versao") == v}
    tarefas_abertas = [it for it in st.get("registro_tecnico", []) if it.get("acao_humana") and it["id"] not in respondidos]
    msgs_sem = [m for m in st.get("mensagens", []) if m["id"] not in respondidos]
    hist = []
    for x in vs:
        sx = tr_data(load_state(cid, x), dico(vdir(cid, x), lang))
        h = (sx.get("historico") or [{}])[-1]
        mv = next((m for m in met["por_versao"] if m["versao"] == x), {})
        mm = {"medido": bool(mv.get("medido")), "agentes": mv.get("agentes")}
        if mm["medido"]:
            mm.update(total=totais_uso(mv["uso"])["total"], custo=custo_versao(mv))
        dlx = deliverables(cid, x)
        nota = ""
        if "nota" in dlx:  # note de l'agent pour la version (sans le titre principal)
            linhas = dlx["nota"].read_text(encoding="utf-8").splitlines()
            nota = md_to_html("\n".join(linhas[1:] if linhas and linhas[0].startswith("# ") else linhas))
        data = h.get("data", sx.get("data_analise", ""))
        data_fmt = "/".join(reversed(data.split("-"))) if re.match(r"^\d{4}-\d{2}-\d{2}$", data or "") else data
        hist.append({"versao": x, "data": data, "data_fmt": data_fmt, "evento": h.get("evento", ""),
                     "status": sx["recomendacao"]["status"], "mudancas": h.get("mudancas", []), "metricas": mm,
                     "pdf": "relatorio" in dlx, "nota_html": nota})
    desde = None
    if met["dossie"].get("criado_em"):
        desde = (datetime.now(timezone.utc) - datetime.fromisoformat(met["dossie"]["criado_em"].replace("Z", "+00:00"))).total_seconds()
    return {"met": met, "pendentes": pend, "tarefas_abertas": tarefas_abertas, "msgs_sem_resposta": msgs_sem,
            "acoes_total": len(tarefas_abertas) + len(msgs_sem), "historico": hist, "runs": ler_runs(cid),
            "desde_criacao": desde}


# ---------------------------------------------------------------- pages
def _mtime_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def linha_dossie(cid, lang):
    vs, meta = versions(cid), case_meta(cid)
    st = {}
    if vs:
        st = tr_data(load_state(cid, vs[0]), dico(vdir(cid, vs[0]), lang))
    inv = inventario(cid)
    status = case_status(cid, vs, meta)
    marcas = [p.stat().st_mtime for p in (CASES / cid).glob("versions/v*/state_v*.json")]
    marcas += [p.stat().st_mtime for p in (CASES / cid).glob("respostas/*/resposta.json")]
    p = CASES / cid / "dossie.json"
    if p.exists():
        marcas.append(p.stat().st_mtime)
    met = resumo_metricas(CASES / cid, vs)
    fatura = {"custo": met["custo"], "medidas": met["medidas"] or met["falhas"], "nao_medidas": met["nao_medidas"]}
    idade = None
    if meta.get("criado_em"):
        idade = (datetime.now(timezone.utc) - datetime.fromisoformat(meta["criado_em"].replace("Z", "+00:00"))).total_seconds()
    return {"id": cid, "nome": meta.get("nome") or st.get("caso", {}).get("nome", ""), "status": status, "fatura": fatura, "idade_s": idade,
            "v": vs[0] if vs else None, "recomendacao": st.get("recomendacao", {}).get("status", ""),
            "acoes": n_acoes(cid, vs[0], st) if vs and status != "tratado" else 0,
            "n_docs": inv["n"] if inv else None, "criado_em": meta.get("criado_em") or "", "agentes_ativos": agentes_ativos(cid),
            "atualizado_em": _mtime_iso(max(marcas)) if marcas else ""}


# ---------------------------------------------------------------- sauvegarde automatique
BACKUP_INTERVALO_S = int(os.environ.get("KYC_BACKUP_INTERVALO_MIN", "30")) * 60
_backup_lock = threading.Lock()


def ciclo_backup() -> dict | None:
    if not _backup_lock.acquire(blocking=False):
        return None  # un cycle est déjà en cours
    try:
        return bkp.ciclo(CASES)
    finally:
        _backup_lock.release()


def _laco_backup():
    time.sleep(60)  # laisse le serveur démarrer
    while True:
        try:
            ciclo_backup()
        except Exception:
            pass
        time.sleep(BACKUP_INTERVALO_S)


CLE_MOTIVOS = {"recusada": "Chave recusada pela Anthropic: verifique a cópia ou crie uma nova chave.",
               "creditos": "Chave válida, mas sem créditos: adicione créditos (Billing) no console Anthropic.",
               "modelo": "O modelo do agente não está acessível com esta chave (organização / workspace).",
               "formato": "Formato inválido: uma chave API começa por sk-ant- (sem espaço)."}


@app.post("/config/cle")
def gravar_cle(request: Request, cle: str = Form("")):
    """Fenêtre « clé API » : vérifie la clé par un échange très court avec le modèle, puis l'enregistre sur cet
    ordinateur (variable utilisateur Windows). La clé n'est jamais renvoyée ni écrite dans un fichier ou un journal."""
    from kyc_agent import cle as kcle
    if not cle_local(request):
        raise HTTPException(403, "Indisponível")
    t = ui(get_lang(request))
    k = cle.strip()
    if not kcle.format_valido(k):
        return {"ok": False, "msg": t(CLE_MOTIVOS["formato"])}
    ok, motivo = kcle.tester(k, _worker.MODEL)
    if not ok:
        return {"ok": False, "msg": t(CLE_MOTIVOS[motivo])}
    kcle.enregistrer(k)
    return {"ok": True, "msg": t("Chave registrada neste computador") + f" ({kcle.masque(k)})."
            + ("" if motivo == "ok" else " " + t("Não foi possível testá-la agora (rede?): ela será usada na próxima atualização."))}


@app.on_event("startup")
def iniciar_backup():
    if os.environ.get("KYC_BACKUP_AUTO", "1") != "0":
        threading.Thread(target=_laco_backup, daemon=True, name="sauvegarde").start()


def estado_backup() -> dict:
    e = bkp.estado()
    try:
        idade = (datetime.now(timezone.utc) - datetime.fromisoformat(e["em"])).total_seconds() if e.get("em") else None
    except ValueError:
        idade = None
    return {**e, "idade_s": idade, "destino": str(bkp.backup_dir()), "intervalo_min": BACKUP_INTERVALO_S // 60,
            "ativa": os.environ.get("KYC_BACKUP_AUTO", "1") != "0", "em_curso": _backup_lock.locked(),
            "mesmo_disco": Path(bkp.backup_dir()).anchor.lower() == Path(CASES).resolve().anchor.lower()}


@app.post("/sauvegardes/agora")
def backup_agora():
    threading.Thread(target=ciclo_backup, daemon=True).start()
    return RedirectResponse("/sauvegardes?iniciado=1", status_code=303)


@app.get("/sauvegardes", response_class=HTMLResponse)
def backups_pagina(request: Request):
    lang = get_lang(request)
    raiz = bkp.backup_dir()
    cids = sorted(p.name for p in raiz.iterdir() if p.is_dir() and SAFE.match(p.name)) if raiz.is_dir() else []
    dossies = []
    for cid in cids:
        cs = bkp.copias(cid)
        pecas = raiz / cid / "pecas"
        tam = sum(c["tamanho"] for c in cs) + (sum(x.stat().st_size for x in pecas.iterdir()) if pecas.is_dir() else 0)
        dossies.append({"cid": cid, "existe": (CASES / cid).is_dir(), "copias": cs, "tamanho": tam})
    return render(request, "sauvegardes.html", {"dossies": dossies, "bk": estado_backup(), "iniciado": request.query_params.get("iniciado"),
                                                "garder": bkp.garder(), **formatadores(lang)})


@app.post("/sauvegardes/restaurar")
def backup_restaurar(cid: str = Form(...), copia: str = Form(...)):
    if not SAFE.match(cid):
        raise HTTPException(400)
    try:
        novo = bkp.restaurar(cid, copia, cases=CASES)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/?novo={quote(novo.name)}", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    lang = get_lang(request)
    items = sorted((linha_dossie(c, lang) for c in list_cases()), key=lambda x: x["criado_em"], reverse=True)
    cont = {k: sum(1 for x in items if x["status"] == k) for k in STATUS}
    return render(request, "home.html", {"cases": items, "contagem": cont, "STATUS": STATUS, "novo": request.query_params.get("novo"),
                                         "excluido": request.query_params.get("excluido"), "bk": estado_backup(),
                                         "proximo_id": proximo_id(), "lim": {"arq": MAX_DOCS, "mb_arq": MAX_DOC_FILE >> 20,
                                                                              "mb_tot": MAX_DOCS_TOTAL >> 20},
                                         **formatadores(lang)})


@app.get("/c/{cid}")
def case_latest(cid: str):
    case_dir(cid)
    vs = versions(cid)
    return RedirectResponse(f"/c/{cid}/v{vs[0]}/painel" if vs else f"/c/{cid}/painel")


@app.get("/c/{cid}/painel", response_class=HTMLResponse)
def case_painel_sem_versao(request: Request, cid: str):
    case_dir(cid)
    vs = versions(cid)
    if vs:
        return RedirectResponse(f"/c/{cid}/v{vs[0]}/painel")
    lang = get_lang(request)
    meta, inv = case_meta(cid), inventario(cid)
    status = case_status(cid, vs, meta)
    ctx = {"cid": cid, "v": None, "vs": [], "tab": "painel", "tabs": TABS, "tabs_off": True, "base": f"/c/{cid}/",
           "caso": {"id": cid, "nome": meta.get("nome", "")}, "status": status, "STATUS": STATUS, "ntar": 0, "badges": {},
           "meta": meta, "status_dossie": status, "n_docs": inv["n"] if inv else 0, "pend_agente": pendentes_agente(cid),
           "agente": estado_agente(cid, lang),
           **formatadores(lang)}
    ctx.update(painel(cid, None, {}, None, lang))
    ctx.update(graficos(ctx["agente"], lang), pedido=pedido_atual(cid), retomada=retomada_disponivel(cid), recusas=recusas_painel(cid), requete=requete_atual(cid))
    ctx.update(hdr_ctx(cid, lang))
    return render(request, "painel.html", ctx)


@app.get("/c/{cid}/documentos", response_class=HTMLResponse)
def case_docs(request: Request, cid: str):
    case_dir(cid)
    lang = get_lang(request)
    vs, meta = versions(cid), case_meta(cid)
    inv = inventario(cid) or {"arquivos": [], "n": 0, "tamanho_total": 0}
    pastas = {}
    for f in inv["arquivos"]:
        pastas.setdefault(f["caminho"].rpartition("/")[0], []).append({**f, "nome": f["caminho"].rpartition("/")[2]})
    nome = meta.get("nome") or (load_state(cid, vs[0]).get("caso", {}).get("nome", "") if vs else "")
    return render(request, "documentos.html", {"caso": {"id": cid, "nome": nome}, "cid": cid, "vs": vs, "meta": meta,
                                               "status": case_status(cid, vs, meta), "STATUS": STATUS, "inv": inv,
                                               "pastas": dict(sorted(pastas.items())), "servivel": inv.get("base") == "documentos",
                                               **formatadores(lang), **hdr_ctx(cid, lang)})


def x_base(inv, f):
    return f.get("armazenado") or inv.get("base")


@app.get("/c/{cid}/doc")
def case_doc(cid: str, path: str, download: int = 0):
    inv = inventario(cid) if SAFE.match(cid) else None
    f = next((x for x in (inv or {}).get("arquivos", []) if x["caminho"] == path), None)
    if not f or (x_base(inv, f) != "documentos"):
        raise HTTPException(404)  # liste blanche : uniquement les documents inventoriés et stockés dans le dossier
    root = (CASES / cid / "documentos").resolve()
    p = (root / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404)
    inline = not download and p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".txt")
    return FileResponse(p, filename=p.name, content_disposition_type="inline" if inline else "attachment")


# ---------------------------------------------------------------- création d'un dossier (import d'un répertoire local)
MAX_DOCS = 3000
MAX_DOC_FILE = 200 * 1024 * 1024
MAX_DOCS_TOTAL = 2 * 1024 * 1024 * 1024
IGNORADOS = {"thumbs.db", "desktop.ini", ".ds_store"}  # fichiers système, pas des documents
WIN_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')
WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)


def caminho_seguro(rel: str) -> list[str] | None:
    """Chemin relatif fourni par le navigateur → composants sûrs (pas de .., pas d'absolu, noms Windows valides)."""
    parts = [x for x in re.split(r"[\\/]+", rel or "") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts):
        return None
    out = []
    for x in parts:
        x = WIN_BAD.sub("_", x).rstrip(" .")[:150] or "_"
        out.append("_" + x if WIN_RESERVED.match(x) else x)
    return out


# ---------------------------------------------------------------- envoi par morceaux
# Le navigateur ouvre un « envoi » (zone d'attente hors de cases/), y dépose les fichiers par lots (petits fichiers)
# ou par morceaux (gros fichiers), réessaie un lot en échec et peut reprendre après une coupure. La création du
# dossier (ou l'ajout de pièces) consomme ensuite l'envoi complet : le dossier n'apparaît qu'une fois tout reçu.
LOTE_ARQUIVOS = 25                  # fichiers par lot
LOTE_BYTES = 32 * 1024 * 1024       # octets par lot
PEDACO_BYTES = 16 * 1024 * 1024     # un fichier plus gros est envoyé en morceaux de cette taille
ENVIO_TTL_S = 24 * 3600             # un envoi abandonné est effacé après 24 h
TOKEN_ENVIO = re.compile(r"^[0-9a-f]{32}$")


def envios_dir() -> Path:
    return CASES.parent / ".envios"  # hors de cases/ : jamais listé comme dossier, hors du périmètre de l'agent


def _manifesto(d: Path) -> dict:
    return json.loads((d / "manifesto.json").read_text(encoding="utf-8"))


def _gravar_manifesto(d: Path, m: dict):
    tmp = d / "manifesto.json.tmp"
    tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / "manifesto.json")


def envio_dir(token: str) -> Path:
    if not TOKEN_ENVIO.match(token or ""):
        raise HTTPException(400, "Envio inválido")
    d = envios_dir() / token
    if not (d / "manifesto.json").is_file():
        raise HTTPException(404, "Envio desconhecido ou expirado: recomece o envio")
    return d


def limpar_envios():
    raiz = envios_dir()
    for d in list(raiz.iterdir()) if raiz.is_dir() else []:
        try:
            velho = time.time() - (d / "manifesto.json").stat().st_mtime > ENVIO_TTL_S
        except OSError:
            velho = True
        if velho:
            shutil.rmtree(d, onerror=_rm_force)


def _chave_envio(m: dict, rel: str) -> tuple[str, list[str]]:
    """Chemin sûr + contrôle des doublons (Windows ne distingue pas majuscules/minuscules)."""
    parts = caminho_seguro(rel)
    if not parts:
        raise HTTPException(400, f"Caminho inválido: {rel}")
    cam = "/".join(parts)
    a = m["arquivos"].get(cam.lower())
    if a and a["original"] != rel:
        raise HTTPException(400, f"Caminho duplicado: {rel}")
    return cam, parts


@app.post("/envios")
def abrir_envio(tipo: str = ""):
    """tipo=zip : l'envoi contient un seul fichier ZIP (jusqu'à MAX_DOCS_TOTAL), décompressé à la création."""
    limpar_envios()
    token = secrets.token_hex(16)
    d = envios_dir() / token
    (d / "arquivos").mkdir(parents=True)
    _gravar_manifesto(d, {"criado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"), "arquivos": {},
                          "tipo": "zip" if tipo == "zip" else "pasta"})
    return {"envio": token, "lote_arquivos": LOTE_ARQUIVOS, "lote_bytes": LOTE_BYTES, "pedaco_bytes": PEDACO_BYTES}


# ---------------------------------------------------------------- import d'un fichier ZIP
ZIP_TAUX_MAX = 200                   # au-delà, un fichier « explose » à la décompression (bombe ZIP) : refusé
ZIP_TAUX_SEUIL = 10 * 1024 * 1024    # le contrôle du taux ne s'applique qu'aux fichiers décompressés > 10 Mo
ZIP_PARASITAS = ("__macosx/",)       # métadonnées macOS, pas des documents


def _zip_do_envio(d: Path) -> tuple[dict, Path]:
    m = _manifesto(d)
    arqs = list(m["arquivos"].values())
    if m.get("tipo") != "zip" or len(arqs) != 1 or not arqs[0]["original"].lower().endswith(".zip"):
        raise HTTPException(400, "Envio ZIP inválido: um único arquivo .zip é esperado")
    a = arqs[0]
    if a["recebido"] != a["tamanho"]:
        raise HTTPException(409, "Envio incompleto: retome o envio")
    return a, (d / "arquivos").joinpath(*a["caminho"].split("/"))


def analisar_zip(p: Path) -> dict:
    """Inspecte le ZIP sans le décompresser. Refuse tout le ZIP au moindre élément dangereux (chemin qui sort du
    dossier, lien symbolique, mot de passe, bombe de décompression, doublon, limites dépassées)."""
    try:
        z = zipfile.ZipFile(p)
    except (zipfile.BadZipFile, OSError):
        raise HTTPException(400, "Arquivo ZIP inválido ou corrompido")
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if any(i.flag_bits & 0x1 for i in infos):
            raise HTTPException(400, "ZIP protegido por senha: não suportado. Descompacte-o você mesmo e escolha a pasta.")
        membros, ignorados, vistos = [], [], set()
        for i in infos:
            nome = i.filename.replace("\\", "/")
            if nome.startswith("/") or re.match(r"^[A-Za-z]:", nome) or ".." in nome.split("/"):
                raise HTTPException(400, f"ZIP recusado: caminho perigoso ({nome})")
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise HTTPException(400, f"ZIP recusado: link simbólico ({nome})")
            parts = caminho_seguro(nome)
            if not parts:
                raise HTTPException(400, f"ZIP recusado: caminho inválido ({nome})")
            if nome.lower().startswith(ZIP_PARASITAS) or parts[-1].lower() in IGNORADOS or parts[-1].startswith("._"):
                ignorados.append(nome)
                continue
            if i.file_size > MAX_DOC_FILE:
                raise HTTPException(413, f"Arquivo grande demais no ZIP: {nome}")
            if i.file_size > ZIP_TAUX_SEUIL and i.file_size > ZIP_TAUX_MAX * max(i.compress_size, 1):
                raise HTTPException(400, f"ZIP recusado: taxa de compressão anormal ({nome})")
            chave = "/".join(parts).lower()
            if chave in vistos:
                raise HTTPException(400, f"ZIP recusado: caminho duplicado ({nome})")
            vistos.add(chave)
            membros.append({"nome": nome, "parts": parts, "tamanho": i.file_size})
    if not membros:
        raise HTTPException(400, "O ZIP não contém documentos")
    if len(membros) > MAX_DOCS:
        raise HTTPException(413, f"Arquivos demais no ZIP (máx. {MAX_DOCS})")
    if sum(x["tamanho"] for x in membros) > MAX_DOCS_TOTAL:
        raise HTTPException(413, "ZIP grande demais depois de descompactado")
    # un seul répertoire racine dans le ZIP (cas courant d'un dossier compressé) : il n'est pas un sous-dossier
    raizes = {x["parts"][0] for x in membros}
    if len(raizes) == 1 and all(len(x["parts"]) > 1 for x in membros):
        for x in membros:
            x["rel"] = "/".join(x["parts"][1:])
    else:
        for x in membros:
            x["rel"] = "/".join(x["parts"])
    pastas = {x["rel"].rsplit("/", 1)[0] for x in membros if "/" in x["rel"]}
    return {"membros": membros, "ignorados": ignorados, "n": len(membros), "subpastas": len(pastas),
            "tamanho_total": sum(x["tamanho"] for x in membros),
            "zips_internos": [x["rel"] for x in membros if x["rel"].lower().endswith(".zip")]}


@app.get("/envios/{token}/zip")
def resumo_zip(token: str):
    """Ce que contient le ZIP reçu, avant la création définitive."""
    d = envio_dir(token)
    a, p = _zip_do_envio(d)
    r = analisar_zip(p)
    return {"ok": True, "zip": a["original"].split("/")[-1], "n": r["n"], "subpastas": r["subpastas"], "ignorados": len(r["ignorados"]),
            "tamanho_total": r["tamanho_total"], "zips_internos": r["zips_internos"]}


def extrair_zip(d: Path) -> tuple[list[dict], dict]:
    """Décompresse le ZIP de l'envoi dans <envoi>/extraido (une seule fois), en vérifiant la taille réelle de chaque
    fichier (un en-tête ZIP peut mentir). Le ZIP lui-même n'est pas conservé : seule son empreinte est gardée."""
    a, p = _zip_do_envio(d)
    info_p = d / "zip.json"
    if info_p.exists():
        info = json.loads(info_p.read_text(encoding="utf-8"))
        return info.pop("_arquivos"), info
    r = analisar_zip(p)
    dest_raiz = d / "extraido"
    shutil.rmtree(dest_raiz, onerror=_rm_force) if dest_raiz.exists() else None
    h_zip = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            h_zip.update(chunk)
    arquivos = []
    with zipfile.ZipFile(p) as z:
        for x in r["membros"]:
            dest = dest_raiz.joinpath(*x["rel"].split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            h, n = hashlib.sha256(), 0
            with z.open(x["nome"]) as src, dest.open("wb") as out:
                while chunk := src.read(1 << 20):
                    n += len(chunk)
                    if n > x["tamanho"]:
                        raise HTTPException(400, f"ZIP recusado: conteúdo maior que o declarado ({x['nome']})")
                    h.update(chunk)
                    out.write(chunk)
            arquivos.append({"original": x["rel"], "caminho": "extraido/" + x["rel"], "tamanho": n, "recebido": n,
                             "sha256": h.hexdigest(), "no_zip": x["nome"]})
    info = {"arquivo": a["original"].split("/")[-1], "tamanho": a["tamanho"], "sha256": h_zip.hexdigest(),
            "membros": len(arquivos), "ignorados": r["ignorados"], "zips_internos_nao_descompactados": r["zips_internos"],
            "nota": "ZIP descompactado na importação; o arquivo ZIP não é conservado (somente sua impressão digital)."}
    info_p.write_text(json.dumps({**info, "_arquivos": arquivos}, ensure_ascii=False), encoding="utf-8")
    p.unlink()  # gain de place : le ZIP n'est plus nécessaire, son empreinte est dans zip.json
    return arquivos, info


@app.get("/envios/{token}")
def estado_envio(token: str):
    """Reprise : octets déjà reçus pour chaque fichier (chemin d'origine)."""
    m = _manifesto(envio_dir(token))
    return {"arquivos": {a["original"]: a["recebido"] for a in m["arquivos"].values()}}


@app.post("/envios/{token}/lote")
async def receber_lote(request: Request, token: str):
    """Un lot de petits fichiers (multipart). Renvoyer le même lot remplace simplement les fichiers."""
    d = envio_dir(token)
    form = await request.form(max_files=LOTE_ARQUIVOS, max_fields=5)
    try:
        caminhos = json.loads(form.get("caminhos") or "[]")
    except ValueError:
        raise HTTPException(400, "Lista de caminhos inválida")
    arquivos = form.getlist("arquivos")
    if not arquivos or len(arquivos) != len(caminhos):
        raise HTTPException(400, "Nenhum arquivo recebido")
    if sum(f.size or 0 for f in arquivos) > LOTE_BYTES:
        raise HTTPException(413, "Lote grande demais")
    m = _manifesto(d)
    for f, rel in zip(arquivos, caminhos):
        cam, parts = _chave_envio(m, rel)
        dest = (d / "arquivos").joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        h, n = hashlib.sha256(), 0
        with dest.open("wb") as out:
            while chunk := await f.read(1 << 20):
                h.update(chunk)
                n += len(chunk)
                out.write(chunk)
        m["arquivos"][cam.lower()] = {"original": rel, "caminho": cam, "tamanho": n, "recebido": n, "sha256": h.hexdigest()}
    _gravar_manifesto(d, m)
    return {"ok": True, "n": len(arquivos)}


@app.post("/envios/{token}/pedaco")
async def receber_pedaco(request: Request, token: str, caminho: str, inicio: int, total: int):
    """Un morceau d'un gros fichier (corps brut), écrit à la position `inicio`. Un morceau renvoyé est réécrit ;
    un morceau qui arrive trop loin est refusé (409) avec la position attendue, pour que le navigateur reprenne."""
    d = envio_dir(token)
    m = _manifesto(d)
    if total < 0 or total > (MAX_DOCS_TOTAL if m.get("tipo") == "zip" else MAX_DOC_FILE):
        raise HTTPException(413, f"Arquivo grande demais: {caminho}")
    cam, parts = _chave_envio(m, caminho)
    a = m["arquivos"].get(cam.lower())
    recebido = a["recebido"] if a and a["tamanho"] == total else 0
    if inicio < 0 or inicio > recebido:
        raise HTTPException(409, f"Posição esperada: {recebido}")
    dest = (d / "arquivos").joinpath(*parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with dest.open("r+b" if inicio and dest.exists() else "wb") as out:
        out.seek(inicio)
        async for chunk in request.stream():
            n += len(chunk)
            if n > PEDACO_BYTES or inicio + n > total:
                raise HTTPException(413, "Pedaço grande demais")
            out.write(chunk)
        out.truncate(inicio + n)
    m["arquivos"][cam.lower()] = {"original": caminho, "caminho": cam, "tamanho": total, "recebido": inicio + n, "sha256": None}
    _gravar_manifesto(d, m)
    return {"ok": True, "recebido": inicio + n}


@app.post("/envios/{token}/abandonar")
def abandonar_envio(token: str):
    shutil.rmtree(envio_dir(token), onerror=_rm_force)
    return {"ok": True}


class _FonteUpload:
    """Fichier reçu dans la requête elle-même (envoi en une fois)."""
    def __init__(self, f):
        self.f, self.size = f, f.size or 0

    async def salvar(self, dest: Path) -> tuple[int, str]:
        h, n = hashlib.sha256(), 0
        with dest.open("wb") as out:
            while chunk := await self.f.read(1 << 20):
                h.update(chunk)
                n += len(chunk)
                out.write(chunk)
        return n, h.hexdigest()

    def desfazer(self):
        pass


class _FonteEnvio:
    """Fichier déjà reçu dans un envoi par morceaux : simplement déplacé (pas de copie)."""
    def __init__(self, base: Path, a: dict):
        self.src, self.size, self.sha, self.dest = base.joinpath(*a["caminho"].split("/")), a["tamanho"], a["sha256"], None

    async def salvar(self, dest: Path) -> tuple[int, str]:
        if not self.sha:
            h = hashlib.sha256()
            with self.src.open("rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            self.sha = h.hexdigest()
        os.replace(self.src, dest)
        self.dest = dest
        return self.size, self.sha

    def desfazer(self):  # échec de la création : le fichier retourne dans l'envoi (pas besoin de le renvoyer)
        if self.dest and self.dest.exists():
            os.replace(self.dest, self.src)
            self.dest = None


async def fontes_do_formulario(form, raiz: str | None = None) -> tuple[list[str], list, Path | None, dict | None]:
    """(chemins d'origine, sources, répertoire de l'envoi, info ZIP) — envoi par morceaux (champ `envio`), ZIP
    décompressé, ou envoi en une fois. `raiz` : préfixe donné aux chemins d'un ZIP (création d'un dossier)."""
    token = form.get("envio")
    if not token:
        try:
            caminhos = json.loads(form.get("caminhos") or "[]")
        except ValueError:
            raise HTTPException(400, "Lista de caminhos inválida")
        arquivos = form.getlist("arquivos")
        if not arquivos or len(arquivos) != len(caminhos):
            raise HTTPException(400, "Nenhum arquivo recebido")
        return caminhos, [_FonteUpload(f) for f in arquivos], None, None
    d = envio_dir(token)
    if _manifesto(d).get("tipo") == "zip":
        arqs, info = await asyncio.to_thread(extrair_zip, d)  # décompression longue : hors de la boucle
        pre = (raiz.rstrip("/") + "/") if raiz else ""
        return [pre + a["original"] for a in arqs], [_FonteEnvio(d, a) for a in arqs], d, info
    arqs = sorted(_manifesto(d)["arquivos"].values(), key=lambda a: a["original"])
    faltam = [a["original"] for a in arqs if a["recebido"] != a["tamanho"]]
    try:
        n_esp, b_esp = int(form.get("n") or -1), int(form.get("bytes") or -1)
    except ValueError:
        n_esp = b_esp = -1
    if faltam or len(arqs) != n_esp or sum(a["tamanho"] for a in arqs) != b_esp:
        raise HTTPException(409, f"Envio incompleto ({len(arqs) - len(faltam)}/{n_esp} arquivos): retome o envio")
    if not arqs:
        raise HTTPException(400, "Nenhum arquivo recebido")
    return [a["original"] for a in arqs], [_FonteEnvio(d / "arquivos", a) for a in arqs], d, None


@app.post("/dossies")
async def criar_dossie(request: Request):
    form = await request.form(max_files=MAX_DOCS, max_fields=20)
    cid = (form.get("id") or "").strip()
    nome = (form.get("nome") or "").strip()[:200]
    if not SAFE.match(cid) or len(cid) > 40:
        raise HTTPException(400, "Identificador inválido (letras, números, . _ -)")
    if (CASES / cid).exists():
        raise HTTPException(409, f"O dossiê {cid} já existe")
    caminhos, arquivos, d_envio, info_zip = await fontes_do_formulario(form, raiz=cid)
    origem = (caminho_seguro(caminhos[0]) or ["pasta"])[0]
    if info_zip:
        origem = info_zip["arquivo"]
    total = sum(f.size or 0 for f in arquivos)
    if total > MAX_DOCS_TOTAL:
        raise HTTPException(413, "Pasta grande demais")
    CASES.mkdir(parents=True, exist_ok=True)
    tmp = CASES / f".tmp_{cid}_{secrets.token_hex(3)}"
    docs = tmp / "documentos"
    lista, ignorados = [], []
    try:
        for f, rel in zip(arquivos, caminhos):
            parts = caminho_seguro(rel)
            if not parts:
                raise HTTPException(400, f"Caminho inválido: {rel}")
            if len(parts) > 1 and (parts[0] == origem or (info_zip and parts[0] == cid)):
                parts = parts[1:]  # le répertoire choisi (ou le ZIP) lui-même n'est pas un sous-dossier du dossier
            if parts[-1].lower() in IGNORADOS:
                ignorados.append("/".join(parts))
                continue
            if (f.size or 0) > MAX_DOC_FILE:
                raise HTTPException(413, f"Arquivo grande demais: {rel}")
            dest = docs.joinpath(*parts)
            if dest.exists():
                raise HTTPException(400, f"Caminho duplicado: {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            n, sha = await f.salvar(dest)
            lista.append({"caminho": "/".join(parts), "original": rel, "tamanho": n, "sha256": sha,
                          "integrado_em_versao": None})
        if not lista:
            raise HTTPException(400, "A pasta não contém documentos")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lista.sort(key=lambda x: x["caminho"].lower())
        (tmp / "documentos.json").write_text(json.dumps({
            "base": "documentos", "origem": origem, "importado_em": now, "n": len(lista),
            "tamanho_total": sum(x["tamanho"] for x in lista), "ignorados": ignorados + (info_zip or {}).get("ignorados", []),
            **({"importado_de_zip": {k: v for k, v in info_zip.items() if k != "ignorados"}} if info_zip else {}), "arquivos": lista},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (tmp / "dossie.json").write_text(json.dumps({
            "criado_em": now, "nome": nome or origem, "pasta_origem": origem, "criado_por": USER or "local",
            "fonte": "criação pela aplicação web (importação de " + ("arquivo ZIP)" if info_zip else "pasta)")},
            ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(CASES / cid)  # le dossier n'apparaît qu'une fois complet
    except BaseException:
        for f in arquivos:
            f.desfazer()  # envoi par morceaux : les fichiers restent disponibles pour un nouvel essai
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if d_envio:
        shutil.rmtree(d_envio, onerror=_rm_force)
    return {"ok": True, "id": cid, "n": len(lista), "url": f"/?novo={quote(cid)}"}


def _rm_force(func, path, *_):
    os.chmod(path, 0o700)  # fichiers en lecture seule sous Windows
    func(path)


@app.post("/c/{cid}/excluir")
def excluir_dossie(cid: str, confirmo: str = Form("")):
    """Suppression définitive du dossier (documents, versions, livrables, réponses, métriques)."""
    d = case_dir(cid)
    if confirmo != cid or cid.startswith("."):
        raise HTTPException(400, "Confirmação ausente")
    lixo = CASES / f".del_{cid}_{secrets.token_hex(3)}"
    d.rename(lixo)  # disparaît immédiatement du tableau, même si l'effacement physique échoue
    shutil.rmtree(lixo, onerror=_rm_force)
    bkp.excluir(cid)  # suppression définitive annoncée : les sauvegardes du dossier sont effacées aussi
    _state.cache_clear()
    return RedirectResponse(f"/?excluido={quote(cid)}", status_code=303)


@app.post("/c/{cid}/documentos")
async def adicionar_documentos(request: Request, cid: str):
    """Ajoute des pièces au dossier : elles comptent dans le dossier ET dans « en attente de traitement »."""
    case_dir(cid)
    form = await request.form(max_files=MAX_DOCS, max_fields=20)
    caminhos, arquivos, d_envio, info_zip = await fontes_do_formulario(form)
    if sum(f.size or 0 for f in arquivos) > MAX_DOCS_TOTAL:
        raise HTTPException(413, "Envio grande demais")
    inv = inventario(cid) or {"base": "documentos", "origem": "", "importado_em": None, "n": 0, "tamanho_total": 0,
                              "ignorados": [], "arquivos": []}
    if len(inv["arquivos"]) + len(arquivos) > MAX_DOCS:
        raise HTTPException(413, "Arquivos demais")
    root = CASES / cid / "documentos"
    ocupados = {f["caminho"].lower() for f in inv["arquivos"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    novos, escritos = [], []
    try:
        for f, rel in zip(arquivos, caminhos):
            parts = caminho_seguro(rel)
            if not parts:
                raise HTTPException(400, f"Caminho inválido: {rel}")
            if parts[-1].lower() in IGNORADOS:
                continue
            if (f.size or 0) > MAX_DOC_FILE:
                raise HTTPException(413, f"Arquivo grande demais: {rel}")
            base_nome, i = parts[-1], 1
            stem, suf = Path(base_nome).stem, Path(base_nome).suffix
            while "/".join(parts).lower() in ocupados or root.joinpath(*parts).exists():
                i += 1
                parts[-1] = f"{stem} ({i}){suf}"
            dest = root.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            escritos.append(dest)
            n, sha = await f.salvar(dest)
            cam = "/".join(parts)
            ocupados.add(cam.lower())
            novos.append({"caminho": cam, "original": rel, "tamanho": n, "sha256": sha, "armazenado": "documentos",
                          "adicionado_em": now, "adicionado_por": USER or "local", "integrado_em_versao": None,
                          **({"zip_origem": info_zip["arquivo"]} if info_zip else {})})
    except BaseException:
        for f in arquivos:
            f.desfazer()  # envoi par morceaux : les fichiers retournent dans l'envoi
        for p in escritos:
            p.unlink(missing_ok=True)
        raise
    if d_envio:
        shutil.rmtree(d_envio, onerror=_rm_force)
    if not novos:
        raise HTTPException(400, "Nenhum documento a adicionar")
    inv["arquivos"] = sorted(inv["arquivos"] + novos, key=lambda x: x["caminho"].lower())
    if info_zip:
        inv.setdefault("zips_importados", []).append({**info_zip, "importado_em": now})
    inv["n"] = len(inv["arquivos"])
    inv["tamanho_total"] = sum(x["tamanho"] for x in inv["arquivos"])
    (CASES / cid / "documentos.json").write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "n": len(novos)}


# ---------------------------------------------------------------- journal complet de l'agent (audit)
RUN_NOME = re.compile(r"^[0-9A-Za-z_-]+_pedido$")
J_ROTULOS = {"inicio": "Instruções dadas ao agente", "etapa": "Etapa", "texto": "Texto do agente", "raciocinio": "Raciocínio",
             "ferramenta": "Ferramenta usada", "resultado_ferramenta": "Resultado da ferramenta", "recusa": "Ação recusada",
             "subagente_inicio": "Subagente iniciado", "subagente_fim": "Subagente concluído",
             "tarefa_inicio": "Comando em segundo plano iniciado", "tarefa_fim": "Comando em segundo plano concluído", "resultado": "Resultado final",
             "fim": "Fim da execução"}


def _j_texto(v) -> str:
    """Texte d'un champ du journal (y compris un texte tronqué : début + taille et empreinte du texte complet)."""
    if isinstance(v, dict) and v.get("truncado"):
        return f"{v.get('inicio', '')}\n[… truncado: {v.get('tamanho')} caracteres no total, SHA-256 {str(v.get('sha256'))[:16]}…]"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, indent=2)
    return "" if v is None else str(v)


def _j_linha(e: dict) -> dict:
    t = e.get("tipo", "")
    primeira = lambda x: (_j_texto(x).strip().splitlines() or [""])[0][:220]
    if t == "inicio":
        resumo = f"{e.get('modelo', '')} · v{e.get('versao_origem', 0)} → v{e.get('versao_alvo', '?')}"
        corpo = "\n\n".join(f"### {k}\n{_j_texto(e[k])}" for k in ("pedido", "prompt_sistema", "subagente_prompt", "ferramentas_livres",
                                                                    "subagentes_max", "orcamento_usd") if k in e)
    elif t in ("etapa", "texto", "raciocinio"):
        resumo, corpo = primeira(e.get("texto")), _j_texto(e.get("texto"))
    elif t == "ferramenta":
        par = e.get("parametros") or {}
        alvo = next((par[k] for k in ("file_path", "command", "pattern", "url", "query", "description", "path") if k in par), "")
        resumo, corpo = f"{e.get('nome', '')} · {primeira(alvo)}", _j_texto(par)
    elif t == "resultado_ferramenta":
        c = _j_texto(e.get("conteudo"))
        resumo, corpo = ("ERRO · " if e.get("erro") else "") + f"{len(c):,} car. · ".replace(",", " ") + primeira(c), c
    elif t == "recusa":
        resumo, corpo = f"{e.get('ferramenta', '')} — {e.get('motivo', '')}", _j_texto(e.get("parametros"))
    elif t.startswith(("subagente", "tarefa")):
        resumo, corpo = _j_texto(e.get("descricao") or e.get("status") or ""), _j_texto(e.get("resumo") or "")
    elif t == "resultado":
        resumo = f"custo {e.get('custo_usd')} US$ · {e.get('turnos')} turnos" + (" · ERRO" if e.get("erro") else "")
        corpo = _j_texto(e.get("texto"))
    elif t == "fim":
        resumo = str(e.get("estado", "")) + (f" · v{e['versao_produzida']}" if e.get("versao_produzida") else "") + \
            (f" · {e['erro']}" if e.get("erro") else "")
        corpo = _j_texto(e.get("rastreamento") or "")
    else:
        resumo, corpo = primeira(e), _j_texto(e)
    return {"n": e.get("n"), "hora": str(e.get("ts", ""))[11:23], "tipo": t, "rotulo": J_ROTULOS.get(t, t), "agente": e.get("agente", ""),
            "resumo": resumo, "corpo": corpo if corpo.strip() and corpo.strip() != resumo.strip() else ""}


def _j_reclassificar(eventos: list[dict]) -> list[dict]:
    """Journaux antérieurs à la correction : une commande lancée en arrière-plan (Bash…) y était notée comme sous-agent.
    On la reclasse à l'affichage, sans toucher au fichier (chaîne d'empreintes intacte)."""
    outils = {e.get("id"): e.get("nome") for e in eventos if e.get("tipo") == "ferramenta"}
    out = []
    for e in eventos:
        t, ag = e.get("tipo", ""), str(e.get("agente") or "")
        if t in ("subagente_inicio", "subagente_fim") and ag.startswith("subagente: "):
            nome = outils.get(ag[len("subagente: "):])
            if nome and nome not in OUTILS_SUBAGENTE:
                e = {**e, "tipo": t.replace("subagente", "tarefa"), "agente": "principal"}
        out.append(e)
    return out


def execucoes(cid) -> list[dict]:
    d = CASES / cid / "runs"
    out = []
    for rd in sorted((x for x in d.iterdir() if x.is_dir() and RUN_NOME.match(x.name)), reverse=True) if d.is_dir() else []:
        try:
            st = json.loads((rd / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        out.append({"run": rd.name, "inicio": st.get("inicio"), "estado": st.get("estado", "solicitado"),
                    "versao": st.get("versao_produzida"), "custo_usd": st.get("custo_usd"), "simulacao": st.get("simulacao"),
                    "duracao_s": (st.get("duracao_s") if st.get("duracao_s") is not None else duracao_total(st))
                                 if st.get("fim") and st.get("inicio") else None,
                    "retomada_de": st.get("retomada_de"),
                    "journal": (rd / jrn.NOME).exists()})
    return out


def _journal_path(cid, run: str = "", versao: int | None = None) -> Path | None:
    if versao is not None:
        p = vdir(cid, versao) / jrn.NOME_VERSAO
        return p if p.exists() else None
    if run and RUN_NOME.match(run):
        p = CASES / cid / "runs" / run / jrn.NOME
        return p if p.exists() else None
    return None


@app.get("/c/{cid}/journal", response_class=HTMLResponse)
def journal_pagina(request: Request, cid: str, run: str = "", versao: int | None = None):
    """Journal complet de l'agent : consignes, textes, raisonnements, outils et résultats, refus, sous-agents, coût."""
    case_dir(cid)
    lang = get_lang(request)
    exs = execucoes(cid)
    if not run and versao is None:
        run = next((x["run"] for x in exs if x["journal"]), "")
    p = _journal_path(cid, run, versao)
    eventos = [_j_linha(e) for e in _j_reclassificar(jrn.ler(p))] if p else []
    tipos = sorted({e["tipo"] for e in eventos}, key=lambda t: list(J_ROTULOS).index(t) if t in J_ROTULOS else 99)
    return render(request, "journal.html", {
        "cid": cid, "exs": exs, "run": run, "versao": versao, "eventos": eventos, "tipos": tipos, "J_ROTULOS": J_ROTULOS,
        "agentes": sorted({e["agente"] for e in eventos}), "integridade": jrn.verificar(p) if p else None,
        "detalhado": any(e["tipo"] == "ferramenta" for e in eventos), "ex": next((x for x in exs if x["run"] == run), None),
        **formatadores(lang), **hdr_ctx(cid, lang)})


@app.get("/c/{cid}/journal.jsonl")
def journal_bruto(cid: str, run: str = "", versao: int | None = None):
    case_dir(cid)
    p = _journal_path(cid, run, versao)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, filename=f"{cid}_journal_{run or f'v{versao}'}.jsonl", media_type="application/x-ndjson")


CODE_ROOT = Path(__file__).resolve().parent.parent
RESULTADO_VISIVEL_S = 24 * 3600  # le bandeau de résultat reste affiché 24 h


def ultimo_pedido(cid) -> dict | None:
    """Dernière demande de mise à jour (cases/<ID>/runs/<ts>_pedido/) avec l'état écrit par le worker."""
    d = CASES / cid / "runs"
    for rd in sorted(d.glob("*_pedido"), reverse=True) if d.is_dir() else []:
        try:
            ped = json.loads((rd / "pedido.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        st = {}
        try:
            st = json.loads((rd / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if st.get("fim") and st.get("duracao_s") is None and st.get("inicio"):
            st["duracao_s"] = duracao_total(st)  # exécutions antérieures à l'enregistrement de la durée
        ped.update(_dir=rd.name, status=st)
        if st.get("estado") in ("concluido", "simulado", "erro", "parado"):
            ped["estado"] = st["estado"]
        return ped
    return None


def pedido_atual(cid) -> dict | None:
    """Bandeau du tableau de bord : demande en attente de démarrage, ou résultat récent de la dernière exécution."""
    ped = ultimo_pedido(cid)
    if not ped:
        return None
    if ped["estado"] in ("concluido", "simulado", "erro", "parado"):
        fim = ped["status"].get("fim") or ped["status"].get("atualizado_em")
        try:
            idade = (datetime.now(timezone.utc) - datetime.fromisoformat(fim)).total_seconds()
        except (TypeError, ValueError):
            idade = 0
        return ped if idade < RESULTADO_VISIVEL_S else None
    if ped["estado"] == "solicitado":
        return ped
    return None  # em_execucao : le cerveau du tableau de bord montre l'exécution


def retomada_disponivel(cid) -> dict | None:
    """La dernière mise à jour s'est arrêtée (erreur, arrêt, interruption) et son travail a été mis de côté : elle peut
    être reprise tant qu'aucune autre version n'a été produite depuis."""
    ped = ultimo_pedido(cid)
    if not ped or ped["estado"] not in ("erro", "parado"):
        return None
    rp = CASES / cid / "runs" / ped["_dir"] / RETOMADA / "retomada.json"
    try:
        info = json.loads(rp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    vs = versions(cid)
    if (vs[0] if vs else 0) != info.get("versao_origem"):
        return None
    st = ped["status"]
    return {"run": ped["_dir"], "motivo": info.get("motivo") or st.get("erro") or "", "em": info.get("em"),
            "duracao_s": st.get("duracao_s") or info.get("duracao_s") or 0, "sessao": info.get("sessao"),
            "custo_usd": st.get("custo_usd")}


DOMINIO_OK = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
_lista_lock = threading.Lock()


def site_autorizado(h: str) -> bool:
    dominios, sufixos = carregar_lista_branca()
    h = (h or "").lower()
    return h in dominios or any(h == x or h.endswith("." + x) for x in sufixos)


def recusas_do_run(cid, run: str) -> list[dict]:
    """Envois refusés pendant une exécution (status.json), avec pour chacun : sites désormais autorisés ?"""
    if not RUN_NOME.match(run or ""):
        return []
    try:
        st = json.loads((CASES / cid / "runs" / run / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [{**e, "autorizado": all(site_autorizado(s) for s in e.get("sites", []) if not s.startswith("("))
             and not any(s.startswith("(") for s in e.get("sites", []))} for e in st.get("envios_recusados") or []]


@app.post("/c/{cid}/lista-branca")
def autorizar_site(cid: str, dominio: str = Form(...), motivo: str = Form(""), run: str = Form("")):
    """« Autoriser ce site » : le superviseur ajoute à la liste blanche un site refusé à l'agent (motif obligatoire,
    trace de qui / quand / depuis quel dossier). Seuls les sites effectivement refusés dans cette exécution sont proposés."""
    case_dir(cid)
    dominio, motivo = dominio.strip().lower(), " ".join(motivo.split())[:300]
    if not motivo:
        raise HTTPException(400, "Motivo obrigatório para autorizar um site")
    if not DOMINIO_OK.match(dominio):
        raise HTTPException(400, "Domínio inválido")
    if not any(dominio in e.get("sites", []) for e in recusas_do_run(cid, run)):
        raise HTTPException(400, "Este site não foi recusado nesta execução")
    with _lista_lock:
        arq = _worker.DOMINIOS_OFICIAIS
        d = json.loads(arq.read_text(encoding="utf-8"))
        if not site_autorizado(dominio):
            d.setdefault("dominios", []).append({
                "dominio": dominio, "motivo": motivo, "autorizado_por": USER or "local",
                "autorizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "origem": {"dossie": cid, "execucao": run}})
            d["_atualizado_em"] = datetime.now(timezone.utc).date().isoformat()
            tmp = arq.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(arq)
    vs = versions(cid)
    return RedirectResponse(f"/c/{cid}/v{vs[0]}/painel" if vs else f"/c/{cid}/painel", status_code=303)


def recusas_painel(cid) -> dict | None:
    """Encadré ⚠ du bandeau de résultat : envois refusés pendant la dernière exécution, sites encore à autoriser,
    et possibilité de relancer les consultations une fois les sites autorisés."""
    ped = pedido_atual(cid)
    if not ped or ped["estado"] == "solicitado":
        return None
    er = recusas_do_run(cid, ped["_dir"])
    if not er:
        return None
    sites = sorted({x for e in er for x in e.get("sites", [])})
    return {"run": ped["_dir"], "envios": er, "sites": [{"site": x, "autorizado": not x.startswith("(") and site_autorizado(x),
                                                          "explicito": not x.startswith("(")} for x in sites],
            "relancavel": any(e["autorizado"] for e in er)}


# ---------------------------------------------------------------- requête de mise à jour (crayon orange, pastille « R »)
REQUETE_MAX = 4000


def requete_atual(cid) -> dict | None:
    try:
        rq = json.loads((CASES / cid / "requete.json").read_text(encoding="utf-8"))
        return rq if (rq.get("texto") or "").strip() else None
    except (OSError, ValueError):
        return None


def _painel_url(cid):
    vs = versions(cid)
    return f"/c/{cid}/v{vs[0]}/painel" if vs else f"/c/{cid}/painel"


@app.post("/c/{cid}/requete")
def gravar_requete(cid: str, texto: str = Form("")):
    """Requête de mise à jour : enregistrée sur le dossier, elle sera traitée par la prochaine mise à jour
    (le bouton « Mettre à jour le dossier » devient actif)."""
    case_dir(cid)
    texto = texto.strip()
    if not texto:
        raise HTTPException(400, "Requisição vazia")
    if len(texto) > REQUETE_MAX:
        raise HTTPException(400, f"Requisição longa demais (máximo {REQUETE_MAX} caracteres)")
    if run_atual(cid):
        raise HTTPException(409, "Uma atualização já está em execução para este dossiê")
    if case_status(cid, versions(cid), case_meta(cid)) == "tratado":
        raise HTTPException(409, "Dossiê concluído: reabra-o antes de atualizá-lo")
    (CASES / cid / "requete.json").write_text(json.dumps({"texto": texto, "por": USER or "local",
        "em": datetime.now(timezone.utc).isoformat(timespec="seconds")}, ensure_ascii=False, indent=2), encoding="utf-8")
    return RedirectResponse(_painel_url(cid), status_code=303)


@app.post("/c/{cid}/requete/apagar")
def apagar_requete(cid: str):
    case_dir(cid)
    if run_atual(cid):
        raise HTTPException(409, "Uma atualização já está em execução para este dossiê")
    (CASES / cid / "requete.json").unlink(missing_ok=True)
    return RedirectResponse(_painel_url(cid), status_code=303)


# ---------------------------------------------------------------- conversation avec l'agent (bulle en bas à droite)
CONVERSA_MAX = 4000
CONVERSA_EXPIRA_S = 20 * 60


def _conversa_ler(cid) -> dict:
    from kyc_agent import conversa as K
    c = K.ler(CASES / cid)
    if c.get("estado") == "pensando":  # processus disparu : on débloque la conversation
        try:
            desde = datetime.fromisoformat(c.get("desde")).timestamp()
        except (TypeError, ValueError):
            desde = 0
        if time.time() - desde > CONVERSA_EXPIRA_S:
            c["mensagens"].append({"papel": "agente", "erro": True, "texto": "Pas de réponse de l'agent (délai dépassé).",
                                   "em": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            c["estado"], c["atividade"] = "pronto", ""
            K.gravar(CASES / cid, c)
    return c


def _conversa_json(c: dict) -> dict:
    return {"estado": c.get("estado") or "pronto", "atividade": c.get("atividade") or "",
            "mensagens": [{k: m.get(k) for k in ("papel", "texto", "em", "por", "versao", "custo_usd", "erro", "aviso")}
                          for m in c.get("mensagens") or []],
            "custo_total_usd": round(sum(m.get("custo_usd") or 0 for m in c.get("mensagens") or []), 4)}


@app.get("/c/{cid}/conversa.json")
def conversa_estado(cid: str):
    case_dir(cid)
    return _conversa_json(_conversa_ler(cid))


@app.post("/c/{cid}/conversa")
def conversa_enviar(cid: str, texto: str = Form("")):
    """Question du superviseur : l'agent répond en lecture seule (aucune modification du dossier, aucune version)."""
    from kyc_agent import conversa as K
    case_dir(cid)
    texto = texto.strip()
    if not texto:
        raise HTTPException(400, "Mensagem vazia")
    if len(texto) > CONVERSA_MAX:
        raise HTTPException(400, f"Mensagem longa demais (máximo {CONVERSA_MAX} caracteres)")
    c = _conversa_ler(cid)
    if c.get("estado") == "pensando":
        raise HTTPException(409, "O agente ainda está respondendo")
    vs = versions(cid)
    agora_ = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c.setdefault("mensagens", []).append({"papel": "usuario", "texto": texto, "em": agora_, "por": USER or "local",
                                          "versao": vs[0] if vs else None})
    c.update(estado="pensando", desde=agora_, atividade="")
    K.gravar(CASES / cid, c)
    _lancar("kyc_agent.conversa", [str(CASES / cid)], CASES / cid / K.PASTA / "worker.log")
    return _conversa_json(c)


@app.post("/c/{cid}/conversa/nova")
def conversa_nova(cid: str):
    """Nouvelle conversation : l'ancienne est archivée (conversa/conversa_<date>.json), l'agent repart sans contexte."""
    from kyc_agent import conversa as K
    case_dir(cid)
    c = _conversa_ler(cid)
    if c.get("estado") == "pensando":
        raise HTTPException(409, "O agente ainda está respondendo")
    arq = K.arquivo(CASES / cid)
    if arq.exists() and c.get("mensagens"):
        arq.replace(arq.with_name(f"conversa_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"))
    return _conversa_json(K.ler(CASES / cid))


def _lancar(modulo: str, args: list[str], log: Path):
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log.parent.mkdir(parents=True, exist_ok=True)
    out = log.open("ab")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    subprocess.Popen([sys.executable, "-m", modulo, *args], cwd=str(CODE_ROOT), stdout=out, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, env=env, creationflags=flags, start_new_session=os.name != "nt")


def lancar_worker(cid, rd: Path):
    """Démarre le worker Agent SDK dans un processus séparé (survit à la requête HTTP)."""
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = (rd / "worker.log").open("ab")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    subprocess.Popen([sys.executable, "-m", "kyc_agent.worker", str(CASES / cid), str(rd)], cwd=str(CODE_ROOT),
                     stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                     creationflags=flags, start_new_session=os.name != "nt")


@app.post("/c/{cid}/atualizar")
def solicitar_atualizacao(cid: str, retomar: str = Form(""), reconsultar: str = Form(""), modo: str = Form("")):
    """Bouton « Run » : enregistre la demande (nombre d'agents) puis lance le worker Claude Agent SDK, qui écrit
    runs/<ts>_pedido/status.json + log.jsonl et produit la version suivante. Sans clé API : simulation."""
    case_dir(cid)
    if run_atual(cid):
        raise HTTPException(409, "Uma atualização já está em execução para este dossiê")
    if case_status(cid, versions(cid), case_meta(cid)) == "tratado":
        raise HTTPException(409, "Dossiê concluído: reabra-o antes de atualizá-lo")
    pend = pendentes_agente(cid)
    # consultations refusées puis autorisées par le superviseur (liste blanche) : à refaire par l'agent
    reconsultas = [e for e in recusas_do_run(cid, reconsultar or retomar) if e["autorizado"]]
    if reconsultar and not reconsultas:
        raise HTTPException(409, "Nenhuma consulta bloqueada foi autorizada desde então")
    rq = requete_atual(cid)
    if not pend["total"] and not reconsultas and not rq:
        raise HTTPException(400, "Nenhum documento aguardando tratamento")
    ret = None
    if retomar:
        ret = retomada_disponivel(cid)
        if not ret or ret["run"] != retomar:
            raise HTTPException(409, "Esta atualização não pode mais ser retomada")
    # mode choisi au lancement : agent seul, ou multi-agents (bêta) ; une reprise garde le mode de l'exécution reprise
    if ret:
        try:
            modo = json.loads((CASES / cid / "runs" / ret["run"] / "pedido.json").read_text(encoding="utf-8")).get("modo") or modo
        except (OSError, ValueError):
            pass
    modo = modo if modo in MODOS else MODO_AGENTE
    now = datetime.now(timezone.utc)
    rd = CASES / cid / "runs" / f"{now:%Y%m%dT%H%M%S}_pedido"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "pedido.json").write_text(json.dumps({
        "estado": "solicitado", "solicitado_em": now.isoformat(timespec="seconds"),
        "solicitado_por": USER or "local", "total": pend["total"], "modo": modo,
        "pendentes": {"pecas": [f["caminho"] for f in pend["docs"]], "respostas": [r["_dir"] for r in pend["respostas"]]},
        **({"requete": rq} if rq else {}),
        **({"reconsultas": [{k: e[k] for k in ("sites", "ferramenta", "trecho", "em")} for e in reconsultas],
            "reconsultas_de": reconsultar or retomar} if reconsultas else {}),
        **({"retomada": {"de": ret["run"], "sessao": ret["sessao"], "motivo": ret["motivo"],
                         "duracao_anterior_s": ret["duracao_s"], "custo_anterior_usd": ret["custo_usd"]}} if ret else {})},
        ensure_ascii=False, indent=2), encoding="utf-8")
    # le travail mis de côté par les exécutions précédentes n'est plus proposé (sauf celui qu'on reprend)
    for x in (CASES / cid / "runs").glob(f"*/{RETOMADA}"):
        if not ret or x.parent.name != ret["run"]:
            shutil.rmtree(x, onerror=_rm_force)
    # état initial écrit tout de suite : le tableau de bord passe en « en cours » sans attendre le démarrage du worker
    (rd / "status.json").write_text(json.dumps({
        "estado": "em_curso", "inicio": now.isoformat(timespec="seconds"), "atualizado_em": now.isoformat(timespec="seconds"),
        "tarefa": "Reprise de la mise à jour…" if ret else "Démarrage de l'agent…", "modelo": "claude-opus-5-5", "uso": {},
        "duracao_anterior_s": ret["duracao_s"] if ret else 0},
        ensure_ascii=False, indent=2), encoding="utf-8")
    lancar_worker(cid, rd)
    vs = versions(cid)
    return RedirectResponse(f"/c/{cid}/v{vs[0]}/painel" if vs else f"/c/{cid}/painel", status_code=303)


def _matar_arvore(pid: int):
    """Arrêt forcé du worker et de ses processus enfants (programme Claude, outils)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


PRAZO_PARADA_S = 45  # délai laissé au worker pour s'arrêter proprement avant l'arrêt forcé


def _garantir_parada(cid: str, rd: Path):
    """Le worker vérifie la demande d'arrêt toutes les 2 s et remet lui-même le dossier en état. S'il ne répond pas
    dans le délai (bloqué dans un outil), il est arrêté de force et l'application remet le dossier en état."""
    sp = rd / "status.json"
    for _ in range(PRAZO_PARADA_S):
        time.sleep(1)
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if st.get("estado") != "em_curso":
            return
    recente = time.time() - sp.stat().st_mtime < INTERROMPIDO_S  # évite de viser un PID réattribué à un autre programme
    if st.get("pid") and recente:
        _matar_arvore(int(st["pid"]))
    reverter(CASES / cid, rd, "Mise à jour arrêtée par l'utilisateur (arrêt forcé : le processus ne répondait pas).", estado="parado")


@app.post("/c/{cid}/parar")
def parar_agentes(cid: str):
    """Bouton « Arrêter les agents » : demande d'arrêt ; le dossier est remis dans l'état d'avant l'exécution."""
    case_dir(cid)
    run = run_atual(cid)
    if not run:
        raise HTTPException(409, "Nenhuma atualização em execução")
    rd = CASES / cid / "runs" / run["id"]
    if not (rd / "parar.json").exists():
        (rd / "parar.json").write_text(json.dumps({"solicitado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                   "solicitado_por": USER or "local"}, ensure_ascii=False), encoding="utf-8")
        threading.Thread(target=_garantir_parada, args=(cid, rd), daemon=True).start()
    vs = versions(cid)
    return RedirectResponse(f"/c/{cid}/v{vs[0]}/painel" if vs else f"/c/{cid}/painel", status_code=303)


@app.post("/c/{cid}/status")
def mudar_status(cid: str, acao: str = Form(...)):
    case_dir(cid)
    m = case_meta(cid)
    if acao == "tratar":
        if not versions(cid):
            raise HTTPException(400, "Dossiê sem análise: não pode ser marcado como tratado")
        m.update(tratado_em=datetime.now(timezone.utc).isoformat(timespec="seconds"), tratado_por=USER or "local")
    elif acao == "reabrir":
        if not m.get("tratado_em"):
            raise HTTPException(400, "Dossiê não está concluído")
        m.pop("tratado_em", None)
        m.pop("tratado_por", None)
    else:
        raise HTTPException(400)
    vs = versions(cid)
    m.setdefault("historico_status", []).append({"acao": acao, "por": USER or "local", "versao": vs[0] if vs else None,
                                                 "em": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    save_meta(cid, m)
    return RedirectResponse(f"/c/{cid}", status_code=303)


@app.get("/c/{cid}/v{v}/{tab}", response_class=HTMLResponse)
def case_tab(request: Request, cid: str, v: int, tab: str):
    if tab not in dict(TABS):
        raise HTTPException(404)
    lang = get_lang(request)
    st_pt = load_state(cid, v)
    d = dico(vdir(cid, v), lang)
    st = tr_data(st_pt, d)
    dl = deliverables(cid, v)
    ctx = {"cid": cid, "v": v, "vs": versions(cid), "tab": tab, "tabs": TABS, "s": st, "dl": dl,
           "base": f"/c/{cid}/v{v}/", "ntar": n_acoes(cid, v, st),
           "badges": dict(zip(("verificacoes", "mensagens"), contagem_acoes(cid, v, st))), "lang": lang, "tr_missing": lang == "fr" and not d}
    resp = [r for r in listar_respostas(CASES / cid) if r.get("versao") == v]
    ctx["respostas"] = {}
    for r in resp:
        if d:  # libellés seulement ; le texte saisi par l'humain n'est jamais modifié
            r = {**r, "rotulo": d.get(r.get("rotulo"), r.get("rotulo")), "confirmacao": d.get(r.get("confirmacao"), r.get("confirmacao"))}
        ctx["respostas"].setdefault(r["item"], []).append(r)
    ctx["acoes"] = {it["id"]: acoes(it) for it in st.get("registro_tecnico", []) if it.get("acao_humana")}
    ctx["acoes"].update({m["id"]: acoes_mensagem(m) for m in st.get("mensagens", [])})
    ctx["pend_titulos"] = {p["id"]: p.get("item") or p.get("elemento_faltante") or p["id"] for p in st.get("pendencias", [])}
    ctx["ok"] = request.query_params.get("ok")
    ctx.update(formatadores(lang))
    ctx.update(hdr_ctx(cid, lang))
    meta = case_meta(cid)
    inv = inventario(cid)
    ctx.update(meta=meta, status_dossie=case_status(cid, ctx["vs"], meta), STATUS=STATUS, n_docs=inv["n"] if inv else None,
               pend_agente=pendentes_agente(cid), agente=estado_agente(cid, lang), pedido=pedido_atual(cid),
               retomada=retomada_disponivel(cid), recusas=recusas_painel(cid), requete=requete_atual(cid))
    ctx.update(graficos(ctx["agente"], lang))
    if tab == "painel":
        ctx.update(painel(cid, v, st, d, lang))
    elif tab == "matriz":
        ctx["sheets"] = tr_data(_xlsx(str(dl["matriz"]), dl["matriz"].stat().st_mtime), d) if "matriz" in dl else []
    elif tab == "mensagens":
        msgs = messages(cid, v, st)
        if d:  # l'original portugais reste celui qu'on copie / envoie
            for m, mp in zip(msgs, messages(cid, v, st_pt)):
                m["corpo_pt"], m["mailto"] = mp["corpo"], mp["mailto"]
        ctx["msgs"] = msgs
    elif tab == "provas":
        idx, names = zip_index(cid, v)
        idx = tr_data(idx, d)
        pastas = {}
        for r in idx:
            pastas.setdefault(r["pasta"], []).append(r)
        ctx["pastas"] = pastas
        ctx["n_arquivos"] = len(idx)
        ctx["zip_size"] = dl["zip"].stat().st_size if "zip" in dl else 0
    return render(request, f"{tab}.html", ctx)


# ---------------------------------------------------------------- réponses humaines (boîte de réception)
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 10
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@app.post("/c/{cid}/v{v}/resposta")
async def resposta(request: Request, cid: str, v: int, item: str = Form(...), acao: str = Form(...),
                   texto: str = Form(""), confirmo: str = Form(""), arquivos: list[UploadFile] = File(default=[]),
                   pendencias: list[str] = Form(default=[])):
    st = load_state(cid, v)
    it = next((x for x in st.get("registro_tecnico", []) if x["id"] == item and x.get("acao_humana")), None)
    msg = next((x for x in st.get("mensagens", []) if x["id"] == item), None) if not it else None
    if not it and not msg:
        raise HTTPException(404, "Item inexistente")
    a = next((x for x in (acoes(it) if it else acoes_mensagem(msg)) if x["id"] == acao), None)
    if not a:
        raise HTTPException(404, "Ação inexistente")
    texto = (texto or "").strip()
    arquivos = [f for f in arquivos if f and f.filename]
    erros = []
    if a.get("texto") == "obrigatorio" and not texto:
        erros.append("texto obrigatório")
    if a.get("arquivos") == "obrigatorio" and not arquivos:
        erros.append("arquivo obrigatório")
    if a.get("arquivos", "nao") == "nao" and arquivos:
        erros.append("esta ação não aceita arquivos")
    if a.get("um_dos_dois") and not texto and not arquivos:
        erros.append("informe o texto da resposta e/ou junte arquivos")
    if a.get("confirmacao") and confirmo != "1":
        erros.append("confirmação necessária")
    if len(arquivos) > (MAX_FILES if a.get("multiplo") else 1):
        erros.append("arquivos demais")
    aceita = {e.strip().lower() for e in (a.get("aceita") or DEFAULT_ACEITA).split(",")}
    for f in arquivos:
        if Path(f.filename).suffix.lower() not in aceita:
            erros.append(f"tipo não aceito: {f.filename}")
    if len(texto) > 20000:
        erros.append("texto longo demais")
    dispensadas = None
    if a.get("dispensa") and "pendencias" in a:
        dispensadas = [p for p in dict.fromkeys(pendencias) if p in a["pendencias"]]
        if a["pendencias"] and not dispensadas:
            erros.append("escolha ao menos uma pendência")
    if erros:
        raise HTTPException(400, "; ".join(erros))
    now = datetime.now(timezone.utc)
    d = CASES / cid / "respostas" / f"{now:%Y%m%dT%H%M%S}_{secrets.token_hex(2)}_{item}_{acao}"
    d.mkdir(parents=True, exist_ok=False)
    salvos = []
    for i, f in enumerate(arquivos, 1):
        data = await f.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise HTTPException(413, f"arquivo grande demais: {f.filename}")
        nome = f"{i:02d}_" + SAFE_NAME.sub("_", Path(f.filename).name)[:120]
        (d / nome).write_bytes(data)
        salvos.append({"nome": nome, "original": f.filename, "tamanho": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    reg = {"case": cid, "versao": v, "item": item, "acao": acao, "rotulo": a.get("rotulo"), "texto": texto,
           "confirmacao": a.get("confirmacao") if confirmo == "1" else None, "arquivos": salvos,
           "autor": USER or "local", "recebida_em": now.isoformat(timespec="seconds"), "status": "recebida",
           "integrada_em_versao": None, **({"dispensa": True} if a.get("dispensa") else {}),
           **({"pendencias_dispensadas": dispensadas} if dispensadas is not None else {})}
    (d / "resposta.json").write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    aba = "mensagens" if msg else "verificacoes"
    return RedirectResponse(f"/c/{cid}/v{v}/{aba}?ok={item}#{item}", status_code=303)


@app.get("/c/{cid}/v{v}/file/{kind}")
def case_file(cid: str, v: int, kind: str, download: int = 0):
    dl = deliverables(cid, v)
    if kind.startswith("msg-"):
        name = kind[4:]
        p = vdir(cid, v) / "mensagens" / name
        if "/" in name or "\\" in name or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="text/markdown; charset=utf-8", filename=name)
    if kind not in dl:
        raise HTTPException(404)
    p = dl[kind]
    media = {".pdf": "application/pdf", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             ".zip": "application/zip", ".md": "text/markdown; charset=utf-8"}[p.suffix]
    return FileResponse(p, media_type=media, filename=p.name, content_disposition_type="attachment" if download or p.suffix != ".pdf" else "inline")


@app.get("/c/{cid}/v{v}/zip/item")
def zip_item(cid: str, v: int, path: str, download: int = 0):
    dl = deliverables(cid, v)
    if "zip" not in dl:
        raise HTTPException(404)
    _, names = zip_index(cid, v)
    if path not in names:  # liste blanche : uniquement les membres réels de l'archive
        raise HTTPException(404)
    z = zipfile.ZipFile(dl["zip"])
    name = path.split("/")[-1]
    ext = name.rsplit(".", 1)[-1].lower()
    media = {"pdf": "application/pdf", "png": "image/png", "json": "application/json", "csv": "text/csv; charset=utf-8",
             "md": "text/markdown; charset=utf-8", "txt": "text/plain; charset=utf-8", "xml": "application/xml"}.get(ext, "application/octet-stream")
    inline = not download and ext in ("pdf", "png", "json", "txt", "md", "csv", "xml")
    disp = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}"

    def gen():
        with z, z.open(path) as f:
            while chunk := f.read(1 << 16):
                yield chunk
    return StreamingResponse(gen(), media_type=media, headers={"Content-Disposition": disp})
