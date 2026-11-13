# Application complète (interface + agent Claude Agent SDK + captures Playwright) pour Render.
# Les dossiers clients ne sont PAS dans l'image : ils vivent sur le disque persistant monté sur /data.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/navigateurs \
    KYC_AGENT_MAX_USD=30

WORKDIR /app
COPY requirements.txt requirements-web.txt ./
# libatomic1 : requis par le Node.js embarqué de Playwright (absent de l'image slim)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --root-user-action=ignore --no-cache-dir -r requirements.txt -r requirements-web.txt \
 && python -m playwright install --with-deps chromium-headless-shell \
 && rm -rf /var/lib/apt/lists/*

# Code et guides de référence uniquement (voir .dockerignore : ni cases/, ni sauvegardes/, ni data/files/)
COPY webapp ./webapp
COPY kyc_agent ./kyc_agent
COPY data/format_output.txt data/job_guidelines.pdf data/job_guidelines_v2.pdf ./data/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# L'application (et l'agent qu'elle lance) tourne sous un compte sans privilèges
RUN useradd --create-home --uid 1000 kyc \
 && chmod 755 /usr/local/bin/docker-entrypoint.sh \
 && chmod -R a+rX /app /opt/navigateurs

EXPOSE 10000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn webapp.app:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
