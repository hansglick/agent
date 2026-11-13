"""Bilinguisme de l'app web (pt-BR / fr).

- UI : libellés des gabarits, traduits par t() (clé = texte portugais ; repli = portugais).
- Données du dossier : dictionnaire par version `versions/vN/i18n_fr.json` {texte pt: texte fr},
  appliqué récursivement au state, aux feuilles XLSX et à l'index du ZIP. Une chaîne absente du
  dictionnaire reste en portugais (jamais de traduction inventée à la volée).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

LANGS = ("pt", "fr")

UI_FR = {
    # navigation / en-tête
    "Tarefas": "Tâches", "Relatório": "Rapport", "Matriz": "Matrice", "Mensagens e anexos": "Messages et pièces jointes",
    "ZIP de provas": "ZIP de preuves", "Note FR": "Note FR", "Versão": "Version", "(atual)": "(actuelle)",
    "Idioma": "Langue", "Português (Brasil)": "Portugais (Brésil)", "Francês": "Français",
    "Rascunho preparado por agente de IA — recomendação sujeita a revisão e autorização humanas. Confidencial – uso interno.":
        "Brouillon préparé par un agent IA — recommandation soumise à revue et autorisation humaines. Confidentiel – usage interne.",
    "Copiar texto": "Copier le texte", "Mensagens": "Messages", "Provas": "Preuves", "Copiado ✓": "Copié ✓", "Cópia indisponível": "Copie indisponible",
    # accueil
    "Dossiês": "Dossiers", "Caso": "Dossier", "Nome": "Nom", "Última versão": "Dernière version", "Recomendação": "Recommandation",
    "Nenhum dossiê encontrado em DATA_DIR/cases.": "Aucun dossier trouvé dans DATA_DIR/cases.",
    # tarefas
    "Tarefas em curso": "Tâches en cours", "a fazer": "à faire",
    "Marcar como feito fica só neste navegador (lembrete pessoal) — não altera o dossiê.":
        "Cocher « fait » reste uniquement dans ce navigateur (aide-mémoire personnel) — ne modifie pas le dossier.",
    "A devolver:": "À renvoyer :", "Abrir a mensagem": "Ouvrir le message", "Abrir o relatório": "Ouvrir le rapport",
    "Abrir o site ↗": "Ouvrir le site ↗", "Feito:": "Fait :",
    # vérifications
    "Verificações": "Vérifications", "Verificações que o agente não pôde realizar": "Vérifications que l'agent n'a pas pu réaliser",
    "O agente não contorna captchas e só consulta as bases que lhe são fornecidas. Quando um controle fica bloqueado, ele é registrado aqui (registro técnico interno) — nunca apresentado como resultado negativo.":
        "L'agent ne contourne pas les captchas et ne consulte que les bases qui lui sont fournies. Quand un contrôle est bloqué, il est enregistré ici (registre technique interne) — jamais présenté comme un résultat négatif.",
    "Não são pedidos a terceiros: são ações internas do Compliance. Depois de realizá-las, entregue o resultado ao agente, que o integra na próxima versão do dossiê.":
        "Ce ne sont pas des demandes à des tiers : ce sont des actions internes de Compliance. Une fois réalisées, remettez le résultat à l'agent, qui l'intègre dans la prochaine version du dossier.",
    "A realizar pelo Compliance": "À réaliser par Compliance", "Para informação — nenhuma ação necessária": "Pour information — aucune action nécessaire",
    "Por que não foi feito": "Pourquoi ce n'a pas été fait", "Impacto no dossiê": "Impact sur le dossier", "Evidências": "Preuves",
    "Nenhuma verificação bloqueada nesta versão.": "Aucune vérification bloquée dans cette version.",
    "Verificações bloqueadas": "Vérifications bloquées", "ver a aba": "voir l'onglet", "Ver a verificação": "Voir la vérification",
    # réponses
    "Responder": "Répondre", "Enviar": "Envoyer", "Texto": "Texte", "Comentário (opcional)": "Commentaire (facultatif)",
    "Documentos": "Documents", "(opcional)": "(facultatif)", "Formatos aceitos:": "Formats acceptés :", "Efeito:": "Effet :",
    "Confirmar e registrar": "Confirmer et enregistrer", "Respostas recebidas": "Réponses reçues",
    "integrada na versão": "intégrée en version", "recebida — aguardando integração pelo agente": "reçue — en attente d'intégration par l'agent",
    "Resposta registrada. Ela será integrada pelo agente na próxima versão do dossiê.":
        "Réponse enregistrée. Elle sera intégrée par l'agent dans la prochaine version du dossier.",
    "resposta recebida": "réponse reçue",
    "Registrar a resposta recebida": "Enregistrer la réponse reçue",
    "Resposta (colar o texto recebido)": "Réponse (coller le texte reçu)", "Anexos recebidos": "Pièces jointes reçues",
    "Colar o texto da resposta e/ou juntar os documentos enviados pelo destinatário. Uma resposta parcial pode ser registrada; as seguintes se somam a ela.":
        "Coller le texte de la réponse et/ou joindre les documents envoyés par le destinataire. Une réponse partielle peut être enregistrée ; les suivantes s'y ajoutent.",
    "O agente confronta a resposta com as pendências cobertas por esta mensagem e atualiza o dossiê na próxima versão.":
        "L'agent confronte la réponse aux pendências couvertes par ce message et met à jour le dossier dans la prochaine version.",
    "Responder / enviar documentos": "Répondre / envoyer des documents",
    "Resposta tratada pelo agente na próxima versão.": "Réponse traitée par l'agent dans la prochaine version.",
    # tableau de bord
    "Painel": "Tableau de bord", "Dossiê em tratamento": "Dossier en cours de traitement", "Versão atual": "Version actuelle",
    "Dossiê": "Dossier", "Versões": "Versions", "Criação do dossiê": "Création du dossier", "Tempo desde a criação": "Durée depuis la création",
    "Consumo do agente": "Consommation de l'agent", "Tokens de entrada": "Tokens en entrée", "Tokens de saída": "Tokens en sortie",
    "Tokens no total": "Tokens au total", "Custo (equivalente API)": "Coût (équivalent API)",
    "Tempo de processamento do agente": "Durée de traitement de l'agent",
    "Medição exata por atualização a partir da v3.": "Mesure exacte par mise à jour à partir de la v3.",
    "Versões não medidas separadamente:": "Versions non mesurées séparément :", "Sessão inicial (informativo)": "Session initiale (pour information)",
    "Sessão Claude Code inicial (análise v1/v2 do dossiê E desenvolvimento da aplicação web). Não atribuível somente ao dossiê; mostrada a título informativo.":
        "Session Claude Code initiale (analyse v1/v2 du dossier ET développement de l'application web). Non attribuable au seul dossier ; affichée pour information.",
    "Duração (relógio)": "Durée (horloge)", "Entrada = não cacheada + escrita em cache + leitura de cache.": "Entrée = non mise en cache + écriture en cache + lecture du cache.",
    "Tarifa pública Claude API (Opus 5.5), consultada em 10/11/2026": "Tarif public Claude API (Opus 5.5), consulté le 10/11/2026",
    "Uso atual pela assinatura Claude: valor não faturado por token.": "Usage actuel via l'abonnement Claude : montant non facturé au token.",
    "Ações a tomar": "Actions à prendre", "das quais tarefas (verificações)": "dont tâches (vérifications)",
    "das quais respostas a mensagens aguardadas": "dont réponses aux messages attendues", "Resposta aguardada de": "Réponse attendue de",
    "Respostas aguardando tratamento": "Réponses en attente de traitement", "Atualizar o dossiê para a v": "Mettre à jour le dossier vers la v",
    "Botão disponível quando o agente estiver ligado à aplicação (Claude Agent SDK, conta API necessária). Enquanto isso, peça ao agente no Claude Code: «integra as respostas».":
        "Bouton disponible quand l'agent sera relié à l'application (Claude Agent SDK, compte API nécessaire). En attendant, demande à l'agent dans Claude Code : « intègre les réponses ».",
    "Recebida em": "Reçue le", "Ação": "Action", "Conteúdo": "Contenu", "Autor": "Auteur", "confirmação": "confirmation",
    "Nenhuma resposta aguardando tratamento.": "Aucune réponse en attente de traitement.",
    "Nenhuma execução registrada. Os logs da atualização do dossiê aparecerão aqui em tempo real quando o botão de atualização estiver ativo.":
        "Aucune exécution enregistrée. Les logs de la mise à jour du dossier s'afficheront ici en temps réel quand le bouton de mise à jour sera actif.",
    "Histórico": "Historique", "Evento": "Événement", "Mudanças": "Changements", "Consumo": "Consommation", "atual": "actuelle",
    "mudanças": "changements", "não medido separadamente": "non mesuré séparément",
    # suivi des dossiers
    "Acompanhamento dos dossiês": "Suivi des dossiers", "Novo dossiê": "Nouveau dossier", "Status": "Statut",
    "Em preparação": "En préparation", "Em instrução": "En cours d'instruction", "Tratado": "Traité", "Documentos": "Documents",
    "Criado em": "Créé le", "Última atualização": "Dernière mise à jour", "novo": "nouveau",
    "Nenhum dossiê. Clique em «+» para criar o primeiro.": "Aucun dossier. Clique sur « + » pour créer le premier.",
    "Em preparação: documentos importados, análise ainda não produzida. Em instrução: ao menos uma versão produzida pelo agente. Tratado: validação humana registrada.":
        "En préparation : documents importés, analyse pas encore produite. En cours d'instruction : au moins une version produite par l'agent. Traité : validation humaine enregistrée.",
    "Comando em segundo plano iniciado": "Commande en arrière-plan lancée",
    "envio(s) de dados recusado(s) ao agente": "envoi(s) de données refusé(s) à l'agent",
    "sites fora da lista branca dos sites oficiais": "sites hors de la liste blanche des sites officiels",
    "Detalhe e o que fazer": "Détail et marche à suivre",
    "Requisição de atualização": "Requête de mise à jour",
    "Custo da atualização em curso": "Coût de la mise à jour en cours",
    "Última atualização": "Dernière mise à jour",
    "Recolher / mostrar": "Replier / déplier", "Preparação": "Préparation", "Atividade": "Activité",
    "Agente único": "Agent unique", "econômico": "économique",
    "Um só agente faz todo o trabalho, sem nenhum subagente. Fatura menor; análise mais longa.":
        "Un seul agent fait tout le travail, sans aucun sous-agent. Facture plus basse ; analyse plus longue.",
    "O agente reflete": "L'agent réfléchit", "O agente redige": "L'agent rédige", "Redigindo": "Rédaction de",
    "Preparando": "Prépare", "caracteres": "caractères",
    "Modo de trabalho": "Mode de travail", "Agente livre": "Agent libre", "Multiagentes": "Multi-agents", "versão beta": "version bêta",
    "O agente decide sozinho se reparte o trabalho entre subagentes, e quantos. Comportamento comprovado.":
        "L'agent décide seul s'il répartit le travail entre des sous-agents, et combien. Comportement éprouvé.",
    "O agente principal reparte leitura das peças, screening por parte e verificações de autenticidade entre subagentes especializados em paralelo, depois consolida. Mais rápido; custo um pouco maior.":
        "L'agent principal répartit la lecture des pièces, le screening par partie et les vérifications d'authenticité entre des sous-agents spécialisés en parallèle, puis consolide. Plus rapide ; coût un peu plus élevé.",
    "Não é necessário (ignorar)": "Pas nécessaire (ignorer)",
    "O supervisor considera que este pedido não é necessário para este dossiê.":
        "Le superviseur considère que cette demande n'est pas nécessaire pour ce dossier.",
    "Na próxima atualização, o agente encerra o item como «dispensado pelo supervisor» (motivo, autor, data). Não é um resultado negativo; o agente não o recria, salvo fato novo que ele deverá explicar.":
        "Lors de la prochaine mise à jour, l'agent clôt l'élément comme « écarté par le superviseur » (motif, auteur, date). Ce n'est pas un résultat négatif ; l'agent ne le recrée pas, sauf fait nouveau qu'il devra expliquer.",
    "Na próxima atualização, o agente encerra as pendências marcadas como «dispensadas pelo supervisor» (motivo, autor, data) e as retira das mensagens. Não é um resultado negativo; ele não as recria, salvo fato novo que deverá explicar.":
        "Lors de la prochaine mise à jour, l'agent clôt les pendances cochées comme « écartées par le superviseur » (motif, auteur, date) et les retire des messages. Ce n'est pas un résultat négatif ; il ne les recrée pas, sauf fait nouveau qu'il devra expliquer.",
    "Pendências a dispensar": "Pendances à écarter", "escolha ao menos uma pendência": "cochez au moins une pendance",
    "Descreva o que o agente deve fazer na próxima atualização; ela produzirá uma nova versão do dossiê.":
        "Décrivez ce que l'agent doit faire lors de la prochaine mise à jour ; elle produira une nouvelle version du dossier.",
    "Em tratamento pela atualização em curso.": "En cours de traitement par la mise à jour en cours.",
    "Requisição vazia": "Requête vide", "Mensagem vazia": "Message vide",
    "Requisição longa demais (máximo 4000 caracteres)": "Requête trop longue (4000 caractères maximum)",
    "Mensagem longa demais (máximo 4000 caracteres)": "Message trop long (4000 caractères maximum)",
    "O agente ainda está respondendo": "L'agent est encore en train de répondre",
    "Conversar com o agente": "Discuter avec l'agent", "Conversa com o agente": "Conversation avec l'agent",
    "Nova conversa": "Nouvelle conversation", "Fechar": "Fermer",
    "O agente recomeça sem o contexto da conversa atual (arquivada).": "L'agent repart sans le contexte de la conversation actuelle (archivée).",
    "Somente perguntas: o agente consulta o dossiê sem modificá-lo. Para mudar o dossiê, use a requisição de atualização (lápis laranja).":
        "Questions uniquement : l'agent consulte le dossier sans le modifier. Pour changer le dossier, utilisez la requête de mise à jour (crayon orange).",
    "Sua pergunta…": "Votre question…", "O agente está pensando": "L'agent réfléchit",
    "Usar como requisição de atualização": "Utiliser comme requête de mise à jour",
    "Faça uma pergunta sobre o dossiê: controles, peças, pendências, regras do guia…":
        "Posez une question sur le dossier : contrôles, pièces, pendances, règles du guide…",
    "autorizado": "autorisé", "Autorizar este site": "Autoriser ce site", "Autorizar": "Autoriser",
    "Relançar as consultas bloqueadas": "Relancer les consultations bloquées",
    "O agente refaz as consultas dos sites autorizados e integra os resultados numa nova versão.":
        "L'agent refait les consultations des sites autorisés et intègre les résultats dans une nouvelle version.",
    "Se for um site oficial legítimo, autorize-o (botão acima, motivo obrigatório) e relance as consultas bloqueadas. Caso contrário, nada a fazer: a consulta correspondente aparece como tarefa humana.":
        "S'il s'agit d'un site officiel légitime, autorisez-le (bouton ci-dessus, motif obligatoire) puis relancez les consultations bloquées. Sinon, rien à faire : la consultation correspondante apparaît comme tâche humaine.",
    "O agente poderá enviar dados a este site (formulário, API) em todos os dossiês. Autorize apenas um site oficial necessário a um controle.":
        "L'agent pourra envoyer des données à ce site (formulaire, API) dans tous les dossiers. N'autorisez qu'un site officiel nécessaire à un contrôle.",
    "Motivo (obrigatório)": "Motif (obligatoire)",
    "ex.: portal oficial de verificação de certidões da prefeitura": "ex. : portail officiel de vérification des certidões de la mairie",
    "Motivo obrigatório para autorizar um site": "Motif obligatoire pour autoriser un site", "Domínio inválido": "Domaine invalide",
    "Este site não foi recusado nesta execução": "Ce site n'a pas été refusé lors de cette exécution",
    "Nenhuma consulta bloqueada foi autorizada desde então": "Aucune consultation bloquée n'a été autorisée depuis",
    "Se for um site oficial legítimo, acrescente-o à lista branca kyc_agent/dominios_oficiais.json (com o motivo) e relance ou retome a atualização. Caso contrário, nada a fazer: a consulta correspondente aparece como tarefa humana.":
        "S'il s'agit d'un site officiel légitime, ajoutez-le à la liste blanche kyc_agent/dominios_oficiais.json (avec le motif), puis relancez ou reprenez la mise à jour. Sinon, rien à faire : la consultation correspondante apparaît comme tâche humaine.",
    # reprise d'une mise à jour, durée de travail de l'agent
    "Retomar a atualização": "Reprendre la mise à jour", "Retomar": "Reprendre", "Recomeçar do zero": "Recommencer à zéro",
    "O trabalho do agente foi guardado: você pode retomar a atualização.": "Le travail de l'agent a été mis de côté : vous pouvez reprendre la mise à jour.",
    "A atualização anterior parou": "La mise à jour précédente s'est arrêtée",
    "O trabalho já feito pelo agente foi guardado": "Le travail déjà fait par l'agent a été mis de côté",
    "duração": "durée",
    "Retomar: o agente reencontra seu trabalho e sua conversa e continua de onde parou. Recomeçar: o trabalho guardado é descartado e o agente recomeça do zero.":
        "Reprendre : l'agent retrouve son travail et sa conversation et continue là où il s'était arrêté. Recommencer : le travail mis de côté est abandonné et l'agent repart de zéro.",
    "O dossiê será restaurado no estado anterior à execução e os documentos continuarão aguardando tratamento. O trabalho já feito pelo agente é guardado: você poderá retomar a atualização. Os tokens já consumidos permanecem faturados.":
        "Le dossier sera remis dans l'état d'avant l'exécution et les documents resteront en attente de traitement. Le travail déjà fait par l'agent est mis de côté : vous pourrez reprendre la mise à jour. Les tokens déjà consommés restent facturés.",
    "Esta atualização não pode mais ser retomada": "Cette mise à jour ne peut plus être reprise",
    "Duração do trabalho do agente": "Durée de travail de l'agent", "Duração do agente": "Durée de l'agent",
    "Tempo de trabalho do agente nesta atualização": "Temps de travail de l'agent pour cette mise à jour",
    "retomada": "reprise",
    "Comando em segundo plano concluído": "Commande en arrière-plan terminée",
    "Identificador do dossiê": "Identifiant du dossier", "Nome do dossiê": "Nom du dossier",
    "Letras sem acento, números, ponto, traço e sublinhado; sem espaços (ex.: TC-187-TESTE)":
        "Lettres sans accent, chiffres, point, tiret et tiret bas ; pas d'espace (ex. : TC-187-TEST)",
    "Identificador inválido (letras, números, . _ -)": "Identifiant invalide : lettres sans accent, chiffres, . _ - uniquement, sans espace",
    # erreurs du serveur (message exact, ou préfixe avant « : »)
    "Caminho inválido": "Chemin invalide", "Caminho duplicado": "Chemin en double",
    "Arquivo grande demais": "Fichier trop volumineux", "Pasta grande demais": "Dossier trop volumineux",
    "A pasta não contém documentos": "Le répertoire ne contient aucun document",
    "Nenhum arquivo recebido": "Aucun fichier reçu", "Lista de caminhos inválida": "Liste de chemins invalide",
    "Envio inválido": "Envoi invalide", "Lote grande demais": "Lot trop volumineux", "Pedaço grande demais": "Morceau trop volumineux",
    "Dossiê inexistente": "Dossier inexistant", "Confirmação ausente": "Confirmation manquante",
    "Nenhum documento a adicionar": "Aucun document à ajouter",
    "Nenhum documento aguardando tratamento": "Aucun document en attente de traitement",
    "Arquivo ZIP inválido ou corrompido": "Fichier ZIP invalide ou corrompu",
    "O ZIP não contém documentos": "Le ZIP ne contient aucun document",
    "Envio ZIP inválido: um único arquivo .zip é esperado": "Envoi ZIP invalide : un seul fichier .zip est attendu",
    "ZIP protegido por senha: não suportado. Descompacte-o você mesmo e escolha a pasta.":
        "ZIP protégé par mot de passe : non pris en charge. Décompressez-le vous-même et choisissez le répertoire.",
    "Todos os arquivos da pasta e das subpastas são importados como documentos do dossiê (subpastas conservadas). Arquivos de sistema (Thumbs.db, desktop.ini, .DS_Store) são ignorados.":
        "Tous les fichiers du répertoire et de ses sous-dossiers sont importés comme documents du dossier (sous-dossiers conservés). Les fichiers système (Thumbs.db, desktop.ini, .DS_Store) sont ignorés.",
    "Cancelar": "Annuler", "Criar o dossiê": "Créer le dossier", "arquivos": "fichiers", "Pasta": "Sous-dossier", "Envio": "Envoi",
    "Arquivo grande demais (máx.)": "Fichier trop volumineux (max.)", "Arquivos demais (máx.)": "Trop de fichiers (max.)",
    "Pasta grande demais (máx.)": "Répertoire trop volumineux (max.)", "Falha na criação": "Échec de la création",
    "Gravando o dossiê…": "Enregistrement du dossier…", "A pasta selecionada está vazia.": "Le répertoire sélectionné est vide.",
    "Dossiê em preparação": "Dossier en préparation",
    "Documentos importados; a análise do agente ainda não foi produzida.": "Documents importés ; l'analyse de l'agent n'a pas encore été produite.",
    "Próxima etapa: análise do agente (v1)": "Prochaine étape : analyse de l'agent (v1)", "Iniciar a análise (v1)": "Lancer l'analyse (v1)",
    "Botão disponível quando o agente estiver ligado à aplicação (Claude Agent SDK, conta API necessária). Enquanto isso, peça ao agente no Claude Code: «analisa o dossiê»":
        "Bouton disponible quand l'agent sera relié à l'application (Claude Agent SDK, compte API nécessaire). En attendant, demande à l'agent dans Claude Code : « analyse le dossier »",
    "Voltar ao painel do dossiê": "Retour au tableau de bord du dossier", "Documentos do dossiê": "Documents du dossier",
    "Pasta de origem": "Répertoire d'origine", "importada em": "importé le", "arquivo(s) de sistema ignorado(s)": "fichier(s) système ignoré(s)",
    "Filtrar…": "Filtrer…", "Arquivo": "Fichier", "Tamanho": "Taille", "Nenhum documento inventariado para este dossiê.": "Aucun document inventorié pour ce dossier.",
    "Documentos de origem do dossiê TC-187 (pasta data/files), inventariados após a análise; não copiados para o dossiê.":
        "Documents source du dossier TC-187 (répertoire data/files), inventoriés après l'analyse ; non copiés dans le dossier.",
    "validado por": "validé par", "Reabrir o dossiê": "Rouvrir le dossier", "Registra a validação humana final do dossiê.": "Enregistre la validation humaine finale du dossier.",
    "Marcar como tratado (validação humana)": "Marquer comme traité (validation humaine)",
    "Excluir": "Supprimer", "Excluir o dossiê": "Supprimer le dossier", "Dossiê excluído:": "Dossier supprimé :",
    "Atenção: todos os dados deste dossiê serão perdidos definitivamente.": "Attention : toutes les données de ce dossier seront définitivement perdues.",
    "documentos importados": "documents importés",
    "versões e livrables (relatório PDF, matriz XLSX, mensagens, ZIP de provas)": "versions et livrables (rapport PDF, matrice XLSX, messages, ZIP de preuves)",
    "respostas e anexos registrados, métricas e histórico": "réponses et pièces jointes enregistrées, métriques et historique",
    "Esta ação não pode ser desfeita.": "Cette action est irréversible.", "Excluir definitivamente": "Supprimer définitivement",
    "Fatura": "Facture", "não medido": "non mesuré", "n. medido": "non mesuré",
    "Custo acumulado dos tokens consumidos pelo agente neste dossiê (equivalente API, US$).":
        "Coût cumulé des tokens consommés par l'agent sur ce dossier (équivalent API, $).",
    "Criado há": "Créé il y a",
    "Atualizar o dossiê": "Mettre à jour le dossier", "documentos aguardando tratamento": "documents en attente de traitement",
    "Peças do dossiê": "Pièces du dossier", "documentos": "documents", "Adicionar peças ao dossiê": "Ajouter des pièces au dossier",
    "Documentos aguardados pelo agente": "Documents attendus par l'agent", "verificações": "vérifications",
    "respostas a mensagens": "réponses aux messages", "Respostas a mensagens": "Réponses aux messages",
    "Ver os documentos aguardados": "Voir les documents attendus",
    "Acompanhamento detalhado (consumo, histórico, logs do agente)": "Suivi détaillé (consommation, historique, logs de l'agent)",
    "peça(s) do dossiê ainda não tratada(s)": "pièce(s) du dossier pas encore traitée(s)",
    "elemento(s) de resposta em Verificações": "élément(s) de réponse dans Vérifications",
    "elemento(s) de resposta em Mensagens": "élément(s) de réponse dans Messages",
    "Peças do dossiê ainda não tratadas": "Pièces du dossier pas encore traitées",
    "O lançamento direto pela aplicação estará disponível quando o agente estiver ligado (Claude Agent SDK, conta API necessária). Enquanto isso, peça ao agente no Claude Code: «trata o dossiê»":
        "Le lancement direct depuis l'application sera disponible quand l'agent sera relié (Claude Agent SDK, compte API nécessaire). En attendant, demande à l'agent dans Claude Code : « traite le dossier »",
    "Fechar": "Fermer", "Nenhuma.": "Aucune.", "Falha no envio": "Échec de l'envoi", "Gravando…": "Enregistrement…",
    "Cérebro do agente": "Cerveau de l'agent", "Nenhuma atualização em curso": "Aucune mise à jour en cours",
    "Entrada": "Entrée", "Saída": "Sortie", "Total": "Total",
    "Tokens da atualização atual do dossiê": "Tokens de la mise à jour en cours du dossier",
    "Tokens do projeto": "Tokens du projet", "Fatura atual do projeto": "Facture actuelle du projet",
    "Equivalente à tarifa Claude API": "Équivalent au tarif Claude API", "sem as versões não medidas": "hors versions non mesurées",
    "Acompanhamento detalhado (histórico, logs do agente)": "Suivi détaillé (historique, logs de l'agent)",
    "Demonstração da animação — nenhuma atualização real em curso": "Démonstration de l'animation — aucune mise à jour réelle en cours",
    "Fatura por atualização": "Facture par mise à jour", "Tokens por atualização": "Tokens par mise à jour", "Tabela": "Tableau", "tentativa sem versão": "tentative sans version", "Início": "Début", "Duração": "Durée", "tarefas do plano do agente": "tâches du plan de l'agent",
    "Chave API Anthropic": "Clé API Anthropic", "Chave API": "Clé API", "Criar uma chave": "Créer une clé",
    "Nenhuma chave API está registrada neste computador. Sem ela, o agente funciona apenas em modo simulação.":
        "Aucune clé API n'est enregistrée sur cet ordinateur. Sans elle, l'agent ne fonctionne qu'en mode simulation.",
    "A chave é verificada por uma troca muito curta com o modelo (fração de centavo, nenhum dado de dossiê) e guardada apenas neste computador (variável de usuário do Windows KYC_ANTHROPIC_API_KEY). Ela nunca é exibida.":
        "La clé est vérifiée par un échange très court avec le modèle (fraction de centime, aucune donnée de dossier) et enregistrée uniquement sur cet ordinateur (variable utilisateur Windows KYC_ANTHROPIC_API_KEY). Elle n'est jamais affichée.",
    "Mais tarde (simulação)": "Plus tard (simulation)", "Verificar e registrar": "Vérifier et enregistrer",
    "Verificação em curso…": "Vérification en cours…", "Erro de comunicação com a aplicação.": "Erreur de communication avec l'application.",
    "Chave registrada neste computador": "Clé enregistrée sur cet ordinateur",
    "Não foi possível testá-la agora (rede?): ela será usada na próxima atualização.": "Elle n'a pas pu être testée maintenant (réseau ?) : elle sera utilisée à la prochaine mise à jour.",
    "Chave recusada pela Anthropic: verifique a cópia ou crie uma nova chave.": "Clé refusée par Anthropic : vérifiez le copier-coller ou créez une nouvelle clé.",
    "Chave válida, mas sem créditos: adicione créditos (Billing) no console Anthropic.": "Clé valide mais sans crédits : ajoutez des crédits (Billing) dans la console Anthropic.",
    "O modelo do agente não está acessível com esta chave (organização / workspace).": "Le modèle de l'agent n'est pas accessible avec cette clé (organisation / workspace).",
    "Formato inválido: uma chave API começa por sk-ant- (sem espaço).": "Format invalide : une clé API commence par sk-ant- (sans espace).", "MODO SIMULAÇÃO — nenhuma chamada à API, nenhuma versão criada": "MODE SIMULATION — aucun appel à l'API, aucune version créée",
    "Curvas por atualização a partir da primeira atualização medida.": "Courbes par mise à jour dès la première mise à jour mesurée.",
    "Nenhuma atualização medida até agora. O gráfico aparecerá a partir da primeira atualização feita pelo agente ligado à aplicação.":
        "Aucune mise à jour mesurée pour l'instant. Le graphique apparaîtra dès la première mise à jour faite par l'agent relié à l'application.",
    "peças do dossiê": "pièces du dossier",
    "peças": "pièces", "mensagens": "messages", "Número de agentes para realizar a tarefa": "Nombre d'agents pour réaliser la tâche",
    "Vários agentes repartem entre si os documentos a tratar: a atualização é mais rápida, o consumo de tokens um pouco maior.":
        "Plusieurs agents se répartissent les documents à traiter : la mise à jour est plus rapide, la consommation de tokens un peu plus élevée.",
    "Atualização solicitada": "Mise à jour demandée", "agente(s)": "agent(s)",
    "A execução automática pela aplicação ainda não está ligada (Claude Agent SDK). Enquanto isso, peça ao agente no Claude Code: «trata o dossiê»":
        "L'exécution automatique par l'application n'est pas encore branchée (Claude Agent SDK). En attendant, demande à l'agent dans Claude Code : « traite le dossier »",
    "Concluído": "Terminé", "Marcar como concluído": "Marquer le dossier comme terminé",
    "Histórico das versões": "Historique des versions", "versão(ões)": "version(s)",
    "O dossiê voltará ao status «Em instrução».": "Le dossier reviendra au statut « En cours d'instruction ».",
    "Registra a validação humana final do dossiê (versão": "Enregistre la validation humaine finale du dossier (version",
    "Baixar o relatório PDF": "Télécharger le rapport PDF", "Resumo da versão": "Résumé de la version",
    "Atualizações efetuadas": "Mises à jour effectuées", "Tarefas e observações do agente": "Tâches et remarques de l'agent",
    "Encerrar o dossiê (validação)": "Clôturer le dossier (validation)", "Comentários": "Commentaires",
    "O status passará a «Concluído».": "Le statut passera à « Terminé ».",
    'O agente ainda não começou.': "L'agent n'a pas encore démarré.",
    'Atualização concluída': 'Mise à jour terminée',
    'Releia e valide a nova versão antes de qualquer envio.': 'Relisez et validez la nouvelle version avant tout envoi.',
    'Resumo do agente': "Résumé de l'agent",
    'Simulação concluída': 'Simulation terminée',
    'Nenhuma chave API (KYC_ANTHROPIC_API_KEY) configurada: nenhuma chamada à API, nenhuma versão criada, dossiê inalterado.': "Aucune clé API (KYC_ANTHROPIC_API_KEY) configurée : aucun appel à l'API, aucune version créée, dossier inchangé.",
    'Falha da atualização': 'Échec de la mise à jour',
    'Nada foi marcado como tratado; os documentos continuam aguardando.': "Rien n'a été marqué comme traité ; les documents restent en attente.",
    '1 agente trabalhando': '1 agent au travail',
    'agentes trabalhando': 'agents au travail',
    'principal': 'principal',
    'subagente': 'sous-agent',
    'subagentes': 'sous-agents',
    'O agente decide sozinho se reparte o trabalho entre vários subagentes em paralelo; o número de agentes trabalhando aparece durante a execução.': "L'agent décide seul s'il répartit le travail entre plusieurs sous-agents en parallèle ; le nombre d'agents au travail s'affiche pendant l'exécution.",
    'Agentes': 'Agents',
    'até': "jusqu'à",
    'em paralelo': 'en parallèle',
    'subagente(s) lançado(s)': 'sous-agent(s) lancé(s)',
    'Agente trabalhando': 'Agent au travail',
    'Uma atualização do dossiê está em execução': "Une mise à jour du dossier est en cours d'exécution",
    'O dossiê foi restaurado no estado anterior à execução; os documentos continuam aguardando tratamento.': "Le dossier a été remis dans l'état d'avant l'exécution ; les documents restent en attente de traitement.",
    'Atenção — peças ou respostas existentes modificadas durante a execução (verificar):': "Attention — pièces ou réponses existantes modifiées pendant l'exécution (à vérifier) :",
    'Dossiê concluído': 'Dossier terminé',
    'Para atualizá-lo ou retomar a análise, reabra o dossiê.': "Pour le mettre à jour ou reprendre l'analyse, rouvrez le dossier.",
    'Dossiê concluído: reabra-o para atualizá-lo': 'Dossier terminé : rouvrez-le pour le mettre à jour',
    'Pausar os agentes': 'Mettre en pause les agents', 'Pausar': 'Mettre en pause',
    'Disponível após a primeira análise do agente': 'Disponible après la première analyse de l''agent',
    'Pausa solicitada…': 'Mise en pause demandée…',
    'O trabalho em curso será perdido e o dossiê restaurado no estado anterior à execução. Os documentos continuarão aguardando tratamento. Os tokens já consumidos permanecem faturados.': "Le travail en cours sera perdu et le dossier remis dans l'état d'avant l'exécution. Les documents resteront en attente de traitement. Les jetons déjà consommés restent facturés.",
    'Atualização interrompida': 'Mise à jour arrêtée',
    'Nada foi alterado; os documentos continuam aguardando tratamento.': "Rien n'a été modifié ; les documents restent en attente de traitement.",
    'Histórico': 'Historique',
    'concluído': 'terminé',
    'reaberto': 'rouvert',
    'por': 'par',
    # sauvegarde automatique
    "Salvaguarda automática": "Sauvegarde automatique", "desativada": "désactivée", "última verificação há": "dernière vérification il y a",
    "dossiê(s) salvo(s)": "dossier(s) sauvegardé(s)", "erro(s)": "erreur(s)", "primeira salvaguarda em alguns minutos": "première sauvegarde dans quelques minutes",
    "a cada": "toutes les", "A cada": "Toutes les", "Ver as salvaguardas": "Voir les sauvegardes", "no mesmo disco que os dossiês": "sur le même disque que les dossiers",
    "salvaguardas automáticas do dossiê": "sauvegardes automatiques du dossier", "Salvaguardas": "Sauvegardes", "Voltar aos dossiês": "Retour aux dossiers",
    "Salvaguardas automáticas": "Sauvegardes automatiques", "Salvaguarda iniciada: atualize a página em alguns instantes.": "Sauvegarde lancée : actualisez la page dans quelques instants.",
    "cada dossiê modificado é copiado (estado, versões, entregáveis, respostas, diários). As peças são guardadas uma única vez, identificadas pela impressão digital. As":
        "chaque dossier modifié est copié (état, versions, livrables, réponses, journaux). Les pièces sont stockées une seule fois, identifiées par leur empreinte. Les",
    "cópias mais recentes de cada dossiê são conservadas. Nenhuma cópia durante uma execução do agente.":
        "copies les plus récentes de chaque dossier sont conservées. Aucune copie pendant une exécution de l'agent.",
    "Local": "Emplacement",
    "As salvaguardas estão no mesmo disco que os dossiês: não protegem contra a perda ou o roubo do computador. Defina KYC_BACKUP_DIR para um disco externo ou um compartilhamento de rede salvo.":
        "Les sauvegardes sont sur le même disque que les dossiers : elles ne protègent pas contre la perte ou le vol de l'ordinateur. Définissez KYC_BACKUP_DIR vers un disque externe ou un partage réseau sauvegardé.",
    "Último ciclo": "Dernier passage", "sem alteração": "sans changement", "adiado(s) (agente em execução)": "reporté(s) (agent en cours)",
    "Salvar agora": "Sauvegarder maintenant", "cópia(s)": "copie(s)", "dossiê ausente da lista": "dossier absent de la liste", "Cópia": "Copie",
    "Tamanho (sem as peças)": "Taille (hors pièces)", "Restaurar como novo dossiê": "Restaurer comme nouveau dossier",
    "Restaurar esta cópia como um NOVO dossiê? O dossiê atual não é alterado.": "Restaurer cette copie comme un NOUVEAU dossier ? Le dossier actuel n'est pas modifié.",
    "Nenhuma salvaguarda ainda.": "Aucune sauvegarde pour l'instant.",
    # journal de l'agent
    "Diário do agente": "Journal de l'agent", "subagente": "sous-agent", "agente principal": "agent principal", "Diário completo do agente": "Journal complet de l'agent", "Abrir": "Ouvrir",
    "Versão produzida antes da criação do diário do agente": "Version produite avant la création du journal de l'agent",
    "Instruções dadas ao agente": "Consignes données à l'agent", "Etapa": "Étape", "Texto do agente": "Texte de l'agent",
    "Raciocínio": "Raisonnement", "Ferramenta usada": "Outil utilisé", "Resultado da ferramenta": "Résultat de l'outil",
    "Ação recusada": "Action refusée", "Subagente iniciado": "Sous-agent lancé", "Subagente concluído": "Sous-agent terminé",
    "Resultado final": "Résultat final", "Fim da execução": "Fin de l'exécution", "Execuções": "Exécutions",
    "Execução": "Exécution", "Eventos": "Événements", "Agente": "Agent", "Todos os agentes": "Tous les agents",
    "Baixar o diário (.jsonl)": "Télécharger le journal (.jsonl)", "Hora (UTC)": "Heure (UTC)", "Tipo": "Type",
    "Cadeia de impressões digitais verificada": "Chaîne d'empreintes vérifiée", "eventos": "événements",
    "Atenção: o diário foi alterado após a escrita (cadeia de impressões digitais rompida no evento":
        "Attention : le journal a été modifié après son écriture (chaîne d'empreintes rompue à l'événement",
    "Diário não disponível para esta execução (anterior à criação do diário).":
        "Journal non disponible pour cette exécution (antérieure à la création du journal).",
    "Nenhuma execução do agente neste dossiê.": "Aucune exécution de l'agent dans ce dossier.",
    "Tudo o que o agente recebeu, escreveu, pediu e obteve, e o que lhe foi recusado. Textos muito longos são truncados (tamanho e impressão digital do texto completo indicados). Contém dados do dossiê: uso interno.":
        "Tout ce que l'agent a reçu, écrit, demandé et obtenu, et ce qui lui a été refusé. Les textes très longs sont tronqués (taille et empreinte du texte complet indiquées). Contient des données du dossier : usage interne.",
    "Diário congelado com a versão": "Journal figé avec la version", "simulação": "simulation", "Mostrar as etapas resumidas": "Afficher les étapes résumées",
    "em curso": "en cours", "concluída": "terminée", "falhou": "échec", "interrompida": "arrêtée",
    # envoi par morceaux
    "lote": "lot", "resta ≈": "reste ≈", "Nova tentativa automática": "Nouvel essai automatique", "Retomar o envio": "Reprendre l'envoi",
    "Os arquivos já recebidos não serão reenviados.": "Les fichiers déjà reçus ne seront pas renvoyés.",
    "Escolher uma pasta": "Choisir un répertoire", "Escolher um arquivo ZIP": "Choisir un fichier ZIP", "Arquivo ZIP": "Fichier ZIP",
    "Enviar e analisar o ZIP": "Envoyer et analyser le ZIP", "Conteúdo do ZIP": "Contenu du ZIP", "subpastas": "sous-dossiers",
    "ignorados": "ignorés", "Análise do ZIP…": "Analyse du ZIP…", "Adicionar arquivos": "Ajouter des fichiers",
    "Adicionar um arquivo ZIP": "Ajouter un fichier ZIP", "Adicionar ao dossiê": "Ajouter au dossier",
    "ZIP internos não descompactados (mantidos como peças)": "ZIP internes non décompressés (gardés comme pièces)",
    "O ZIP é descompactado pela aplicação (subpastas conservadas); somente sua impressão digital é conservada. ZIP protegidos por senha ou com caminhos perigosos são recusados.":
        "Le ZIP est décompressé par l'application (sous-dossiers conservés) ; seule son empreinte est conservée. Les ZIP protégés par mot de passe ou contenant des chemins dangereux sont refusés.",
    # rapport
    "Pendências que condicionam o KYC": "Pendências qui conditionnent le KYC", "Pontos de DD": "Points de DD",
    "Riscos que bloqueiam o KYC": "Risques qui bloquent le KYC", "Partes registradas": "Parties enregistrées",
    "Acessos bloqueados (registro técnico)": "Accès bloqués (registre technique)",
    "Relatório KYC / KYB / AML — versão": "Rapport KYC / KYB / AML — version", "Abrir o PDF": "Ouvrir le PDF",
    "Baixar": "Télécharger", "Fundamentação": "Justification", "Caminho para conclusão:": "Chemin vers la conclusion :",
    "Escalonamento:": "Escalade :", "1.2 Partes": "1.2 Parties", "Parte": "Partie", "Identificação": "Identification",
    "Papéis": "Rôles", "Fonte do vínculo": "Source du lien", "Tratamento KYC": "Traitement KYC",
    "1.3 Controles": "1.3 Contrôles", "Filtrar controles…": "Filtrer les contrôles…", "Nível": "Niveau",
    "Controle": "Contrôle", "Partes": "Parties", "Estado": "État", "Execução": "Exécution",
    "Resultado → conclusão": "Résultat → conclusion", "Evid.": "Preuves",
    "1.4 Fluxos financeiros": "1.4 Flux financiers", "Pagador": "Payeur", "Recebedor": "Bénéficiaire",
    "Valor": "Montant", "Motivo": "Motif", "Base contratual": "Base contractuelle", "Natureza": "Nature",
    "1.5 Pontos já esclarecidos": "1.5 Points déjà éclaircis", "Ponto": "Point", "Conclusão": "Conclusion",
    "1.6 Riscos que subsistem": "1.6 Risques qui subsistent", "Risco": "Risque", "Fato / fonte": "Fait / source",
    "Medidas": "Mesures", "Residual": "Résiduel", "BLOQUEIA KYC": "BLOQUE LE KYC", "não bloqueia": "ne bloque pas",
    "Evento:": "Événement :", "Parte/fluxo:": "Partie/flux :", "Executado:": "Exécuté :", "Proposto:": "Proposé :",
    "1.7 O que ainda deve ser recebido": "1.7 Ce qui doit encore être reçu", "Condiciona o KYC": "Conditionne le KYC",
    "Due diligence (posterior)": "Due diligence (ultérieure)", "Item": "Point", "Complemento esperado": "Complément attendu",
    "Critério de fechamento": "Critère de clôture", "Responsável / etapa": "Responsable / étape",
    "Condições (§13.3)": "Conditions (§13.3)", "Já cumpridas": "Déjà remplies", "Necessárias ao KYC": "Nécessaires au KYC",
    "Antes de assinatura / closing": "Avant signature / closing",
    "Ações humanas — registro técnico (interno)": "Actions humaines — registre technique (interne)",
    "Verificações que o agente não pôde executar (captcha não contornado, base não fornecida, portal inacessível). Não são pedidos externos: cabe ao Compliance executá-las e devolver o resultado ao agente.":
        "Vérifications que l'agent n'a pas pu exécuter (captcha non contourné, base non fournie, portail inaccessible). Ce ne sont pas des demandes externes : il revient à Compliance de les exécuter et de renvoyer le résultat à l'agent.",
    "O que fazer": "Que faire", "A devolver ao agente": "À renvoyer à l'agent", "Impacto": "Impact",
    "ação Compliance": "action Compliance", "Abrir o site": "Ouvrir le site",
    "1.8 Revisão e autorização": "1.8 Revue et autorisation", "Etapa": "Étape", "Identidade": "Identité", "Data": "Date",
    "Revisão": "Revue", "Autorização": "Autorisation", "pendente": "en attente",
    "Journal de versões": "Journal des versions", "Pré-visualizar o PDF do relatório": "Prévisualiser le PDF du rapport",
    # matrice
    "Matriz operacional — versão": "Matrice opérationnelle — version", "Baixar o XLSX": "Télécharger le XLSX",
    "Conteúdo lido diretamente do arquivo XLSX entregue. A primeira aba contém somente as dependências externas; os acessos bloqueados ficam no registro técnico interno.":
        "Contenu lu directement dans le fichier XLSX livré. La première feuille ne contient que les dépendances externes ; les accès bloqués sont dans le registre technique interne.",
    "Filtrar…": "Filtrer…", "Nenhuma matriz nesta versão.": "Aucune matrice dans cette version.",
    # messages
    "Rascunhos para revisão.": "Brouillons pour revue.",
    "Nenhuma mensagem é enviada pela aplicação: Compliance relê, ajusta e envia pelo canal autorizado (guia v2 §2.3, §15.3). O botão «Abrir no e-mail» só prepara o rascunho no seu cliente de e-mail; os anexos devem ser juntados manualmente.":
        "Aucun message n'est envoyé par l'application : Compliance relit, ajuste et envoie par le canal autorisé (guide v2 §2.3, §15.3). Le bouton « Ouvrir dans l'e-mail » prépare seulement le brouillon dans votre messagerie ; les pièces jointes doivent être ajoutées manuellement.",
    "Abrir no e-mail": "Ouvrir dans l'e-mail", "Envio pelo BDR — sem botão de e-mail": "Envoi par le BDR — pas de bouton e-mail",
    "O cliente é contatado por intermédio do BDR: copie o texto e transmita-o ao BDR.": "Le client est contacté par l'intermédiaire du BDR : copiez le texte et transmettez-le au BDR.", "Baixar .md": "Télécharger .md", "Para:": "À :", "Assunto:": "Objet :",
    "Pendências cobertas:": "Pendências couvertes :", "Anexos": "Pièces jointes", "Anexo previsto": "Pièce jointe prévue",
    "Arquivo(s)": "Fichier(s)", "baixar": "télécharger", "ver mensagem": "voir le message",
    "a juntar manualmente — não disponível no dossiê": "à joindre manuellement — non disponible dans le dossier",
    "Nenhuma mensagem necessária nesta versão.": "Aucun message nécessaire dans cette version.",
    "Texto original": "Texte original",
    # preuves
    "Pacote de provas — versão": "Paquet de preuves — version", "Baixar o ZIP completo": "Télécharger le ZIP complet",
    "arquivos indexados (INDICE.csv do ZIP). Cada linha leva ao arquivo dentro do ZIP, com origem, descrição, seção que sustenta e SHA-256.":
        "fichiers indexés (INDICE.csv du ZIP). Chaque ligne mène au fichier dans le ZIP, avec origine, description, section justifiée et SHA-256.",
    "Filtrar por nome, pasta, descrição, ID…": "Filtrer par nom, dossier, description, ID…", "Arquivo": "Fichier",
    "Origem": "Origine", "Descrição": "Description", "Sustenta": "Justifie", "Tamanho": "Taille", "ausente": "absent",
    "Nenhum ZIP de provas nesta versão.": "Aucun ZIP de preuves dans cette version.",
    # note
    "Aucune note pour cette version.": "Aucune note pour cette version.",
}


def ui(lang: str):
    """Renvoie t(texte_pt) pour la langue demandée."""
    if lang == "fr":
        return lambda s: UI_FR.get(s, s)
    return lambda s: s


# ---------------------------------------------------------------- données
_RULES_FR = [(re.compile(r": descartada\b"), ": écarté"), (re.compile(r"\bCPF centro\b"), "CPF (centre)"),
             (re.compile(r"\bnão informado\b"), "non renseigné")]
SKIP_KEYS = {"id", "codigo", "arquivo", "arquivo_no_zip", "sha256", "link", "fonte", "fonte_original",
             "schema_version", "nota_fr"}


@lru_cache(maxsize=16)
def _dico(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dico(vdir: Path, lang: str) -> dict | None:
    if lang != "fr":
        return None
    p = vdir / "i18n_fr.json"
    return _dico(str(p), p.stat().st_mtime) if p.exists() else None


def tr_str(s: str, d: dict) -> str:
    if s in d:
        return d[s]
    for sep in (" | ", " — "):
        if sep in s:
            parts = s.split(sep)
            out = [tr_str(p, d) for p in parts]
            if out != parts:
                return sep.join(out)
    out = s
    for rx, rep in _RULES_FR:
        out = rx.sub(rep, out)
    return out


def tr_data(o, d: dict | None, key: str = ""):
    if not d:
        return o
    if isinstance(o, dict):
        return {k: (v if k in SKIP_KEYS else tr_data(v, d, k)) for k, v in o.items()}
    if isinstance(o, list):
        return [tr_data(v, d, key) for v in o]
    if isinstance(o, str):
        return tr_str(o, d)
    return o
