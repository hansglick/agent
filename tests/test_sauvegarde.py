"""Sauvegarde automatique (kyc_agent/sauvegarde.py + pages de l'application)."""
import json
import os
import time
import zipfile

from fastapi.testclient import TestClient

from conftest import adicionar_pendentes
from kyc_agent import sauvegarde as bkp
from test_app import run_em_curso


def conteudo(case):
    return {p.relative_to(case).as_posix(): p.read_bytes() for p in case.rglob("*") if p.is_file()}


def test_copie_puis_rien_si_inchange_puis_nouvelle_copie(dados, caso):
    r = bkp.ciclo(dados / "cases")
    assert r["feitas"] == ["EX-001"] and not r["erros"]
    cs = bkp.copias("EX-001")
    assert len(cs) == 1
    with zipfile.ZipFile(cs[0]["path"]) as z:
        nomes = set(z.namelist())
        meta = json.loads(z.read("_pecas.json"))
    assert "state.json" in nomes and "versions/v1/EX-001_Relatorio_KYC_v1.pdf" in nomes
    assert not any(n.startswith("documentos/") for n in nomes)  # les pièces sont à part, une seule fois
    assert [x["caminho"] for x in meta["pecas"]] == ["contrato_social.pdf"]
    assert (bkp.backup_dir() / "EX-001" / "pecas" / meta["pecas"][0]["sha256"]).read_bytes() == b"%PDF-1.4 ficticio"
    # rien n'a changé : pas de nouvelle copie
    assert bkp.ciclo(dados / "cases")["sem_alteracao"] == ["EX-001"] and len(bkp.copias("EX-001")) == 1
    # le dossier change : nouvelle copie, la pièce inchangée n'est pas recopiée
    time.sleep(1.1)  # horodatage de copie à la seconde
    adicionar_pendentes(caso)
    assert bkp.ciclo(dados / "cases")["feitas"] == ["EX-001"]
    assert len(bkp.copias("EX-001")) == 2
    assert len(list((bkp.backup_dir() / "EX-001" / "pecas").iterdir())) == 2  # contrato_social + nova_1


def test_pas_de_copie_pendant_une_execution(dados, caso):
    run_em_curso(caso)
    r = bkp.ciclo(dados / "cases")
    assert r["adiadas"] == ["EX-001"] and not bkp.copias("EX-001")


def test_restauration_dans_un_nouveau_dossier(dados, caso):
    bkp.ciclo(dados / "cases")
    original = conteudo(caso)
    novo = bkp.restaurar("EX-001", "ultima", "EX-001-R", cases=dados / "cases")
    rest = conteudo(novo)
    assert rest["documentos/contrato_social.pdf"] == b"%PDF-1.4 ficticio"
    for k in ("state.json", "versions/v1/EX-001_Relatorio_KYC_v1.pdf", "documentos.json"):
        assert rest[k] == original[k], k
    meta = json.loads(rest["dossie.json"])
    assert meta["restaurado_de"]["dossie"] == "EX-001" and meta["nome"].endswith("(restaurado)")
    assert conteudo(caso) == original  # l'original n'est pas touché
    try:
        bkp.restaurar("EX-001", "ultima", "EX-001-R", cases=dados / "cases")
        assert False, "doit refuser d'écraser"
    except ValueError as e:
        assert "existe déjà" in str(e)


def test_piece_alteree_dans_la_sauvegarde_detectee(dados, caso):
    bkp.ciclo(dados / "cases")
    for p in (bkp.backup_dir() / "EX-001" / "pecas").iterdir():
        p.write_bytes(b"altere")
    try:
        bkp.restaurar("EX-001", "ultima", "EX-001-X", cases=dados / "cases")
        assert False
    except ValueError as e:
        assert "altérée" in str(e)
    assert not (dados / "cases" / "EX-001-X").exists()


def test_conservation_limitee_et_nettoyage_des_pieces(dados, caso, monkeypatch):
    monkeypatch.setenv("KYC_BACKUP_GARDER", "2")
    for i in range(3):
        if i:
            time.sleep(1.1)
            (caso / "documentos" / f"extra_{i}.pdf").write_bytes(f"piece {i}".encode())
        bkp.salvar(caso)
    assert len(bkp.copias("EX-001")) == 2
    # la pièce de la 1re copie (supprimée) reste utilisée par les suivantes ; aucune pièce orpheline
    usados = set()
    for c in bkp.copias("EX-001"):
        with zipfile.ZipFile(c["path"]) as z:
            usados |= {x["sha256"] for x in json.loads(z.read("_pecas.json"))["pecas"]}
    assert {p.name for p in (bkp.backup_dir() / "EX-001" / "pecas").iterdir()} == usados


def test_pages_et_suppression(dados, caso):
    from webapp.app import app
    cli = TestClient(app)
    cli.cookies.set("lang", "fr")
    assert "Sauvegarde automatique" in cli.get("/").text
    bkp.ciclo(dados / "cases")
    page = cli.get("/sauvegardes").text
    assert "EX-001" in page and "Restaurer comme nouveau dossier" in page
    nome = bkp.copias("EX-001")[0]["nome"]
    r = cli.post("/sauvegardes/restaurar", data={"cid": "EX-001", "copia": nome}, follow_redirects=False)
    assert r.status_code == 303 and "EX-001-restaurado-" in r.headers["location"]
    # la suppression définitive d'un dossier efface aussi ses sauvegardes
    assert cli.post("/c/EX-001/excluir", data={"confirmo": "EX-001"}, follow_redirects=False).status_code == 303
    assert not (bkp.backup_dir() / "EX-001").exists()
