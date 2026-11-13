"""Métriques d'un dossier : tokens, coût équivalent API, durées.

Fichiers (tous facultatifs, l'app affiche « non mesuré » s'ils manquent) :
- cases/<ID>/dossie.json                 {"criado_em": ISO, "fonte": "..."}
- cases/<ID>/versions/vN/metricas.json   {"medido": true, "modelo": "...", "uso": {in,out,cw5,cw1h,cr}, "duracao_s": n,
                                          "inicio": ISO, "fim": ISO}  — écrit par chaque exécution de l'agent (v3+)
                                          ou {"medido": false, "nota": "..."} pour v1/v2.
- cases/<ID>/metricas/sessao_inicial.json  total de la session Claude Code initiale (info : inclut le développement de l'app).

Tarifs : référence Claude API (skill claude-api, shared/models.md, consulté le 2026-11-10) — Opus 5.5 :
entrée 4 $/MTok, sortie 20 $/MTok, lecture cache 0,20 $/MTok ; écriture cache = 1,25× entrée (TTL 5 min) ou 2× (TTL 1 h).
Le coût est un ÉQUIVALENT API : l'usage actuel passe par l'abonnement Claude, il n'est pas facturé au token.

CLI : python -m kyc_agent.metricas import-sessao <transcript.jsonl> <case_dir>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

TARIFAS = {  # $ par million de tokens
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cr": 0.20, "cw5": 5.0, "cw1h": 8.0},
}
FONTE_TARIFAS = "Tarifa pública Claude API (Opus 5.5), consultada em 10/11/2026"
CAMPOS = ("in", "out", "cw5", "cw1h", "cr")


def custo(uso: dict, modelo: str) -> float | None:
    t = TARIFAS.get(modelo)
    if not t:
        return None
    return sum((uso.get(k) or 0) * t[k] for k in CAMPOS) / 1e6


def custo_versao(m: dict) -> float:
    """Coût d'une version mesurée : total exact renvoyé par le SDK (custo_usd) sinon calcul sur le tarif public."""
    if m.get("custo_usd") is not None:
        return float(m["custo_usd"])
    return custo(m.get("uso") or {}, m.get("modelo", "")) or 0.0


def totais(uso: dict) -> dict:
    entrada = sum(uso.get(k) or 0 for k in ("in", "cw5", "cw1h", "cr"))
    return {"entrada": entrada, "saida": uso.get("out") or 0, "total": entrada + (uso.get("out") or 0)}


def ler_transcript(path: Path) -> dict:
    """Agrège l'usage d'un transcript Claude Code (dédupliqué par id de message)."""
    seen, first, last, modelos = {}, None, None, set()
    for line in Path(path).open(encoding="utf-8"):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        ts = o.get("timestamp")
        if ts:
            first, last = first or ts, ts
        m = o.get("message")
        if o.get("type") == "assistant" and isinstance(m, dict) and m.get("usage"):
            seen[m.get("id") or o.get("uuid")] = m["usage"]
            if m.get("model"):
                modelos.add(m["model"])
    uso = dict.fromkeys(CAMPOS, 0)
    for u in seen.values():
        cc = u.get("cache_creation") or {}
        uso["in"] += u.get("input_tokens") or 0
        uso["out"] += u.get("output_tokens") or 0
        uso["cr"] += u.get("cache_read_input_tokens") or 0
        w5, w1 = cc.get("ephemeral_5m_input_tokens") or 0, cc.get("ephemeral_1h_input_tokens") or 0
        uso["cw5"] += w5 + max(0, (u.get("cache_creation_input_tokens") or 0) - w5 - w1)  # non ventilé → 5 min
        uso["cw1h"] += w1
    dur = (datetime.fromisoformat(last.replace("Z", "+00:00")) - datetime.fromisoformat(first.replace("Z", "+00:00"))).total_seconds() if first else 0
    return {"uso": uso, "mensagens": len(seen), "modelos": sorted(modelos), "inicio": first, "fim": last, "duracao_relogio_s": dur}


