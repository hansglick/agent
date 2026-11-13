"""Filtrage (screening) d'une liste de personnes contre les listes officielles téléchargées.

Sources gérées (fichiers officiels, téléchargés par fetch_sources) :
  - OFAC SDN + Consolidated (SDN.CSV, ALT.CSV, CONS_PRIM.CSV, CONS_ALT.CSV)
  - ONU  Consolidated List (consolidated.xml)
  - CGU  PEP (AAAAMM_PEP.csv)            — CPF partiellement masqué dans la source
  - CGU  CEIS / CNEP (AAAAMMJJ_*.csv)     — CPF/CNPJ en clair

Usage :
  python -m kyc_agent.screening <sources_dir> <subjects.json> <out.json> [resolucoes.json]

subjects.json : [{"id": "JBM", "nome": "...", "cpf": "38086751015", "nascimento": "1962-10-29", "motivo": "..."}]

Règles (guide §4.6-4.8, §7) : chaque résultat = "sem correspondência" | "correspondência potencial" |
"correspondência confirmada" | "inconclusivo". Une correspondance approchée n'est JAMAIS confirmée
automatiquement : elle est listée pour résolution humaine/agent avec les identifiants disponibles.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

FUZZY_THRESHOLD = 0.88  # similarité token-set minimale pour signaler une correspondance potentielle


# ---------------------------------------------------------------- normalisation
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s).upper()
    return re.sub(r"\s+", " ", s).strip()


PARTICLES = {"DE", "DA", "DO", "DOS", "DAS", "E"}


def tokens(s: str) -> list[str]:
    return [t for t in norm(s).split() if t not in PARTICLES]


def similarity(a: str, b: str) -> float:
    """Similarité token-set (ordre des tokens indifférent, particules ignorées).
    Méthode fixée AVANT exécution (guide v2 §7.2.2) : aucun ajustement a posteriori pour supprimer
    un candidat ; les candidats sont conservés et résolus par identifiants (resolucoes.json)."""
    ta, tb = sorted(tokens(a)), sorted(tokens(b))
    if not ta or not tb:
        return 0.0
    return difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def cpf_valid(cpf: str) -> bool:
    d = digits(cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------- loaders -> [(name, extra_dict)]
def load_ofac(src: Path):
    rows = []
    for prim, alt, label in (("ofac_sdn.csv", "ofac_alt.csv", "SDN"), ("ofac_cons_prim.csv", "ofac_cons_alt.csv", "CONS")):
        names = {}
        with open(src / prim, encoding="latin-1", newline="") as f:
            for r in csv.reader(f):
                if len(r) > 3:
                    names[r[0]] = {"ent_num": r[0], "lista": label, "tipo": r[2].strip(), "programa": r[3].strip(), "nome_lista": r[1]}
                    rows.append((r[1], names[r[0]]))
        with open(src / alt, encoding="latin-1", newline="") as f:
            for r in csv.reader(f):
                if len(r) > 3 and r[0] in names:
                    rows.append((r[3], {**names[r[0]], "alias": r[3]}))
    return rows


def load_un(src: Path):
    rows = []
    root = ET.parse(src / "un_consolidated.xml").getroot()
    for ind in root.iter("INDIVIDUAL"):
        parts = [ind.findtext(t) or "" for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
        meta = {"ref": ind.findtext("REFERENCE_NUMBER"), "lista": "UN-IND"}
        rows.append((" ".join(p for p in parts if p), meta))
        for al in ind.iter("INDIVIDUAL_ALIAS"):
            if al.findtext("ALIAS_NAME"):
                rows.append((al.findtext("ALIAS_NAME"), {**meta, "alias": al.findtext("ALIAS_NAME")}))
    for ent in root.iter("ENTITY"):
        meta = {"ref": ent.findtext("REFERENCE_NUMBER"), "lista": "UN-ENT"}
        rows.append((ent.findtext("FIRST_NAME") or "", meta))
    return rows, root.get("dateGenerated")


def load_cgu(path: Path):
    with open(path, encoding="latin-1", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


# ---------------------------------------------------------------- matching
def name_hits(subject_name: str, rows, key=lambda r: r[0]):
    out = []
    sn = norm(subject_name)
    st = set(tokens(subject_name))
    for r in rows:
        cand = key(r)
        if not cand:
            continue
        cn = norm(cand)
        if cn == sn:
            out.append((1.0, r))
            continue
        ct = set(tokens(cand))
        # préfiltre rapide : au moins 2 tokens en commun
        if len(st & ct) < 2:
            continue
        s = similarity(subject_name, cand)
        if s >= FUZZY_THRESHOLD:
            out.append((round(s, 3), r))
    return sorted(out, key=lambda x: -x[0])


R_NONE = "nenhuma correspondência identificada"
R_POSS = "correspondência possível"
R_CONF = "correspondência confirmada"
R_DISC = "nenhuma correspondência — candidato(s) descartado(s) por identificadores"


def cand_key(lista: str, c: dict) -> str:
    """Clé stable d'un candidat, pour rattacher une résolution documentée."""
    for k in ("ent_num", "ref"):
        if c.get(k):
            return f"{lista}:{c[k]}:{norm(c.get('nome_lista', ''))}"
    nm = c.get("Nome_PEP") or c.get("NOME DO SANCIONADO") or c.get("nome_lista", "")
    doc = c.get("CPF") or c.get("CPF OU CNPJ DO SANCIONADO") or ""
    return f"{lista}:{norm(nm)}:{digits(doc)}"


