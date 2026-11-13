"""Bouton « Pas nécessaire (ignorer) » : le superviseur écarte une tâche ou des pendances (motif obligatoire)."""
import json

import pytest
from fastapi.testclient import TestClient

from kyc_agent import worker
from kyc_agent.respostas import IGNORAR, acoes, acoes_mensagem


@pytest.fixture
def cli(dados):
    from webapp.app import app
    c = TestClient(app)
    c.cookies.set("lang", "fr")
    return c


def respostas(caso):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((caso / "respostas").glob("*/resposta.json"))]


def test_action_toujours_proposee():
    assert acoes({"id": "T9"})[-1]["id"] == IGNORAR
    assert [a["id"] for a in acoes({"id": "T9", "respostas": [{"id": IGNORAR, "rotulo": "x"}]})] == [IGNORAR]  # pas de doublon
    assert [a["id"] for a in acoes({"id": "T9", "respostas": [{"id": IGNORAR}, {"id": "ok"}]})] == ["ok", IGNORAR]
    a = acoes_mensagem({"id": "M1", "pendencias": ["P01", "P02"]})[-1]
    assert a["id"] == IGNORAR and a["pendencias"] == ["P01", "P02"] and a["texto"] == "obrigatorio"


def test_ignorer_une_tache(cli, caso):
    page = cli.get("/c/EX-001/v1/verificacoes").text
    assert "Pas nécessaire (ignorer)" in page and "btn-ignorar" in page
    assert cli.post("/c/EX-001/v1/resposta", data={"item": "T1", "acao": IGNORAR}, follow_redirects=False).status_code == 400
    r = cli.post("/c/EX-001/v1/resposta", data={"item": "T1", "acao": IGNORAR, "texto": "Base interne inexistante chez GF"},
                 follow_redirects=False)
    assert r.status_code == 303
    rr = respostas(caso)[-1]
    assert rr["dispensa"] and rr["texto"] == "Base interne inexistante chez GF" and rr["item"] == "T1"
    # l'agent reçoit une consigne explicite
    d = sorted((caso / "respostas").iterdir())[-1].name
    txt = worker.resumo_pendentes(caso, {"pendentes": {"respostas": [d], "pecas": []}})
    assert "DEMANDE ÉCARTÉE PAR LE SUPERVISEUR : T1" in txt and "Base interne inexistante" in txt
    # la tâche n'est plus comptée dans les documents attendus ; la mise à jour devient possible
    page = cli.get("/c/EX-001/v1/painel").text
    assert 'id="cta-n"' in page


def test_ignorer_des_pendances_d_un_message(cli, caso):
    page = cli.get("/c/EX-001/v1/mensagens").text
    assert "Pendances à écarter" in page and 'name="pendencias" value="P01"' in page
    base = {"item": "M1", "acao": IGNORAR, "texto": "Hors périmètre"}
    assert cli.post("/c/EX-001/v1/resposta", data=base, follow_redirects=False).status_code == 400  # aucune pendance cochée
    assert cli.post("/c/EX-001/v1/resposta", data={**base, "pendencias": ["P99"]}, follow_redirects=False).status_code == 400
    assert cli.post("/c/EX-001/v1/resposta", data={**base, "pendencias": ["P01"]}, follow_redirects=False).status_code == 303
    rr = respostas(caso)[-1]
    assert rr["pendencias_dispensadas"] == ["P01"] and rr["dispensa"]
    d = sorted((caso / "respostas").iterdir())[-1].name
    assert "pendances écartées : P01" in worker.resumo_pendentes(caso, {"pendentes": {"respostas": [d], "pecas": []}})
    assert "(P01)" in cli.get("/c/EX-001/v1/mensagens").text


def test_methode_explique_la_demande_ecartee():
    t = (worker.PROMPTS / "metodo.md").read_text(encoding="utf-8")
    assert "dispensado pelo supervisor" in t and "ne les recrée pas" in t
