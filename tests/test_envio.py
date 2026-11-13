"""Envoi par morceaux (webapp/app.py) : lots, gros fichiers découpés, reprise, création du dossier, ajout de pièces."""
import hashlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def cli(dados, monkeypatch):
    from webapp import app as webapp
    monkeypatch.setattr(webapp, "PEDACO_BYTES", 10)  # petits morceaux pour exercer le découpage
    monkeypatch.setattr(webapp, "LOTE_BYTES", 40)
    c = TestClient(webapp.app)
    c.cookies.set("lang", "fr")
    return c


GRANDE = b"0123456789abcdefghijKLMNOPQRSTUVWXYZ!"  # 37 octets > PEDACO_BYTES : envoyé en 4 morceaux


def abrir(cli):
    r = cli.post("/envios")
    assert r.status_code == 200
    return r.json()["envio"]


def lote(cli, tok, arquivos):
    files = [("arquivos", (rel.split("/")[-1], data)) for rel, data in arquivos]
    return cli.post(f"/envios/{tok}/lote", data={"caminhos": json.dumps([rel for rel, _ in arquivos])}, files=files)


def pedaco(cli, tok, rel, data, inicio, total):
    return cli.post(f"/envios/{tok}/pedaco", params={"caminho": rel, "inicio": inicio, "total": total}, content=data)


def enviar_grande(cli, tok, rel, data, de=0):
    for i in range(de, len(data), 10):
        r = pedaco(cli, tok, rel, data[i:i + 10], i, len(data))
        assert r.status_code == 200, r.text
    return r.json()["recebido"]


def test_creation_par_lots_et_morceaux(cli, dados):
    tok = abrir(cli)
    assert lote(cli, tok, [("Pasta/a.pdf", b"AAA"), ("Pasta/sub/b.txt", b"BB")]).status_code == 200
    assert enviar_grande(cli, tok, "Pasta/grande.pdf", GRANDE) == len(GRANDE)
    # la zone d'attente est hors de cases/ et n'apparaît pas comme un dossier
    assert (dados / ".envios" / tok).is_dir() and tok not in cli.get("/").text
    r = cli.post("/dossies", data={"id": "EX-002", "nome": "Teste", "envio": tok, "n": 3, "bytes": 5 + len(GRANDE)})
    assert r.status_code == 200, r.text
    case = dados / "cases" / "EX-002"
    inv = json.loads((case / "documentos.json").read_text(encoding="utf-8"))
    assert [a["caminho"] for a in inv["arquivos"]] == ["a.pdf", "grande.pdf", "sub/b.txt"]
    g = next(a for a in inv["arquivos"] if a["caminho"] == "grande.pdf")
    assert (case / "documentos" / "grande.pdf").read_bytes() == GRANDE and g["sha256"] == hashlib.sha256(GRANDE).hexdigest()
    assert not (dados / ".envios" / tok).exists()  # zone d'attente effacée après la création


def test_reprise_apres_coupure(cli):
    tok = abrir(cli)
    assert pedaco(cli, tok, "P/g.pdf", GRANDE[:10], 0, len(GRANDE)).status_code == 200
    # un morceau qui arrive trop loin est refusé : le navigateur se réaligne sur ce que le serveur a reçu
    assert pedaco(cli, tok, "P/g.pdf", GRANDE[20:30], 20, len(GRANDE)).status_code == 409
    assert cli.get(f"/envios/{tok}").json()["arquivos"] == {"P/g.pdf": 10}
    # un morceau renvoyé (même position) est réécrit sans doublon
    assert pedaco(cli, tok, "P/g.pdf", GRANDE[:10], 0, len(GRANDE)).json()["recebido"] == 10
    assert enviar_grande(cli, tok, "P/g.pdf", GRANDE, de=10) == len(GRANDE)


def test_envoi_incomplet_refuse_puis_complete(cli, dados):
    tok = abrir(cli)
    lote(cli, tok, [("P/a.pdf", b"AAA")])
    pedaco(cli, tok, "P/g.pdf", GRANDE[:10], 0, len(GRANDE))
    r = cli.post("/dossies", data={"id": "EX-003", "envio": tok, "n": 2, "bytes": 3 + len(GRANDE)})
    assert r.status_code == 409 and "incompleto" in r.json()["detail"]
    assert not (dados / "cases" / "EX-003").exists()
    enviar_grande(cli, tok, "P/g.pdf", GRANDE, de=10)
    assert cli.post("/dossies", data={"id": "EX-003", "envio": tok, "n": 2, "bytes": 3 + len(GRANDE)}).status_code == 200


