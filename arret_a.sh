#!/bin/bash
# Arrete le bot une fois un nombre d'ameliorations atteint.
#
# Le joueur compte les remparts qui lui restent, pas ceux que le programme a
# montes : on part donc du compte actuel et on ajoute le sien. Une seconde
# condition d'arret evite d'attendre indefiniment si les murs viennent a
# manquer avant le compte.
cd "$(dirname "$0")" || exit 1

CIBLE=${1:?usage: arret_a.sh <nombre total d ameliorations>}

compte() { grep -c 'rempart ameliore' run.log; }
compte_vides() { grep -c 'plus de rempart a ameliorer' run.log; }

# Releve de depart, pris avant la boucle : comparer a zero faisait prendre les
# occurrences deja au journal pour des nouvelles, et l'arret tombait aussitot.
VIDES=$(compte_vides)
echo "arret prevu a $CIBLE ameliorations (actuellement $(compte),"
echo "  $VIDES annonces de vivier vide deja au journal)"

while true; do
    n=$(compte)
    if [ "$n" -ge "$CIBLE" ]; then
        echo "OBJECTIF ATTEINT : $n ameliorations"
        break
    fi
    # Le programme le dit lui-meme quand il ne trouve plus rien a monter.
    # Trois phases de suite sans candidat valent un arret : inutile d'attaquer
    # pour un vivier vide.
    if [ "$(compte_vides)" -ge $((VIDES + 3)) ]; then
        echo "PLUS DE REMPART A MONTER : arret a $n ameliorations"
        break
    fi
    sleep 30
done

# Le superviseur d'abord, sinon il relance le programme qu'on vient d'arreter.
pkill -f 'run_forever.sh'
sleep 1
pkill -f 'python3 -u coc_attack.py'
pkill -f 'watch.py'
sleep 2
echo "arrete a $(date '+%H:%M:%S') | $(compte) ameliorations, $(grep -c '^#* Attaque' run.log) attaques"
ps -eo args | grep -E 'coc_attack|run_forever|watch.py' | grep -v grep && echo "ATTENTION : processus encore vivant" || echo "tout est arrete"
