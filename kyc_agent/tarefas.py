"""Liste des tâches humaines en cours d'un dossier (onglet « Tarefas »), dérivée du state.

Une seule source pour l'app web et l'export partageable :
1. mensagens a enviar (M1…), 2. verificações manuais (registro técnico, ação Compliance),
3. devolver ao agente, 4. validação humana (revisão / autorização).
`lang` ne traduit que les phrases fixes de ce module ; passer un state déjà traduit pour le reste.
"""
from __future__ import annotations

PRIO_ORDEM = {"P1": 1, "P2": 2, "P3": 3}

TXT = {
    "pt": {"quem_msg": "Compliance (revisão e envio)", "tit_msg": "Revisar e enviar {id} — {dest}",
           "det_msg": "Para: {para}\nAssunto: {assunto}", "ret_msg": "Devolver ao agente a resposta recebida e os documentos anexados.",
           "g_enviar": "Mensagens a enviar",
           "i_enviar": "Rascunhos prontos. Nada é enviado pela aplicação: relê, ajusta e envia pelo canal autorizado.",
           "g_verif": "Verificações manuais a realizar",
           "i_verif": "Controles que o agente não pôde executar (captcha não contornado, base não fornecida).",
           "motivo": "Motivo", "g_devolver": "Devolver ao agente", "t_devolver": "Entregar ao agente as respostas e os resultados",
           "d_devolver": "Colar as respostas recebidas ({msgs}) e depositar os arquivos das verificações manuais. O agente "
                         "atualiza o mesmo dossiê na versão {nv}, fecha as pendências resolvidas e explica as mudanças.",
           "q_sup": "Compliance Supervisor", "t_rev": "Revisar o relatório e a recomendação",
           "d_rev": "Recomendação atual: {st}. Registrar identidade e data da revisão (seção 1.8).",
           "q_aut": "Autoridade de validação", "t_aut": "Autorizar (ou não) a conclusão KYC",
           "d_aut": "A decisão final é humana; o agente só prepara a recomendação.", "g_valid": "Validação humana"},
    "fr": {"quem_msg": "Compliance (revue et envoi)", "tit_msg": "Relire et envoyer {id} — {dest}",
           "det_msg": "À : {para}\nObjet : {assunto}", "ret_msg": "Renvoyer à l'agent la réponse reçue et les documents joints.",
           "g_enviar": "Messages à envoyer",
           "i_enviar": "Brouillons prêts. Rien n'est envoyé par l'application : relire, ajuster et envoyer par le canal "
                       "autorisé (le texte à envoyer est l'original en portugais).",
           "g_verif": "Vérifications manuelles à réaliser",
           "i_verif": "Contrôles que l'agent n'a pas pu exécuter (captcha non contourné, base non fournie).",
           "motivo": "Motif", "g_devolver": "Renvoyer à l'agent", "t_devolver": "Remettre à l'agent les réponses et les résultats",
           "d_devolver": "Coller les réponses reçues ({msgs}) et déposer les fichiers des vérifications manuelles. L'agent met "
                         "à jour le même dossier en version {nv}, clôt les pendências résolues et explique les changements.",
           "q_sup": "Superviseur Compliance", "t_rev": "Relire le rapport et la recommandation",
           "d_rev": "Recommandation actuelle : {st}. Enregistrer l'identité et la date de la revue (section 1.8).",
           "q_aut": "Autorité de validation", "t_aut": "Autoriser (ou non) la conclusion KYC",
           "d_aut": "La décision finale est humaine ; l'agent prépare seulement la recommandation.", "g_valid": "Validation humaine"},
}


def _prio_msg(m: dict, pend: dict) -> str:
    ps = [pend[p].get("prioridade") for p in m.get("pendencias", []) if p in pend and pend[p].get("prioridade")]
    return min(ps, key=lambda p: PRIO_ORDEM.get(p, 9)) if ps else "P2"


def tarefas(state: dict, lang: str = "pt") -> list[dict]:
    L = TXT.get(lang, TXT["pt"])
    pend = {p["id"]: p for p in state.get("pendencias", [])}
    grupos = []

    msgs = []
    for m in state.get("mensagens", []):
        msgs.append({
            "id": m["id"], "prioridade": _prio_msg(m, pend), "quem": L["quem_msg"],
            "titulo": L["tit_msg"].format(id=m["id"], dest=m["destinatario"]),
            "detalhe": L["det_msg"].format(para=m["para"], assunto=m["assunto"]),
            "chips": m.get("pendencias", []), "aba": "mensagens", "alvo": m["id"], "retorno": L["ret_msg"],
        })
    msgs.sort(key=lambda t: (PRIO_ORDEM.get(t["prioridade"], 9), t["id"]))
    grupos.append({"id": "enviar", "titulo": L["g_enviar"], "intro": L["i_enviar"], "itens": msgs})

    ver = []
    for t in state.get("registro_tecnico", []):
        if not t.get("acao_humana"):
            continue
        ver.append({"id": t["id"], "prioridade": t["prioridade"], "quem": "Compliance",
                    "titulo": t["controle"], "detalhe": f"{t['intervencao']}\n{L['motivo']}: {t['incidente']}",
                    "link": t.get("link", ""), "retorno": t.get("retorno_esperado", ""), "chips": t.get("evidencias", []),
                    "aba": "verificacoes", "alvo": t["id"]})
    ver.sort(key=lambda t: (PRIO_ORDEM.get(t["prioridade"], 9), t["id"]))
    if ver:
        grupos.append({"id": "verificar", "titulo": L["g_verif"], "intro": L["i_verif"], "itens": ver})

    grupos.append({"id": "devolver", "titulo": L["g_devolver"], "intro": "", "itens": [{
        "id": "R", "prioridade": "P1", "quem": "Compliance", "titulo": L["t_devolver"],
        "detalhe": L["d_devolver"].format(msgs=", ".join(m["id"] for m in state.get("mensagens", [])),
                                          nv=int(state.get("versao", 0)) + 1),
        "retorno": "", "chips": []}]})

    er = state.get("execucao_revisao") or {}
    val = []
    if not er.get("revisado_por"):
        val.append({"id": "V1", "prioridade": "P2", "quem": L["q_sup"], "titulo": L["t_rev"],
                    "detalhe": L["d_rev"].format(st=state["recomendacao"]["status"]),
                    "aba": "relatorio", "alvo": "revisao", "retorno": "", "chips": []})
    if not er.get("autorizado_por"):
        val.append({"id": "V2", "prioridade": "P2", "quem": L["q_aut"], "titulo": L["t_aut"], "detalhe": L["d_aut"],
                    "aba": "relatorio", "alvo": "revisao", "retorno": "", "chips": []})
    if val:
        grupos.append({"id": "validar", "titulo": L["g_valid"], "intro": "", "itens": val})
    return grupos


def n_tarefas(state: dict) -> int:
    return sum(len(g["itens"]) for g in tarefas(state))
