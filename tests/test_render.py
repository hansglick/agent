"""En ligne (Render), l'application ne s'affiche jamais sans identifiant/mot de passe."""
import base64

from fastapi.testclient import TestClient


def test_en_ligne_sans_identifiants_tout_est_bloque(dados, monkeypatch):
    from webapp import app as webapp
    monkeypatch.setattr(webapp, "EM_LINHA", True)
    monkeypatch.setattr(webapp, "USER", None)
    monkeypatch.setattr(webapp, "PWD", None)
    c = TestClient(webapp.app)
    assert c.get("/").status_code == 503
    assert c.get("/c/EX-001/v1/painel").status_code == 503
    assert c.get("/healthz").json() == {"ok": True}  # contrôle de santé de Render, sans rien révéler


def test_en_ligne_avec_identifiants(dados, monkeypatch):
    from webapp import app as webapp
    monkeypatch.setattr(webapp, "EM_LINHA", True)
    monkeypatch.setattr(webapp, "USER", "compliance")
    monkeypatch.setattr(webapp, "PWD", "s3cret")
    c = TestClient(webapp.app)
    assert c.get("/").status_code == 401
    bon = {"Authorization": "Basic " + base64.b64encode(b"compliance:s3cret").decode()}
    assert c.get("/", headers=bon).status_code == 200
