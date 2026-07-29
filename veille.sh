#!/bin/bash
# Veille de nuit : un bilan toutes les quinze minutes, et tout de suite ce qui
# demande une decision - une alerte, un processus mort, un ecran jamais vu.
cd "$(dirname "$0")" || exit 1

INTERVALLE=${INTERVALLE:-900}
# On part de l'existant : ne signaler que ce qui arrive a partir de maintenant.
alertes_vues=$(grep -c '^\[!\]' run.log)
types_vus=$(ls unknown 2>/dev/null | sed 's/^[0-9-]*-//; s/\.png$//' | sort -u)

while true; do
    # Ce qui ne peut pas attendre le prochain bilan.
    # Le programme s'arrete de lui-meme au bout de sa serie, et le superviseur
    # observe une pause avant de relancer : une absence ponctuelle est normale.
    # On ne crie qu'apres l'avoir constatee plusieurs fois de suite.
    for p in run_forever coc_attack.py; do
        absent=1
        for _ in 1 2 3 4 5; do
            if pgrep -f "$p" >/dev/null; then absent=0; break; fi
            sleep 25
        done
        [ "$absent" = 1 ] && echo "URGENT $p absent depuis plus de deux minutes"
    done

    n=$(grep -c '^\[!\]' run.log)
    if [ "$n" -gt "$alertes_vues" ]; then
        grep '^\[!\]' run.log | tail -n $((n - alertes_vues)) | sed 's/^/URGENT /'
        alertes_vues=$n
    fi

    types=$(ls unknown 2>/dev/null | sed 's/^[0-9-]*-//; s/\.png$//' | sort -u)
    nouveau=$(comm -13 <(echo "$types_vus") <(echo "$types"))
    [ -n "$nouveau" ] && echo "URGENT ecran jamais vu : $(echo $nouveau)"
    types_vus=$types

    # Le bilan periodique.
    echo "BILAN $(date '+%H:%M') | attaques $(grep -c '^#* Attaque' run.log)" \
         "| remparts $(grep -c 'rempart ameliore' run.log)" \
         "| maxes ecartes $(grep -c 'au maximum, ecarte' run.log)" \
         "| echecs selection $(grep -c 'non selectionne' run.log)" \
         "| $(grep 'reserves :' run.log | tail -1 | sed 's/\[i\] //')"

    sleep "$INTERVALLE"
done
