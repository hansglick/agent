#!/bin/sh
# Prépare le disque persistant (/data) puis lance l'application sous le compte « kyc » (sans privilèges).
set -e
mkdir -p "$DATA_DIR/cases"
# le disque Render est monté par root : on le confie au compte de l'application (une seule fois si déjà fait)
if [ "$(stat -c %u "$DATA_DIR")" != "1000" ] || [ "$(stat -c %u "$DATA_DIR/cases")" != "1000" ]; then
  chown -R kyc:kyc "$DATA_DIR"
fi
export HOME=/home/kyc
exec runuser -u kyc -- "$@"
