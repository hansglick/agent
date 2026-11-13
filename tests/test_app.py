"""Application web (webapp/app.py) : pages, statuts, réouverture, arrêt, pastilles."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import adicionar_pendentes


@pytest.fixture
def cli(dados):
    from webapp.app import app
    c = TestClient(app)
    c.cookies.set("lang", "fr")
    return c


def ler(p):
    return json.loads(p.read_text(encoding="utf-8"))


def run_em_curso(caso, ativos=1):
    rd = caso / "runs" / "20260116T120000_pedido"
    rd.mkdir(parents=True)
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (rd / "pedido.json").write_text(json.dumps({"estado": "em_execucao", "pendentes": {"pecas": [], "respostas": []}}), encoding="utf-8")
    (rd / "status.json").write_text(json.dumps({"estado": "em_curso", "inicio": agora, "atualizado_em": agora, "tarefa": "Lecture",
                                                "modelo": "claude-opus-5-5", "uso": {}, "pid": 0,
                                                "agentes": {"ativos": ativos, "subagentes": []}}), encoding="utf-8")
    return rd


def test_pages_principales(cli):
    assert cli.get("/").status_code == 200
    for tab in ("painel", "matriz", "verificacoes", "mensagens"):
        r = cli.get(f"/c/EX-001/v1/{tab}")
        assert r.status_code == 200, tab
    assert "EX-001" in cli.get("/").text


def test_pastille_agent_au_travail(cli, caso):
    assert 'data-cid="EX-001" data-one' in cli.get("/").text
    assert cli.get("/agentes.json").json() == {}
    run_em_curso(caso, ativos=3)
    assert cli.get("/agentes.json").json() == {"EX-001": 3}
    assert "3 agents au travail" in cli.get("/").text
    assert "3 agents au travail" in cli.get("/c/EX-001/v1/matriz").text


def test_cloture_reouverture_et_historique(cli, caso):
    r = cli.post("/c/EX-001/status", data={"acao": "tratar"}, follow_redirects=False)
    assert r.status_code == 303
    meta = ler(caso / "dossie.json")
    assert meta["tratado_em"] and meta["historico_status"][-1]["acao"] == "tratar"
    painel = cli.get("/c/EX-001/v1/painel").text
    assert "Dossier terminé" in painel and "Rouvrir le dossier" in painel
    # mise à jour refusée tant que le dossier est terminé
    adicionar_pendentes(caso)
    assert cli.post("/c/EX-001/atualizar", follow_redirects=False).status_code == 409
    # réouverture
    assert cli.post("/c/EX-001/status", data={"acao": "reabrir"}, follow_redirects=False).status_code == 303
    meta = ler(caso / "dossie.json")
    assert "tratado_em" not in meta and [h["acao"] for h in meta["historico_status"]] == ["tratar", "reabrir"]
    assert "Dossier terminé" not in cli.get("/c/EX-001/v1/painel").text
    # rouvrir un dossier non terminé : refusé
    assert cli.post("/c/EX-001/status", data={"acao": "reabrir"}, follow_redirects=False).status_code == 400


def test_bouton_arreter(cli, caso, monkeypatch):
    from webapp import app as webapp
    assert cli.post("/c/EX-001/parar", follow_redirects=False).status_code == 409  # rien en cours
    rd = run_em_curso(caso)
    assert 'id="btn-parar"' in cli.get("/c/EX-001/v1/painel").text
    lancados = []
    monkeypatch.setattr(webapp.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda s: lancados.append(args)})())
    assert cli.post("/c/EX-001/parar", follow_redirects=False).status_code == 303
    assert (rd / "parar.json").exists() and len(lancados) == 1
    assert cli.get("/c/EX-001/agente.json").json()["parada_pedida"] is True
    assert "Mise en pause demandée" in cli.get("/c/EX-001/v1/painel").text


def test_arret_force_si_le_worker_ne_repond_pas(cli, caso, monkeypatch):
    from webapp import app as webapp
    rd = run_em_curso(caso)
    mortos = []
    monkeypatch.setattr(webapp, "PRAZO_PARADA_S", 1)
    monkeypatch.setattr(webapp, "_matar_arvore", lambda pid: mortos.append(pid))
    st = ler(rd / "status.json")
    st["pid"] = 4242
    (rd / "status.json").write_text(json.dumps(st), encoding="utf-8")
    webapp._garantir_parada("EX-001", rd)
    st = ler(rd / "status.json")
    assert mortos == [4242] and st["estado"] == "parado" and st["revertido"] is True


def test_suppression_apres_consultation_de_la_matrice(cli, caso):
    """La page Matrice lit le XLSX : le fichier doit être refermé, sinon Windows bloque la suppression du dossier."""
    assert cli.get("/c/EX-001/v1/matriz").status_code == 200
    r = cli.post("/c/EX-001/excluir", data={"confirmo": "EX-001"}, follow_redirects=False)
    assert r.status_code == 303
    assert not caso.exists() and not list(caso.parent.glob(".del_EX-001_*"))


def test_pas_de_bouton_email_pour_un_message_au_client_via_bdr(cli):
    """Guide v2 : le client est contacté via le BDR — aucun brouillon e-mail adressé directement au client."""
    from webapp.app import envio_intermediado
    assert envio_intermediado({"destinatario": "Cliente", "para": "Fulano <fulano@exemplo.invalid>"})
    assert envio_intermediado({"destinatario": "Contraparte", "para": "Fulano <f@exemplo.invalid> — envio pela BDR Nome"})
    assert envio_intermediado({"destinatario": "Client", "para": "x@exemplo.invalid — envoi par le BDR"})
    assert not envio_intermediado({"destinatario": "BDR", "para": "Nome Fictício BDR <bdr@exemplo.invalid>"})
    assert not envio_intermediado({"destinatario": "Jurídico", "para": "juridico@exemplo.invalid"})
    # le message M1 du dossier fictif est adressé au BDR : le bouton reste proposé
    assert "Ouvrir dans l&#39;e-mail" in cli.get("/c/EX-001/v1/mensagens").text or "Ouvrir dans l'e-mail" in cli.get("/c/EX-001/v1/mensagens").text


def test_identifiant_invalide_message_traduit_et_regle_valide(dados):
    from webapp.app import app
    cli = TestClient(app)
    cli.cookies.set("lang", "fr")
    r = cli.post("/dossies", data={"id": "TC 187", "caminhos": "[]"})
    assert r.status_code == 400 and r.json()["detail"].startswith("Identifiant invalide")
    # règle du champ compatible avec le mode « v » des navigateurs récents (le tiret doit être échappé)
    assert r'pattern="[A-Za-z0-9._\-]{1,40}"' in cli.get("/").text


def test_tache_avec_boutons_comptee_comme_action_humaine(caso):
    """Un état où l'agent a oublié « acao_humana » : la vérification avec boutons de réponse reste attendue."""
    p = caso / "versions" / "v1" / "state_v1.json"
    st = json.loads(p.read_text(encoding="utf-8"))
    st["registro_tecnico"] = [{"id": "T9", "controle": "C07 — CPF", "incidente": "captcha", "intervencao": "consultar", "impacto": "-",
                               "prioridade": "P2", "evidencias": [], "respostas": [{"id": "upload_comprovante", "rotulo": "Enviar",
                                                                                     "arquivos": "obrigatorio"}]}]
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    from webapp.app import app
    assert "C07 — CPF" in TestClient(app).get("/c/EX-001/v1/verificacoes").text


def test_onglets_grises_avant_la_premiere_analyse(cli):
    r = cli.post("/dossies", data={"id": "NOVO-1", "caminhos": json.dumps(["pasta/a.txt"])},
                 files=[("arquivos", ("a.txt", b"conteudo", "text/plain"))], follow_redirects=False)
    assert r.status_code in (200, 303), r.text
    page = cli.get("/c/NOVO-1/painel").text
    assert page.count('class="tab-off"') == 5 and "Disponible après la première analyse" in page
    assert 'href="/c/NOVO-1/matriz"' not in page
    # après la première analyse : onglets actifs
    assert 'class="tab-off"' not in cli.get("/c/EX-001/v1/painel").text
