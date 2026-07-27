#!/bin/bash
# Relance le bot indefiniment.
#
# Le programme s'arrete de lui-meme au bout de sa serie, ou apres cinq echecs
# consecutifs. Sans superviseur, le telephone resterait inactif jusqu'a la
# prochaine intervention : ici il repart, apres une pause laissant le temps a
# un probleme passager (ADB, jeu ferme) de se dissiper.
cd "$(dirname "$0")" || exit 1

SERIE=${SERIE:-50}
PAUSE=${PAUSE:-60}

while true; do
    echo "===== nouvelle serie de $SERIE attaques ($(date '+%H:%M:%S')) ====="
    python3 -u coc_attack.py --rounds "$SERIE" --walls 5
    code=$?
    echo "===== serie terminee (code $code), reprise dans ${PAUSE}s ====="
    sleep "$PAUSE"
done
