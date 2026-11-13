"""Fenêtre « clé API » : proposée seulement en local sans clé ; la clé est vérifiée puis enregistrée, jamais renvoyée."""
from fastapi.testclient import TestClient

CLE = "sk-ant-api03-" + "x" * 40


def _client(dados, monkeypatch, local=True, cle=""):
    from webapp import app as webapp
    from kyc_agent import worker
    monkeypatch.setattr(webapp, "cle_local", lambda request: local)
    monkeypatch.delenv("KYC_AGENT_SIMULATE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if cle:
        monkeypatch.setenv("KYC_ANTHROPIC_API_KEY", cle)
    else:
        monkeypatch.delenv("KYC_ANTHROPIC_API_KEY", raising=False)
    assert bool(worker.chave_api()) == bool(cle)
    return TestClient(webapp.app)


def test_fenetre_ouverte_sans_cle_en_local(dados, monkeypatch):
    h = _client(dados, monkeypatch).get("/").text
    assert 'id="dlg-cle"' in h and 'class="sim-band"' in h  # sans clé : simulation, et on le dit


def test_pas_de_fenetre_avec_cle_ni_hors_local(dados, monkeypatch):
    assert 'id="dlg-cle"' not in _client(dados, monkeypatch, cle=CLE).get("/").text
    c = _client(dados, monkeypatch, local=False)
    assert 'id="dlg-cle"' not in c.get("/").text
    assert c.post("/config/cle", data={"cle": CLE}).status_code == 403


def test_format_invalide_refuse_sans_rien_enregistrer(dados, monkeypatch):
    from kyc_agent import cle
    gravadas = []
    monkeypatch.setattr(cle, "enregistrer", gravadas.append)
    monkeypatch.setattr(cle, "tester", lambda k, m: (_ for _ in ()).throw(AssertionError("pas d'appel API")))
    r = _client(dados, monkeypatch).post("/config/cle", data={"cle": "ma cle"}).json()
    assert r["ok"] is False and not gravadas


def test_cle_refusee_par_anthropic_non_enregistree(dados, monkeypatch):
    from kyc_agent import cle
    gravadas = []
    monkeypatch.setattr(cle, "enregistrer", gravadas.append)
    monkeypatch.setattr(cle, "tester", lambda k, m: (False, "recusada"))
    r = _client(dados, monkeypatch).post("/config/cle", data={"cle": CLE}).json()
    assert r["ok"] is False and not gravadas and CLE not in r["msg"]


def test_cle_valide_enregistree_et_jamais_renvoyee(dados, monkeypatch):
    from kyc_agent import cle
    gravadas = []
    monkeypatch.setattr(cle, "enregistrer", gravadas.append)
    monkeypatch.setattr(cle, "tester", lambda k, m: (True, "ok"))
    r = _client(dados, monkeypatch).post("/config/cle", data={"cle": "  " + CLE + " "}).json()
    assert r["ok"] is True and gravadas == [CLE] and CLE not in r["msg"] and cle.masque(CLE) in r["msg"]