def resumo(case_dir: Path, versoes: list[int]) -> dict:
    """Agrégat pour le tableau de bord."""
    case_dir = Path(case_dir)
    por_versao, uso, dur, custo_tot, nao_medidas = [], dict.fromkeys(CAMPOS, 0), 0.0, 0.0, []
    for v in sorted(versoes):
        p = case_dir / "versions" / f"v{v}" / "metricas.json"
        m = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"medido": False}
        if m.get("medido"):
            for k in CAMPOS:
                uso[k] += m["uso"].get(k) or 0
            dur += m.get("duracao_s") or 0
            custo_tot += custo_versao(m)
        else:
            nao_medidas.append(v)
        por_versao.append({"versao": v, **m})
    tent = tentativas(case_dir, por_versao)
    for a in tent:
        if a["falha"]:  # tentative sans version (échec, pause, interruption) : consommée quand même → comptée dans le cumul
            for k in CAMPOS:
                uso[k] += a["uso"].get(k) or 0
            custo_tot += a["custo"]
    sess = case_dir / "metricas" / "sessao_inicial.json"
    sessao = json.loads(sess.read_text(encoding="utf-8")) if sess.exists() else None
    if sessao:
        sessao["totais"] = totais(sessao["uso"])
        sessao["custo"] = custo(sessao["uso"], (sessao.get("modelos") or [""])[0])
    meta = case_dir / "dossie.json"
    return {"uso": uso, "totais": totais(uso), "custo": custo_tot, "duracao_s": dur, "nao_medidas": nao_medidas,
            "medidas": [v for v in sorted(versoes) if v not in nao_medidas], "por_versao": por_versao, "tentativas": tent,
            "falhas": sum(1 for a in tent if a["falha"]), "sessao": sessao,
            "dossie": json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}, "fonte_tarifas": FONTE_TARIFAS}


def duracao_propria(d: dict) -> int | None:
    """Durée de la tentative seule : valeur enregistrée, sinon fin − début (anciens enregistrements)."""
    if d.get("duracao_execucao_s") is not None:
        return int(d["duracao_execucao_s"])
    try:
        return max(0, round((datetime.fromisoformat(d["fim"]) - datetime.fromisoformat(d["inicio"])).total_seconds()))
    except (KeyError, TypeError, ValueError):
        return None


def tentativas(case_dir: Path, por_versao: list[dict]) -> list[dict]:
    """Toutes les tentatives de mise à jour mesurées, dans l'ordre chronologique : une par version mesurée, plus chaque
    exécution terminée sans version (échec, pause, interruption — même sans consommation), lue dans runs/<run>/status.json.
    Chaque élément : {"versao", "falha", "execucao", "inicio", "duracao_s", "uso", "custo"} (duracao_s : cette tentative seule). Une tentative échouée est rattachée à la
    version qui l'a reprise (retomada_de), sinon à la première version produite après elle, sinon à la prochaine."""
    case_dir = Path(case_dir)
    out, ligadas = [], set()
    for m in por_versao:
        if m.get("medido"):
            out.append({"versao": m["versao"], "falha": False, "execucao": m.get("execucao"), "inicio": m.get("inicio") or "",
                        "duracao_s": duracao_propria(m), "uso": m.get("uso") or {}, "custo": custo_versao(m)})
            ligadas.add(m.get("execucao"))
    retomadas = {m.get("retomada_de"): m["versao"] for m in por_versao if m.get("retomada_de")}
    ultima = max((m["versao"] for m in por_versao), default=0)
    d = case_dir / "runs"
    for sp in sorted(d.glob("*/status.json")) if d.is_dir() else []:
        rid = sp.parent.name
        if rid in ligadas:
            continue
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        uso = st.get("uso") or {}
        if st.get("estado") in ("em_curso", "simulado") or st.get("simulacao"):  # en cours : affichée à part (cerveau) ;
            # simulation : aucun appel à l'API, rien de consommé — ce n'est pas une tentative
            continue
        cu = st.get("custo_usd")
        ini = st.get("inicio") or ""
        seguinte = min((m["versao"] for m in por_versao if (m.get("inicio") or "") > ini), default=ultima + 1)
        out.append({"versao": retomadas.get(rid, seguinte), "falha": True, "execucao": rid, "inicio": st.get("inicio") or "",
                    "duracao_s": duracao_propria(st), "uso": uso, "custo": float(cu) if cu is not None else (custo(uso, st.get("modelo", "claude-opus-5-5")) or 0.0)})
    return sorted(out, key=lambda a: (a["inicio"] or "", a["versao"]))


def import_sessao(transcript: str, case_dir: str):
    case = Path(case_dir)
    r = ler_transcript(Path(transcript))
    r["nota"] = ("Sessão Claude Code inicial (análise v1/v2 do dossiê E desenvolvimento da aplicação web). "
                 "Não atribuível somente ao dossiê; mostrada a título informativo.")
    (case / "metricas").mkdir(exist_ok=True)
    (case / "metricas" / "sessao_inicial.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = case / "dossie.json"
    if not meta.exists():
        meta.write_text(json.dumps({"criado_em": r["inicio"], "fonte": "primeira mensagem da sessão de análise do dossiê"},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    for vd in (case / "versions").glob("v*"):
        mp = vd / "metricas.json"
        if not mp.exists():
            mp.write_text(json.dumps({"medido": False, "nota": "Produzida na sessão inicial; consumo não medido separadamente."},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: r[k] for k in ("uso", "mensagens", "modelos", "inicio", "fim")}, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1] == "import-sessao":
        import_sessao(sys.argv[2], sys.argv[3])
