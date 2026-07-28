#!/bin/bash
# Bilan compact de la nuit : ce qu'il faut regarder d'un coup d'oeil pour
# savoir si le bot travaille ou s'il tourne a vide.
cd "$(dirname "$0")" || exit 1

echo "=== $(date '+%H:%M:%S') ==="
for p in run_forever coc_attack.py watch.py; do
    pgrep -f "$p" >/dev/null && echo "  $p : vivant" || echo "  $p : ABSENT"
done

echo "  remparts ameliores : $(grep -c 'rempart ameliore' run.log)"
echo "  attaques lancees   : $(grep -c '^#* Attaque' run.log)"
plein=$(grep -oP '^\[\+\] \K\d+/\d+(?= slots vides)' run.log \
        | awk -F/ '{t++; if ($1==$2) c++} END {printf "%d/%d", c, t}')
echo "  deploiements complets : $plein"
echo "  murs au maximum    : $(grep -c 'au maximum, ecarte' run.log)"

alertes=$(grep -c '^\[!\]' run.log)
echo "  ALERTES            : $alertes"
[ "$alertes" -gt 0 ] && grep '^\[!\]' run.log | tail -3

# Un ecran inconnu inedit merite un oeil : c'est la seule trace d'une fenetre
# que le programme n'a jamais rencontree.
inedits=$(ls -t unknown 2>/dev/null | sed 's/^[0-9-]*-//; s/\.png$//' | sort -u | tr '\n' ' ')
echo "  types d'ecrans inconnus : $inedits"

echo "  --- dernieres lignes ---"
tail -6 run.log | sed 's/^/  /'
