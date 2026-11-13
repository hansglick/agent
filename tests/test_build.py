"""Générateur de livrables (kyc_agent/build.py) : générique et non-régression."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

import fitz
import pytest
from openpyxl import load_workbook

from conftest import ROOT, construir


# Fragments réels d'un dossier client (noms, banque, numéros) : seules leurs empreintes SHA-256 figurent ici,
# pour vérifier leur absence sans les écrire en clair dans le dépôt de code.
INTERDITS_PDF = [(8, "3c59f2a931cce6d6"), (5, "34fdc6d2e4ed4854"), (6, "fa89ba252e78285d"), (9, "4a82cd3e8f779ec4"), (6, "8e344db614caabc2"), (7, "2ce47a6ff775bffb")]
INTERDITS_CODE = [(8, "3c59f2a931cce6d6"), (5, "34fdc6d2e4ed4854"), (6, "fa89ba252e78285d"), (9, "4a82cd3e8f779ec4"), (7, "8ca1d568110214ae")]


def contient_empreinte(texto: str, interdits) -> list:
    """Empreintes trouvées dans le texte (comparaison de chaque sous-chaîne de la bonne longueur)."""
    trouves = []
    for n, h in interdits:
        vus = {hashlib.sha256(texto[i:i + n].encode()).hexdigest()[:16] for i in range(len(texto) - n + 1)}
        if h in vus:
            trouves.append(h)
    return trouves


def texto_pdf(p: Path) -> str:
    return "\n".join(pg.get_text() for pg in fitz.open(p))


def test_livrables_produits(caso):
    v1 = caso / "versions" / "v1"
    for nome in ("EX-001_Relatorio_KYC_v1.pdf", "EX-001_Matriz_v1.xlsx", "EX-001_Evidencias_v1.zip", "Nota_FR_v1.md", "state_v1.json",
                 "mensagens/M1_BDR.md"):
        assert (v1 / nome).exists(), nome


def test_aucun_contenu_d_un_autre_dossier(caso):
    """Le code ne contient aucun texte propre à un dossier : rien de TC-187 dans les livrables d'un autre dossier."""
    texto = texto_pdf(caso / "versions" / "v1" / "EX-001_Relatorio_KYC_v1.pdf")
    assert not contient_empreinte(texto, INTERDITS_PDF)
    assert "EMPRESA EXEMPLO LTDA" in texto and "FULANO EXEMPLO" in texto


def test_code_sans_donnees_de_dossier():
    src = (ROOT / "kyc_agent" / "build.py").read_text(encoding="utf-8")
    assert not contient_empreinte(src, INTERDITS_CODE)


def test_cle_gen_manquante_arrete_le_build(caso):
    st = json.loads((caso / "state.json").read_text(encoding="utf-8"))
    st["evidencias"].append({"id": "E99", "categoria": "04_Midia_e_processos", "origem": "agente", "fonte": "gen:midia",
                             "arquivo": "(gerado)", "descricao": "x", "sustenta": "—"})
    (caso / "state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    import subprocess, sys
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "kyc_agent.build", str(caso)], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode != 0 and "gen:midia" in (r.stdout + r.stderr)


TC187 = ROOT / "cases" / "TC-187"


def _tc187_em_v2():
    try:
        return json.loads((TC187 / "state.json").read_text(encoding="utf-8"))["versao"] == 2
    except (OSError, ValueError, KeyError):
        return False


@pytest.mark.lento
@pytest.mark.skipif(not _tc187_em_v2(), reason="TC-187 absent ou passé en v3+ : la référence de non-régression est v2")
def test_non_regression_tc187_v2(tmp_path):
    """Régénère TC-187 v2 dans un répertoire temporaire et compare au livrable figé : texte des PDF, valeurs XLSX, messages,
    index du ZIP (hors chemins et empreintes, qui dépendent du répertoire et de l'horodatage des PDF)."""
    import subprocess, sys
    out = tmp_path / "v2"
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "kyc_agent.build", str(TC187), "--out", str(out)], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[-2000:]
    ref = TC187 / "versions" / "v2"

    def membros(pasta):
        d = {}
        for p in pasta.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(pasta).as_posix()
            if p.suffix == ".zip":
                with zipfile.ZipFile(p) as z:
                    for n in z.namelist():
                        d["ZIP:" + n] = z.read(n)
            elif rel not in ("i18n_fr.json", "metricas.json", "state_v2.json"):
                d[rel] = p.read_bytes()
        return d

    def norm(nome, b):
        if nome.endswith(".pdf"):
            return "\n".join(pg.get_text() for pg in fitz.open(stream=b, filetype="pdf"))
        if nome.endswith(".xlsx"):
            wb = load_workbook(io.BytesIO(b))
            linhas = [[c.value for c in r] for ws in wb for r in ws.iter_rows()]
            if nome.endswith("INDICE.xlsx"):
                linhas = [r[:6] for r in linhas]
            return linhas
        if nome.endswith("INDICE.csv"):
            return [l.split(";")[:6] for l in b.decode("utf-8-sig").splitlines()]
        return b

    a, b = membros(ref), membros(out)
    assert set(a) == set(b)
    dif = [k for k in a if norm(k, a[k]) != norm(k, b[k])]
    assert not dif, dif