def test_identifiant_existant_l_envoi_reste_utilisable(cli, dados):
    tok = abrir(cli)
    lote(cli, tok, [("P/a.pdf", b"AAA")])
    assert cli.post("/dossies", data={"id": "EX-001", "envio": tok, "n": 1, "bytes": 3}).status_code == 409
    # identifiant refusé : l'envoi n'est pas consommé, on recrée avec un autre identifiant sans rien renvoyer
    assert cli.post("/dossies", data={"id": "EX-004", "envio": tok, "n": 1, "bytes": 3}).status_code == 200
    assert (dados / "cases" / "EX-004" / "documentos" / "a.pdf").read_bytes() == b"AAA"


def test_echec_pendant_la_creation_rend_les_fichiers(cli, dados, monkeypatch):
    from webapp import app as webapp
    tok = abrir(cli)
    lote(cli, tok, [("P/a.pdf", b"AAA"), ("P/b.pdf", b"BBB")])
    orig = webapp._FonteEnvio.salvar
    n = {"x": 0}

    async def salvar_falha(self, dest):
        n["x"] += 1
        if n["x"] == 2:
            raise OSError("disque plein (simulé)")
        return await orig(self, dest)
    monkeypatch.setattr(webapp._FonteEnvio, "salvar", salvar_falha)
    with pytest.raises(OSError):
        cli.post("/dossies", data={"id": "EX-005", "envio": tok, "n": 2, "bytes": 6})
    assert not (dados / "cases" / "EX-005").exists()
    monkeypatch.setattr(webapp._FonteEnvio, "salvar", orig)
    assert cli.post("/dossies", data={"id": "EX-005", "envio": tok, "n": 2, "bytes": 6}).status_code == 200


def test_ajout_de_pieces_par_morceaux(cli, caso):
    tok = abrir(cli)
    lote(cli, tok, [("nova.pdf", b"N1")])
    enviar_grande(cli, tok, "anexo_grande.pdf", GRANDE)
    r = cli.post("/c/EX-001/documentos", data={"envio": tok, "n": 2, "bytes": 2 + len(GRANDE)})
    assert r.status_code == 200 and r.json()["n"] == 2
    inv = json.loads((caso / "documentos.json").read_text(encoding="utf-8"))
    novos = {a["caminho"]: a for a in inv["arquivos"] if a["integrado_em_versao"] is None}
    assert set(novos) == {"nova.pdf", "anexo_grande.pdf"}
    assert (caso / "documentos" / "anexo_grande.pdf").read_bytes() == GRANDE


def test_doublon_de_chemin_refuse(cli):
    tok = abrir(cli)
    assert lote(cli, tok, [("P/a:b.pdf", b"1")]).status_code == 200
    assert lote(cli, tok, [("P/a_b.pdf", b"2")]).status_code == 400  # même nom une fois rendu sûr pour Windows


def test_abandon_et_expiration(cli, dados, monkeypatch):
    from webapp import app as webapp
    tok = abrir(cli)
    lote(cli, tok, [("P/a.pdf", b"A")])
    assert cli.post(f"/envios/{tok}/abandonar").status_code == 200
    assert not (dados / ".envios" / tok).exists()
    assert cli.get(f"/envios/{tok}").status_code == 404
    assert cli.get("/envios/../cases").status_code in (400, 404)
    vieux = abrir(cli)
    m = dados / ".envios" / vieux / "manifesto.json"
    os.utime(m, (time.time() - webapp.ENVIO_TTL_S - 10,) * 2)
    abrir(cli)  # l'ouverture d'un nouvel envoi efface les envois abandonnés depuis plus de 24 h
    assert not (dados / ".envios" / vieux).exists()


def test_envoi_en_une_fois_toujours_accepte(cli, dados):
    r = cli.post("/dossies", data={"id": "EX-006", "caminhos": json.dumps(["P/x.pdf"])}, files=[("arquivos", ("x.pdf", b"X"))])
    assert r.status_code == 200
    assert (dados / "cases" / "EX-006" / "documentos" / "x.pdf").read_bytes() == b"X"