def finalize(lista, res, resol):
    """Applique les résolutions documentées ; un candidat non résolu laisse 'correspondência possível'."""
    if not res["candidatos"]:
        res["resultado"] = R_NONE
        return
    pend = False
    for c in res["candidatos"]:
        k = cand_key(lista, c)
        c["chave"] = k
        r = resol.get(k)
        if r:
            c["resolucao"] = r
        else:
            pend = True
    if any(c.get("tipo") in ("CPF idêntico", "CNPJ idêntico") for c in res["candidatos"]):
        res["resultado"] = R_CONF + " (identificador idêntico)"
    elif pend:
        res["resultado"] = R_POSS
    elif all(c["resolucao"]["decisao"] == "descartada" for c in res["candidatos"]):
        res["resultado"] = R_DISC
    else:
        res["resultado"] = R_POSS


def screen(src_dir: str, subjects_path: str, out_path: str, resol_path: str | None = None):
    src = Path(src_dir)
    subjects = json.loads(Path(subjects_path).read_text(encoding="utf-8"))
    resol = json.loads(Path(resol_path).read_text(encoding="utf-8")) if resol_path and Path(resol_path).exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    pep_file = sorted(src.glob("unz/*_PEP.csv"))[-1]
    ceis_file = sorted(src.glob("unz/*_CEIS.csv"))[-1]
    cnep_file = sorted(src.glob("unz/*_CNEP.csv"))[-1]

    ofac = load_ofac(src)
    un, un_date = load_un(src)
    pep = load_cgu(pep_file)
    ceis = load_cgu(ceis_file)
    cnep = load_cgu(cnep_file)

    sources = {
        "OFAC": {"arquivos": {p: sha256(src / p) for p in ("ofac_sdn.csv", "ofac_alt.csv", "ofac_cons_prim.csv", "ofac_cons_alt.csv")},
                 "url": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/", "registros": len(ofac),
                 "cobertura": "SDN + Consolidated (non-SDN): nomes primários e aliases"},
        "ONU": {"arquivos": {"un_consolidated.xml": sha256(src / "un_consolidated.xml")}, "data_geracao_lista": un_date,
                "url": "https://scsanctions.un.org/resources/xml/en/consolidated.xml", "registros": len(un),
                "cobertura": "Lista consolidada do CSNU: indivíduos, entidades e aliases"},
        "PEP": {"arquivos": {pep_file.name: sha256(pep_file)}, "competencia": pep_file.name[:6],
                "url": "https://portaldatransparencia.gov.br/download-de-dados/pep", "registros": len(pep),
                "cobertura": "PEP brasileiras (CGU); não cobre PEP estrangeiras nem familiares/estreitos colaboradores"},
        "CEIS": {"arquivos": {ceis_file.name: sha256(ceis_file)}, "data_arquivo": ceis_file.name[:8],
                 "url": "https://portaldatransparencia.gov.br/download-de-dados/ceis", "registros": len(ceis), "cobertura": "PF e PJ"},
        "CNEP": {"arquivos": {cnep_file.name: sha256(cnep_file)}, "data_arquivo": cnep_file.name[:8],
                 "url": "https://portaldatransparencia.gov.br/download-de-dados/cnep", "registros": len(cnep), "cobertura": "PJ (Lei 12.846/2013)"},
    }

    results = []
    for s in subjects:
        doc = digits(s.get("cpf") or s.get("cnpj") or "")
        is_pj = bool(s.get("cnpj"))
        nomes = [s["nome"]] + s.get("variantes", [])
        r = {"id": s["id"], "nome": s["nome"], "variantes": s.get("variantes", []), "tipo": "PJ" if is_pj else "PF",
             "cpf": "" if is_pj else doc, "cnpj": doc if is_pj else "",
             "cpf_digito_valido": (cpf_valid(doc) if doc else None) if not is_pj else None,
             "motivo": s.get("motivo", ""), "executado_em": now, "listas": {}}

        for label, rows in (("OFAC", ofac), ("ONU", un)):
            seen, cands = set(), []
            for nm in nomes:
                for sc, h in name_hits(nm, rows):
                    k = (h[0], h[1].get("ent_num") or h[1].get("ref"))
                    if k not in seen:
                        seen.add(k)
                        cands.append({"score": sc, "termo": nm, "nome_lista": h[0], **h[1]})
            res = {"termo_pesquisado": " | ".join(nomes), "candidatos": sorted(cands, key=lambda c: -c["score"])[:15]}
            finalize(label, res, resol)
            r["listas"][label] = res

        if is_pj:
            r["listas"]["PEP"] = {"termo_pesquisado": "—", "resultado": "não aplicável (pessoa jurídica)", "candidatos": []}
        else:
            pep_hits = []
            mid = doc[3:9] if len(doc) == 11 else None
            for row in pep:
                nm = row.get("Nome_PEP", "")
                masked = digits(row.get("CPF", ""))
                cpf_ok = bool(mid) and masked == mid
                sc = max((1.0 if norm(nm) == norm(n) else (similarity(n, nm) if len(set(tokens(nm)) & set(tokens(n))) >= 2 else 0)) for n in nomes)
                if cpf_ok or sc >= FUZZY_THRESHOLD:
                    pep_hits.append({"score_nome": round(sc, 3), "cpf_mascarado_coincide": cpf_ok, **row})
            res = {"termo_pesquisado": f"{' | '.join(nomes)} / CPF centro {mid or 'n/d'}", "candidatos": pep_hits[:15]}
            finalize("PEP", res, resol)
            r["listas"]["PEP"] = res

        for label, rows in (("CEIS", ceis), ("CNEP", cnep)):
            hits = []
            for row in rows:
                d = digits(row.get("CPF OU CNPJ DO SANCIONADO", ""))
                nm = row.get("NOME DO SANCIONADO", "")
                if doc and d == doc:
                    hits.append({"tipo": "CNPJ idêntico" if is_pj else "CPF idêntico", **{k: row[k] for k in list(row)[:12]}})
                elif any(norm(nm) == norm(n) or (len(set(tokens(nm)) & set(tokens(n))) >= 2 and similarity(n, nm) >= FUZZY_THRESHOLD) for n in nomes):
                    hits.append({"tipo": "nome semelhante", **{k: row[k] for k in list(row)[:12]}})
            res = {"termo_pesquisado": f"{' | '.join(nomes)} / {'CNPJ' if is_pj else 'CPF'} {doc or 'n/d'}", "candidatos": hits[:15]}
            finalize(label, res, resol)
            r["listas"][label] = res
        results.append(r)

    out = {"executado_em": now, "fontes": sources, "limiar_similaridade": FUZZY_THRESHOLD,
           "metodo": ("Similaridade token-set (difflib) sobre nomes normalizados (sem acentos/partículas), limiar fixo "
                      f"{FUZZY_THRESHOLD} definido antes da execução; pré-filtro ≥ 2 tokens comuns; correspondência exata de CPF/CNPJ "
                      "quando a lista o contém (CEIS/CNEP integral; PEP com 6 dígitos centrais). Candidatos conservados e resolvidos "
                      "individualmente por identificadores (guia v2 §§7.1-7.4)."),
           "resultados": results}
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in results:
        print(r["id"], r["nome"], "| doc válido:", r["cpf_digito_valido"])
        for k, v in r["listas"].items():
            unres = [c for c in v["candidatos"] if "resolucao" not in c]
            print(f"   {k:5} {v['resultado'][:60]:60} {len(v['candidatos'])} cand. / {len(unres)} não resolvidos")
    return out


if __name__ == "__main__":
    screen(*sys.argv[1:5])
