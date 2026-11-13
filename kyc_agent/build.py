"""Génère les livrables d'une version de dossier à partir de state.json (déterministe, sans IA).

Usage : python -m kyc_agent.build <case_dir>
Produit dans <case_dir>/versions/v<N>/ :
  - <ID>_Relatorio_KYC_v<N>.pdf   parecer 1.1-1.8 + annexes (matrice externe, registre technique, screenings, catalogue, journal)
  - <ID>_Matriz_v<N>.xlsx         Matriz (4 colonnes) + Registro_tecnico + Controles + Screening + Partes + Journal
  - mensagens/M*_<Destinataire>.md
  - <ID>_Evidencias_v<N>.zip      01_Documentos_cliente … 07_Parecer_e_validacao + INDICE.csv/xlsx (guide v2 §10.3)
  - Nota_FR_v<N>.md, state_v<N>.json

Aucun contenu propre à un dossier n'est écrit dans ce fichier : tout vient de state.json et de work/.
Schéma complet : kyc_agent/prompts/schema_state.md. Champs facultatifs (repli neutre s'ils manquent) :
  caso.contraparte, caso.quem_paga_quem, caso.periodo_documentos, execucao_revisao.autorizacao_pendente,
  screening_fontes_texto, reconciliacao_identidade, midia_qualificacao, consultas_processuais,
  mapa_propriedade, fluxos_nota, arquivos_gerados {identidade, midia}, evidencias[].copiar.
Option : --out <dir> écrit la version ailleurs que dans versions/v<N>/ (tests de non-régression).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import sys
import unicodedata
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FONT_DIR = Path(__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("DV", str(FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DV-B", str(FONT_DIR / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFont(TTFont("DV-I", str(FONT_DIR / "DejaVuSans-Oblique.ttf")))
pdfmetrics.registerFontFamily("DV", normal="DV", bold="DV-B", italic="DV-I", boldItalic="DV-B")

NAVY = colors.HexColor("#1F3A5F")
LIGHT = colors.HexColor("#EEF2F7")
AMBER = colors.HexColor("#B26A00")
RED = colors.HexColor("#A61B1B")
GREEN = colors.HexColor("#2E7D32")
GREY = colors.HexColor("#666666")

S = {
    "body": ParagraphStyle("body", fontName="DV", fontSize=8.6, leading=11.4),
    "small": ParagraphStyle("small", fontName="DV", fontSize=7.4, leading=9.4),
    "cell": ParagraphStyle("cell", fontName="DV", fontSize=7.2, leading=9.0),
    "cellb": ParagraphStyle("cellb", fontName="DV-B", fontSize=7.2, leading=9.0, textColor=colors.white),
    "h1": ParagraphStyle("h1", fontName="DV-B", fontSize=15, leading=19, textColor=NAVY, spaceAfter=4),
    "h2": ParagraphStyle("h2", fontName="DV-B", fontSize=11, leading=14, textColor=NAVY, spaceBefore=8, spaceAfter=4),
    "h3": ParagraphStyle("h3", fontName="DV-B", fontSize=9, leading=12, textColor=NAVY, spaceBefore=4, spaceAfter=2),
    "status": ParagraphStyle("status", fontName="DV-B", fontSize=11, leading=14, textColor=colors.white, alignment=TA_CENTER),
}

STATUS_COLOR = {"KYC_CLEAR": GREEN, "KYC_CLEAR_DD": GREEN, "HOLD_TECNICO": AMBER, "HOLD_KYC_EDD": RED, "STOP": RED,
                "EM_ESPERA_EDD": RED}
LISTAS = ("OFAC", "ONU", "PEP", "CEIS", "CNEP")


def P(text, style="body"):
    t = escape(str(text if text is not None else "")).replace("\n", "<br/>")
    return Paragraph(t, S[style])


def table(header, rows, widths, zebra=True):
    data = [[P(h, "cellb") for h in header]] + [[c if isinstance(c, Paragraph) else P(c, "cell") for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B8C2CF")),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    if zebra:
        for i in range(2, len(data), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), LIGHT))
    t.setStyle(TableStyle(style))
    return t


def ascii_slug(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").upper()


def footer_factory(state, title):
    def _f(canv, doc):
        canv.saveState()
        w, h = doc.pagesize
        canv.setFont("DV", 6.8)
        canv.setFillColor(GREY)
        canv.drawString(15 * mm, h - 9 * mm, f"GF CAPITAL US | COMPLIANCE | CONFIDENCIAL – USO INTERNO | {title}")
        canv.drawRightString(w - 15 * mm, h - 9 * mm, f"{state['caso']['id']} | versão {state['versao']} | ref. {state.get('data_referencia', state['data_analise'])}")
        canv.drawString(15 * mm, 8 * mm, "Rascunho preparado por agente de IA — recomendação sujeita a revisão e autorização humanas (guia v2 §2.2).")
        canv.drawRightString(w - 15 * mm, 8 * mm, f"p. {doc.page}")
        canv.restoreState()
    return _f


def ev_name(state, eid):
    for e in state["evidencias"]:
        if e["id"] == eid:
            return e["arquivo"]
    return eid


def ev_refs(state, ids):
    return "; ".join(f"{i} ({ev_name(state, i)})" for i in ids) if ids else "—"


def party_name(state, pid):
    for p in state["partes"]:
        if p["id"] == pid:
            return p["nome"]
    return pid


# ------------------------------------------------------------------------------------------ rapport
def build_report(state, screening, out_pdf: Path):
    c = state["caso"]
    rec = state["recomendacao"]
    doc = SimpleDocTemplate(str(out_pdf), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=14 * mm, title=f"Relatório KYC {c['id']} v{state['versao']}",
                            author="Compliance GF Capital US (rascunho IA)")
    W = A4[0] - 30 * mm
    st = [P(f"Relatório de análise KYC / KYB / AML — {c['id']}", "h1"), P(c["nome"], "h3"),
          P(f"Versão {state['versao']} · data de referência {state.get('data_referencia', state['data_analise'])} · {state['elaborado_por']}", "small"),
          P(f"Referencial: {state.get('guia_referencia', '')}", "small"), Spacer(1, 4)]
    box = Table([[P(f"Recomendação proposta: {rec['status']}", "status")]], colWidths=[W])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), STATUS_COLOR.get(rec["codigo"], AMBER)),
                             ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    st += [box, Spacer(1, 2), P("Parecer de Compliance sobre KYC/KYB/AML: não vale aprovação da operação, do financiamento ou do desembolso "
                                "(guia v2 §1.4, §13). Status provisório até revisão e autorização humanas (seção 1.8).", "small")]

    # 1.1
    st.append(P("1.1 Síntese", "h2"))
    info = [["Negócio", f"{c['id']} — {c['nome']}"], ["Categoria / etapa", f"{c['categoria']} — {c['etapa_pipeline']}"],
            ["Contraparte", c.get("contraparte") or "; ".join(f"{p['nome']} ({p['tipo']})" for p in state["partes"][:2]) or "—"],
            ["Valor", f"{c.get('valor_declarado', '—')} · Pipeline: {c.get('valor_pipeline', '—')}"], ["Finalidade", c.get("finalidade", "—")],
            ["Quem paga quem", c.get("quem_paga_quem") or "ver 1.4 (fluxos)"],
            ["Prazo declarado", c.get("prazo_declarado", "—")],
            ["BDR", f"{(c.get('bdr') or {}).get('nome', '—')} ({(c.get('bdr') or {}).get('email', '—')})"],
            ["Prioridade / estado", f"{c.get('prioridade', '')} · {c.get('estado_tarefa', '')}"]]
    st.append(table(["Campo", "Informação"], info, [35 * mm, W - 35 * mm], zebra=False))
    st += [Spacer(1, 4), P(rec["resumo"]), P("Fundamentação:", "h3")]
    st += [P("• " + j) for j in rec["justificativa"]]
    st += [P("Caminho para conclusão: " + rec["caminho_para_validacao"]), P("Escalonamento: " + rec["escalonamento"])]

    # 1.2
    st.append(P("1.2 Partes (registro de partes e vínculos)", "h2"))
    rows = [[f"{p['id']}\n{p['nome']}", f"{p['tipo']}\n{p.get('cpf') or p.get('cnpj') or '—'}" + (f"\nNasc. {p['nascimento']}" if p.get("nascimento") else ""),
             "\n".join("• " + x for x in p["papeis"]), p.get("vinculo_fonte", ""), p["kyc"]] for p in state["partes"]]
    st.append(table(["Parte", "Identificação", "Papéis", "Fonte do vínculo", "Tratamento KYC"], rows,
                    [36 * mm, 27 * mm, 55 * mm, 30 * mm, W - 148 * mm]))

    # 1.3
    st.append(P("1.3 Documentos e controles", "h2"))
    data_cons = "/".join(reversed(state["data_analise"].split("-")))
    periodo = c.get("periodo_documentos") or "ver datas de recebimento no Anexo E"
    st.append(P(f"Período e fontes: documentos do pacote do Pipeline ({periodo} — Anexo E) e consultas independentes "
                f"executadas em {data_cons} (Anexo C e índice de evidências). Legenda: «documentado» = peça do cliente/terceiro; «executado pelo agente» = "
                "verificação independente neste ciclo; «não consultado» = controle não realizado (motivo e intervenção no registro técnico) — nunca "
                "equivale a resultado negativo. Estado da tarefa e resultado do controle são registrados separadamente (guia v2 §4.3, §15.4.2)."))
    rows = [[k["id"], k["nivel"], k["controle"], k["partes"], k["estado"], k["execucao"], f"{k['resultado']}\n→ {k['conclusao']}", ", ".join(k["evidencias"]) or "—"]
            for k in state["controles"]]
    st.append(table(["#", "Nível", "Controle", "Partes", "Estado", "Execução", "Resultado → conclusão", "Evid."], rows,
                    [12 * mm, 13 * mm, 20 * mm, 17 * mm, 18 * mm, 27 * mm, W - 122 * mm, 15 * mm]))
    st.append(Spacer(1, 3))
    st.append(P("Síntese dos screenings por parte (detalhe e resolução dos candidatos no Anexo C):", "h3"))
    st.append(screening_summary_table(screening, W))

    # 1.4
    st.append(P("1.4 Fluxos financeiros pertinentes", "h2"))
    rows = [[f["id"], f["pagador"], f["recebedor"], f["valor"], f["motivo"], f["base_contratual"], f["natureza"]] for f in state["fluxos"]]
    st.append(table(["#", "Pagador", "Recebedor", "Valor", "Motivo", "Base contratual", "Natureza da informação"], rows,
                    [8 * mm, 26 * mm, 32 * mm, 22 * mm, 29 * mm, 35 * mm, W - 152 * mm]))

    # 1.5
    st.append(P("1.5 Pontos já esclarecidos", "h2"))
    rows = [[x["id"], x["ponto"], x["conclusao"], x["natureza"], ", ".join(x["evidencias"])] for x in state["pontos_esclarecidos"]]
    st.append(table(["#", "Ponto", "Conclusão", "Natureza", "Evid."], rows, [8 * mm, 35 * mm, W - 98 * mm, 35 * mm, 20 * mm]))

    # 1.6
    st.append(P("1.6 Riscos e incoerências que subsistem (qualificação guia v2 §13.2)", "h2"))
    rows = [[f"{r['id']}\n{r['importancia'].upper()}\n{'BLOQUEIA KYC' if r['bloqueia_kyc'] else 'não bloqueia'}",
             f"{r['titulo']}\nEvento: {r['evento']}\nParte/fluxo: {r['parte_fluxo']}",
             r["fato_fonte"], r["exposicao"], f"Executado: {r['medida_executada']}\nProposto: {r['medida_proposta']}", r["risco_residual"]]
            for r in state["riscos"]]
    st.append(table(["#", "Risco", "Fato / fonte (fato · declaração · hipótese)", "Exposição", "Medidas", "Residual"], rows,
                    [18 * mm, 38 * mm, W - 136 * mm, 25 * mm, 38 * mm, 17 * mm]))

    # 1.7
    st.append(P("1.7 O que ainda deve ser recebido para concluir", "h2"))
    grupos = [("A) Condiciona o KYC — cliente / BDR", lambda p: p["condiciona"] == "KYC" and p["responsavel"] in ("Cliente (via BDR)", "BDR")),
              ("B) Condiciona o KYC — parecer do Jurídico", lambda p: p["condiciona"] == "KYC" and p["responsavel"] == "Jurídico"),
              ("C) Condiciona o KYC — decisão do CEO", lambda p: p["condiciona"] == "KYC" and p["responsavel"] == "CEO"),
              ("D) Pode ser tratado posteriormente na due diligence", lambda p: p["condiciona"] == "DD")]
    for title, f in grupos:
        rows = [[f"{p['id']}\n{p['prioridade']}", p["item"], p["esperado"], p["criterio_fechamento"], f"{p['responsavel']}\n{p['etapa_limite']}"]
                for p in state["pendencias"] if f(p)]
        if rows:
            st.append(P(title, "h3"))
            st.append(table(["#", "Item", "Complemento mínimo esperado", "Critério de fechamento", "Responsável / etapa"], rows,
                            [12 * mm, 35 * mm, W - 125 * mm, 45 * mm, 33 * mm]))
    st.append(P("E) Registro técnico interno — acessos bloqueados (não são dependências externas; detalhe no Anexo B)", "h3"))
    st.append(P("; ".join(f"{t['id']} {t['controle'].split(' — ')[0]} ({t['incidente'].split(' (')[0]})" for t in state.get("registro_tecnico", []))))
    cond = state.get("condicoes")
    if cond:
        st.append(P("F) Condições (guia v2 §13.3)", "h3"))
        rows = [["Já cumpridas", "\n".join("• " + x for x in cond["ja_cumpridas"])],
                ["Necessárias à conclusão KYC", "\n".join("• " + x for x in cond["necessarias_ao_kyc"])],
                ["Antes de assinatura / closing / desembolso", "\n".join("• " + x for x in cond["antes_assinatura_closing"])]]
        st.append(table(["Tipo", "Condições"], rows, [45 * mm, W - 45 * mm], zebra=False))

    # 1.8
    st.append(P("1.8 Revisão e autorização", "h2"))
    er = state.get("execucao_revisao", {})
    rows = [["Execução", er.get("executado_por") or "—", state["data_analise"]],
            ["Revisão (Supervisor de Compliance)", er.get("revisado_por") or "pendente", er.get("data_revisao") or "—"],
            ["Autorização / decisão competente", er.get("autorizado_por") or er.get("autorizacao_pendente") or "pendente", er.get("data_autorizacao") or "—"]]
    st.append(table(["Etapa", "Identidade", "Data"], rows, [55 * mm, W - 85 * mm, 30 * mm], zebra=False))
    st.append(P(er.get("nota", ""), "small"))

    # Annexes
    st.append(PageBreak())
    st.append(P("Anexo A — Matriz operacional (somente dependências externas)", "h2"))
    st.append(P("As leituras, reconciliações e consultas possíveis com os elementos e acessos disponíveis foram executadas neste ciclo."))
    rows = [[f"{p['id']} [{p['condiciona']} · {p['prioridade']}] {p['item']}", f"{p['esperado']}\nFechamento: {p['criterio_fechamento']}",
             f"{p['responsavel']}\n{p['etapa_limite']}", ev_refs(state, p["evidencias"])] for p in state["pendencias"]]
    st.append(table(["Ponto a resolver / risco", "Resposta, documento ou decisão esperada", "Responsável e etapa limite", "Evidências já examinadas"], rows,
                    [42 * mm, W - 137 * mm, 33 * mm, 62 * mm]))

    st.append(P("Anexo B — Registro técnico interno (acessos e incidentes — guia v2 §7.1.4, §12.4.2)", "h2"))
    rows = [[t["id"], t["controle"], t["incidente"], t["intervencao"], t["impacto"], t["prioridade"]] for t in state.get("registro_tecnico", [])]
    st.append(table(["#", "Controle", "Incidente", "Intervenção solicitada", "Impacto", "Prior."], rows,
                    [8 * mm, 38 * mm, 35 * mm, W - 129 * mm, 35 * mm, 13 * mm]))

    st.append(P("Anexo C — Screenings: detalhe por parte e por lista", "h2"))
    st.append(P(screening["metodo"], "small"))
    st.append(P(fontes_screening(state, screening), "small"))
    rows = []
    for r in screening["resultados"]:
        for lista in LISTAS:
            res = r["listas"][lista]
            cand = "\n".join(f"• {c.get('nome_lista') or c.get('Nome_PEP') or c.get('NOME DO SANCIONADO')} — {c.get('resolucao', {}).get('decisao', 'NÃO RESOLVIDO')}: "
                             f"{c.get('resolucao', {}).get('motivo', '')}" for c in res["candidatos"]) or "—"
            rows.append([r["id"], lista, res["termo_pesquisado"], res["resultado"], cand])
    st.append(table(["Parte", "Lista", "Termos pesquisados", "Resultado", "Candidatos e resolução"], rows,
                    [11 * mm, 12 * mm, 45 * mm, 32 * mm, W - 100 * mm]))

    st.append(P("Anexo D — Mensagens preparadas", "h2"))
    rows = [[m["id"], m["destinatario"], m["assunto"], ", ".join(m["pendencias"])] for m in state["mensagens"]]
    st.append(table(["#", "Destinatário", "Assunto", "Pendências cobertas"], rows, [10 * mm, 30 * mm, W - 80 * mm, 40 * mm]))

    st.append(P("Anexo E — Inventário dos documentos do dossiê", "h2"))
    rows = [[d["id"], d["tipo"], d["origem"], d.get("recebido_em", ""), d["uso"], d["achado"]] for d in state["documentos"]]
    st.append(table(["#", "Documento", "Origem", "Recebido", "Uso", "Achado"], rows, [9 * mm, 36 * mm, 15 * mm, 17 * mm, 22 * mm, W - 99 * mm]))

    st.append(P("Anexo F — Índice de evidências (pacote ZIP)", "h2"))
    rows = [[e["id"], e["categoria"], e["arquivo"], e["origem"], e["descricao"], e["sustenta"]] for e in state["evidencias"]]
    st.append(table(["#", "Pasta", "Arquivo", "Origem", "Descrição", "Sustenta"], rows, [9 * mm, 30 * mm, 55 * mm, 18 * mm, W - 140 * mm, 28 * mm]))

    st.append(P("Anexo G — Journal de atualização (guia v2 §12.4.3)", "h2"))
    for h in state["historico"]:
        st.append(P(f"Versão {h['versao']} — {h['data']} — {h['evento']}", "h3"))
        st += [P("• " + m, "small") for m in h["mudancas"]]
        if h.get("journal"):
            rows = [[j["ponto"], j["anterior"], j["revisado"], j["prova"], j["razao"], j["consequencia"]] for j in h["journal"]]
            st.append(table(["Ponto", "Resultado anterior", "Resultado revisado", "Prova", "Razão", "Consequência"], rows,
                            [25 * mm, 30 * mm, W - 135 * mm, 20 * mm, 25 * mm, 35 * mm]))

    f = footer_factory(state, "Parecer KYC/KYB/AML")
    doc.build(st, onFirstPage=f, onLaterPages=f)


def fontes_screening(state, screening):
    """Phrase « Fontes : … » de l'annexe C — texte du dossier s'il existe, sinon composée à partir de screening.json."""
    fr = screening["fontes"]
    if state.get("screening_fontes_texto"):
        txt = state["screening_fontes_texto"].format(**{f"{k}_{c}": v for k, d in fr.items() for c, v in d.items() if not isinstance(v, dict)})
    else:
        partes = []
        for lista in LISTAS:
            d = fr.get(lista) or {}
            data = d.get("data_geracao_lista") or d.get("data_arquivo") or d.get("competencia") or "baixada na data da execução"
            partes.append(f"{lista} {data}" + (f" ({d['registros']} registros)" if d.get("registros") else ""))
        txt = "Fontes: " + "; ".join(partes) + "."
    return f"{txt} Executado em {screening['executado_em']} (UTC)."


def screening_summary_table(screening, W):
    abbr = {"nenhuma correspondência identificada": "nenhuma",
            "nenhuma correspondência — candidato(s) descartado(s) por identificadores": "nenhuma (cand. descartados)",
            "não aplicável (pessoa jurídica)": "n/a (PJ)"}
    rows = []
    for r in screening["resultados"]:
        doc = r["cpf"] or r["cnpj"]
        rows.append([f"{r['id']} {r['nome']}", (doc or "sem identificador") + ("" if r["cpf_digito_valido"] is None else (" ✓" if r["cpf_digito_valido"] else " ✗")),
                     *[abbr.get(r["listas"][l]["resultado"], r["listas"][l]["resultado"]) for l in LISTAS]])
    return table(["Parte", "CPF/CNPJ (dígito)", *LISTAS], rows, [52 * mm, 30 * mm] + [(W - 82 * mm) / 5] * 5)


# ------------------------------------------------------------------------------------------ preuves générées
def simple_pdf(path: Path, state, title, blocks, pagesize=A4):
    doc = SimpleDocTemplate(str(path), pagesize=pagesize, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=14 * mm, title=title)
    f = footer_factory(state, title)
    doc.build([P(title, "h1")] + blocks, onFirstPage=f, onLaterPages=f)


def build_screening_pdfs(state, screening, outdir: Path):
    """Un justificatif par partie et par liste → [(path, sous-dossier)]"""
    W = A4[0] - 30 * mm
    files = []
    date = screening["executado_em"][:10]
    for r in screening["resultados"]:
        for lista in LISTAS:
            res = r["listas"][lista]
            src = screening["fontes"][lista]
            name = f"{state['caso']['id']}_SCREENING-{lista}_{ascii_slug(r['nome'])}_{date}.pdf"
            meta = [["Parte", f"{r['nome']} (id {r['id']}, {r['tipo']})"],
                    ["Identificador", (r["cpf"] or r["cnpj"] or "não disponível — pesquisa somente por nome") + ("" if r["cpf_digito_valido"] is None else f" (dígitos verificadores {'válidos' if r['cpf_digito_valido'] else 'inválidos'})")],
                    ["Motivo / papel", r["motivo"]], ["Lista", f"{lista} — cobertura: {src.get('cobertura', '')}"],
                    ["Termos pesquisados", res["termo_pesquisado"]], ["Fonte oficial", src["url"]],
                    ["Arquivo(s) e SHA-256", "\n".join(f"{k}: {v}" for k, v in src["arquivos"].items())],
                    ["Data / versão da lista", src.get("data_geracao_lista") or src.get("data_arquivo") or src.get("competencia") or "baixada na data da execução"],
                    ["Executado em (UTC)", r["executado_em"]], ["Método e parâmetros", screening["metodo"]],
                    ["Resultado (guia v2 §7.1.3)", res["resultado"].upper()]]
            blocks = [table(["Campo", "Valor"], meta, [40 * mm, W - 40 * mm], zebra=False), Spacer(1, 6)]
            if res["candidatos"]:
                blocks.append(P("Candidatos retornados e resolução documentada:", "h3"))
                for cnd in res["candidatos"]:
                    rz = cnd.get("resolucao")
                    shown = {k: v for k, v in cnd.items() if k not in ("resolucao", "chave")}
                    blocks.append(P(json.dumps(shown, ensure_ascii=False)[:800], "small"))
                    if rz:
                        txt = f"→ Decisão: {rz['decisao']} — {rz['motivo']} (autor: {rz['autor']}, {rz['data']}; revisão: {rz['revisao']})"
                    else:
                        txt = "→ NÃO RESOLVIDO — mantém correspondência possível"
                    blocks.append(P(txt, "body"))
                    blocks.append(Spacer(1, 3))
            concl = {"nenhuma correspondência identificada": "a pesquisa foi efetivamente executada e nenhuma entrada da lista corresponde aos termos/identificadores pesquisados.",
                     }.get(res["resultado"], "ver resolução dos candidatos acima.")
            blocks.append(P("Conclusão do agente: " + concl + " Conclusão limitada à base, aos termos, à versão e à data consultados.", "body"))
            p = outdir / name
            simple_pdf(p, state, f"Comprovante de screening — {lista}", blocks)
            files.append((p, lista))
    return files


def build_identity_pdf(state, screening, path: Path):
    """state["reconciliacao_identidade"] = {"colunas": [...], "pessoas": [{"nome", "linhas": [[...]]}], "observacoes": [...], "limites": [...]}
    Sans ce champ : tableau minimal tiré du registre des parties (PF)."""
    W = A4[0] - 30 * mm
    ri = state.get("reconciliacao_identidade")
    blocks = []
    if ri:
        hdr = ri["colunas"]
        larg = [22 * mm, 45 * mm, 30 * mm, 30 * mm, W - 127 * mm] if len(hdr) == 5 else [22 * mm] + [(W - 22 * mm) / (len(hdr) - 1)] * (len(hdr) - 1)
        for pessoa in ri["pessoas"]:
            blocks += [P(pessoa["nome"], "h3"), table(hdr, pessoa["linhas"], larg), Spacer(1, 6)]
    else:
        rows = [[p["nome"], p.get("cpf") or "—", p.get("nascimento") or "—", p.get("documento") or "—"] for p in state["partes"] if p.get("tipo") == "PF"]
        blocks += [P("Dados de identificação constantes do registro de partes (detalhe por documento não estruturado neste ciclo)", "h3"),
                   table(["Nome", "CPF", "Nascimento", "Documento"], rows, [60 * mm, 32 * mm, 25 * mm, W - 117 * mm]), Spacer(1, 6)]
    dv = [[r["id"], r["nome"], r["cpf"] or r["cnpj"] or "—", {True: "válido", False: "inválido", None: "n/a"}[r["cpf_digito_valido"]]] for r in screening["resultados"]]
    blocks += [P("Dígitos verificadores (algoritmo da Receita Federal) dos identificadores disponíveis", "h3"),
               table(["Parte", "Nome", "CPF/CNPJ", "Dígitos"], dv, [15 * mm, 80 * mm, 45 * mm, W - 140 * mm]), Spacer(1, 4)]
    blocks += [P(x) for x in (ri or {}).get("observacoes", [])]
    simple_pdf(path, state, "Reconciliação de identidade", blocks)


def build_media_pdf(state, media, path: Path):
    W = A4[0] - 30 * mm
    rows = [[b.get("data", ""), b["termo"], b["resultado"], b["conclusao"] + (f"\n{b['url']}" if b.get("url") else "")] for b in media["buscas"]]
    blocks = [P(f"Executado em: {media['executado_em']} · Ferramenta: {media['ferramenta']}"), P("Limitação: " + media["limitacao"]), Spacer(1, 4),
              table(["Data", "Termo pesquisado", "Resultado", "Conclusão"], rows, [22 * mm, 55 * mm, W - 125 * mm, 48 * mm]), Spacer(1, 4),
              P(state.get("midia_qualificacao") or media.get("qualificacao") or "Qualificação (guia v2 §7.6.2): ver a coluna «Conclusão» de cada busca.")]
    simple_pdf(path, state, "Memória de pesquisa — mídia adversa", blocks)


def build_datajud_pdf(state, cons: dict, dj_json: Path, path: Path):
    """Une consultation CNJ DataJud (réponse JSON brute) + son contexte state["consultas_processuais"][i] :
    {"arquivo", "gen", "rotulo", "motivo", "alternativa", "termo", "consultado_em", "leitura", "limite"}."""
    W = A4[0] - 30 * mm
    d = json.loads(dj_json.read_text(encoding="utf-8"))
    s = d["hits"]["hits"][0]["_source"]
    meta = [["Fonte", cons.get("fonte") or "CNJ — DataJud, API pública oficial (documentação datajud-wiki.cnj.jus.br)"],
            ["Motivo (gatilho)", cons.get("motivo", "—")]]
    if cons.get("alternativa"):
        meta.append(["Alternativa", cons["alternativa"]])
    meta += [["Termo", cons.get("termo") or f"numeroProcesso = {s['numeroProcesso']}"], ["Consultado em", cons.get("consultado_em", "—")],
             ["SHA-256 da resposta", hashlib.sha256(dj_json.read_bytes()).hexdigest()],
             ["Processo", f"{s['numeroProcesso']} — {s['classe']['nome']} (assunto: {', '.join(a['nome'] for a in s.get('assuntos', []))})"],
             ["Órgão", f"{s['orgaoJulgador']['nome']} — {s['sistema']['nome']} / {s['formato']['nome']} — ajuizado {s['dataAjuizamento'][:8]}"],
             ["Última atualização", s["dataHoraUltimaAtualizacao"]],
             ["Limite", cons.get("limite") or "O DataJud não informa partes, advogados nem o conteúdo das decisões."]]
    movs = sorted(s["movimentos"], key=lambda m: m["dataHora"])
    rows = [[m["dataHora"][:10], m["nome"], "; ".join(f"{c.get('descricao')}: {c.get('nome')}" for c in m.get("complementosTabelados", []))] for m in movs]
    blocks = [table(["Campo", "Valor"], meta, [35 * mm, W - 35 * mm], zebra=False), Spacer(1, 4)]
    if cons.get("leitura"):
        blocks += [P(cons["leitura"]), Spacer(1, 4)]
    blocks += [P("Movimentos (ordem cronológica)", "h3"), table(["Data", "Movimento", "Complemento"], rows, [22 * mm, 60 * mm, W - 82 * mm])]
    simple_pdf(path, state, "Consulta processual — CNJ DataJud", blocks)


def build_parties_pdf(state, path: Path):
    W = A4[0] - 30 * mm
    rows = [[p["id"], p["nome"], f"{p['tipo']} · {p.get('cpf') or p.get('cnpj') or '—'}", "\n".join("• " + x for x in p["papeis"]), p.get("vinculo_fonte", ""), p["kyc"]] for p in state["partes"]]
    blocks = ([P(state["mapa_propriedade"]), Spacer(1, 4)] if state.get("mapa_propriedade") else []) + [
              table(["#", "Parte", "Tipo / identificador", "Papéis", "Fonte do vínculo", "Tratamento KYC"], rows,
                    [10 * mm, 35 * mm, 28 * mm, 52 * mm, 30 * mm, W - 155 * mm])]
    simple_pdf(path, state, "Registro de partes e cadeia de propriedade", blocks)


def build_flows_pdf(state, path: Path):
    W = A4[0] - 30 * mm
    rows = [[f["id"], f["pagador"], f["recebedor"], f["valor"], f["motivo"], f["base_contratual"], f["natureza"]] for f in state["fluxos"]]
    blocks = [P(state.get("fluxos_nota") or "Matriz de fluxos (guia v2 §8.3.1)."),
              table(["#", "Pagador", "Recebedor", "Valor", "Motivo", "Base", "Natureza"], rows, [8 * mm, 28 * mm, 32 * mm, 22 * mm, 28 * mm, 35 * mm, W - 153 * mm])]
    simple_pdf(path, state, "Matriz de fluxos AML", blocks)


# ------------------------------------------------------------------------------------------ matrice / messages / zip
def _sheet(wb, title, hdr, rows, widths, first=False):
    ws = wb.active if first else wb.create_sheet(title)
    ws.title = title
    ws.append(hdr)
    for r in rows:
        ws.append(r)
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
    for row in ws.iter_rows(min_row=1):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"


def build_matrix_xlsx(state, screening, path: Path):
    wb = Workbook()
    _sheet(wb, "Matriz", ["Ponto a resolver / risco", "Resposta, documento ou decisão esperada", "Responsável e etapa limite", "Referência às evidências já examinadas",
                          "ID", "Condiciona", "Prioridade", "Critério de fechamento", "Estado da tarefa", "Status"],
           [[p["item"], p["esperado"], f"{p['responsavel']} — {p['etapa_limite']}", ev_refs(state, p["evidencias"]), p["id"], p["condiciona"],
             p["prioridade"], p["criterio_fechamento"], p["estado_tarefa"], p["status"]] for p in state["pendencias"]],
           [40, 65, 30, 55, 6, 10, 9, 40, 22, 8], first=True)
    _sheet(wb, "Registro_tecnico", ["ID", "Controle", "Incidente", "Intervenção solicitada", "Impacto", "Prioridade", "Evidências"],
           [[t["id"], t["controle"], t["incidente"], t["intervencao"], t["impacto"], t["prioridade"], ", ".join(t["evidencias"])] for t in state.get("registro_tecnico", [])],
           [6, 45, 40, 60, 35, 9, 12])
    _sheet(wb, "Controles_C01-C42", ["ID", "Nível", "Controle", "Partes", "Estado da tarefa", "Execução", "Resultado", "Conclusão", "Evidências"],
           [[k["id"], k["nivel"], k["controle"], k["partes"], k["estado"], k["execucao"], k["resultado"], k["conclusao"], ", ".join(k["evidencias"])] for k in state["controles"]],
           [10, 14, 30, 22, 22, 40, 60, 45, 14])
    rows = []
    for r in screening["resultados"]:
        for l in LISTAS:
            res = r["listas"][l]
            rows.append([r["id"], r["nome"], r["cpf"] or r["cnpj"], l, res["termo_pesquisado"], res["resultado"], len(res["candidatos"]),
                         " | ".join(f"{c.get('nome_lista') or c.get('Nome_PEP') or c.get('NOME DO SANCIONADO')}: {c.get('resolucao', {}).get('decisao', 'NÃO RESOLVIDO')}" for c in res["candidatos"]),
                         r["executado_em"]])
    _sheet(wb, "Screening_por_parte", ["Parte", "Nome", "CPF/CNPJ", "Lista", "Termos", "Resultado", "Nº candidatos", "Resolução", "Executado (UTC)"],
           rows, [6, 38, 18, 7, 45, 40, 8, 60, 20])
    _sheet(wb, "Partes", ["ID", "Nome", "Tipo", "Identificador", "Papéis", "Fonte do vínculo", "Tratamento KYC"],
           [[p["id"], p["nome"], p["tipo"], p.get("cpf") or p.get("cnpj") or "", " | ".join(p["papeis"]), p.get("vinculo_fonte", ""), p["kyc"]] for p in state["partes"]],
           [6, 40, 10, 22, 60, 30, 45])
    jr = []
    for h in state["historico"]:
        for j in h.get("journal", []):
            jr.append([h["versao"], h["data"], j["ponto"], j["anterior"], j["revisado"], j["prova"], j["razao"], j["consequencia"]])
    _sheet(wb, "Journal", ["Versão", "Data", "Ponto", "Resultado anterior", "Resultado revisado", "Prova", "Razão", "Consequência"], jr, [7, 11, 22, 40, 45, 14, 25, 30])
    wb.save(path)


def build_messages(state, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    out = []
    for m in state["mensagens"]:
        txt = (f"# {m['id']} — {m['destinatario']}\n\n**Para:** {m['para']}\n\n**Assunto:** {m['assunto']}\n\n"
               f"**Anexos:** {'; '.join(m['anexos'])}\n\n**Pendências cobertas:** {', '.join(m['pendencias'])}\n\n---\n\n{m['corpo']}\n")
        p = outdir / f"{m['id']}_{m['destinatario']}.md"
        p.write_text(txt, encoding="utf-8")
        out.append(p)
    return out


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build_zip(state, case_dir: Path, gen: dict, zpath: Path):
    """gen: {'gen:xxx': [Path | (Path, sous-dossier)]}"""
    index = []
    root_dir = case_dir.parents[1]
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        root = f"{state['caso']['id']}_Evidencias_v{state['versao']}"
        for e in state["evidencias"]:
            gen_src = e["fonte"].startswith("gen:")
            srcs = gen[e["fonte"]] if gen_src else [Path(p) for p in e["fonte"].split("|")]
            for i, s in enumerate(srcs):
                sub = ""
                if isinstance(s, tuple):
                    s, sub = s
                s = s if s.is_absolute() else (root_dir / s)
                arc_name = s.name if (gen_src or len(srcs) > 1) else e["arquivo"]
                arc = "/".join(x for x in (root, e["categoria"], sub, arc_name) if x)
                z.write(s, arc)
                index.append({"id": e["id"] + (f".{i+1:02d}" if len(srcs) > 1 else ""), "pasta": e["categoria"] + (f"/{sub}" if sub else ""),
                              "arquivo_no_zip": arc, "origem": e["origem"], "descricao": e["descricao"], "sustenta": e["sustenta"],
                              "fonte_original": s.relative_to(root_dir).as_posix() if s.is_relative_to(root_dir) else str(s), "sha256": sha(s)})
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(index[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(index)
        z.writestr(f"{root}/INDICE.csv", "﻿" + buf.getvalue())
        wb = Workbook()
        ws = wb.active
        ws.title = "Indice"
        ws.append(list(index[0].keys()))
        for r in index:
            ws.append(list(r.values()))
        for col, wdt in zip("ABCDEFGH", [8, 30, 75, 18, 45, 22, 60, 66]):
            ws.column_dimensions[col].width = wdt
        xb = io.BytesIO()
        wb.save(xb)
        z.writestr(f"{root}/INDICE.xlsx", xb.getvalue())
    return index


def main(case_dir: str, out_dir: str | None = None):
    case = Path(case_dir).resolve()
    work = case / "work"
    state = json.loads((case / "state.json").read_text(encoding="utf-8"))
    sp = work / "screening.json"
    if not sp.exists():
        raise SystemExit(f"{sp} manquant : exécuter le screening (python -m kyc_agent.screening …) avant le build.")
    screening = json.loads(sp.read_text(encoding="utf-8"))
    mp = work / "media_log.json"
    media = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else None
    v = state["versao"]
    cid = state["caso"]["id"]
    d = state.get("data_referencia", state["data_analise"])
    out = Path(out_dir).resolve() if out_dir else case / "versions" / f"v{v}"
    if out.exists():
        shutil.rmtree(out)
    gen_dir = out / "_gerados"
    (gen_dir / "screening").mkdir(parents=True)
    nomes = state.get("arquivos_gerados") or {}

    ident = gen_dir / f"{cid}_RECONCILIACAO-IDENTIDADE_{nomes.get('identidade') or 'PARTES'}_{d}.pdf"
    build_identity_pdf(state, screening, ident)
    scr = build_screening_pdfs(state, screening, gen_dir / "screening")
    gen = {"gen:identidade": [ident], "gen:screening": scr}
    if media:
        med = gen_dir / f"{cid}_MIDIA-ADVERSA-LOG_{nomes.get('midia') or str(len(state['partes'])) + '-PARTES'}_{d}.pdf"
        build_media_pdf(state, media, med)
        gen["gen:midia"] = [med]

    for i, cons in enumerate(state.get("consultas_processuais", [])):
        dj = case / cons["arquivo"]
        if not dj.exists():
            raise SystemExit(f"Consulta processual sem arquivo: {dj}")
        rot = cons.get("rotulo") or dj.stem
        djp = gen_dir / f"{cid}_DATAJUD-PROCESSO-{rot}_{d}.pdf"
        build_datajud_pdf(state, cons, dj, djp)
        djc = gen_dir / f"{cid}_DATAJUD-PROCESSO-{rot}_{d}.json"
        shutil.copy(dj, djc)
        gen[cons.get("gen") or f"gen:datajud{i + 1}"] = [djp, djc]
    sls = work / "web" / "ofac_sls"
    if sls.exists():
        files = []
        for pdf in sorted(sls.glob("OFAC-SLS_*.pdf")):
            dst = gen_dir / f"{cid}_{pdf.stem}_{d}.pdf"
            shutil.copy(pdf, dst)
            files.append(dst)
        if (sls / "ofac_sls_log.json").exists():
            files.append(sls / "ofac_sls_log.json")
        gen["gen:ofac_sls"] = files
    parts = gen_dir / f"{cid}_REGISTRO-PARTES-E-CADEIA-PROPRIEDADE_{d}.pdf"
    build_parties_pdf(state, parts)
    flows = gen_dir / f"{cid}_MATRIZ-FLUXOS-AML_{d}.pdf"
    build_flows_pdf(state, flows)
    gen["gen:partes"] = [parts]
    gen["gen:fluxos"] = [flows]

    rel = out / f"{cid}_Relatorio_KYC_v{v}.pdf"
    build_report(state, screening, rel)
    mx = out / f"{cid}_Matriz_v{v}.xlsx"
    build_matrix_xlsx(state, screening, mx)
    msgs = build_messages(state, out / "mensagens")
    gen["gen:matriz"] = [mx] + [(m, "mensagens") for m in msgs]
    gen["gen:relatorio"] = [rel]
    # evidencias[].copiar : le fichier source est copié sous son nom normalisé (e["arquivo"]) avant d'entrer dans le ZIP
    for e in state["evidencias"]:
        if e.get("copiar") and not e["fonte"].startswith("gen:"):
            dst = gen_dir / e["arquivo"]
            shutil.copy(case.parents[1] / e["fonte"], dst)
            gen[f"gen:copia_{e['id']}"] = [dst]
            e["fonte"] = f"gen:copia_{e['id']}"
    faltam = sorted({e["fonte"] for e in state["evidencias"] if e["fonte"].startswith("gen:") and e["fonte"] not in gen})
    if faltam:
        raise SystemExit(f"Evidências geradas sem fonte neste ciclo: {', '.join(faltam)}")
    idx = build_zip(state, case, gen, out / f"{cid}_Evidencias_v{v}.zip")
    if state.get("nota_fr"):
        (out / f"Nota_FR_v{v}.md").write_text(state["nota_fr"], encoding="utf-8")
    (out / f"state_v{v}.json").write_text((case / "state.json").read_text(encoding="utf-8"), encoding="utf-8")
    shutil.rmtree(gen_dir)
    print(f"OK -> {out}  ({len(idx)} fichiers dans le ZIP)")
    for p in sorted(out.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(out)}  {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    args = sys.argv[1:]
    out_arg = None
    if "--out" in args:
        k = args.index("--out")
        out_arg = args[k + 1]
        del args[k:k + 2]
    main(args[0], out_arg)
