#!/usr/bin/env python3
"""Chien de garde pour une serie d'attaques.

Suit run.log et ne signale que ce qui merite une intervention. Filtrer sur
"Error" ne suffit pas : les pannes les plus genantes sont silencieuses, comme
un deploiement qui ne vide plus les slots, des remparts qui n'avancent plus,
ou un programme qui se fige sans rien ecrire. Chacune de ces situations est
donc surveillee explicitement.

    python3 watch.py [run.log]
"""

import os
import re
import subprocess
import sys
import time

LOG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "run.log")

SILENCE_MAX = 480        # secondes sans nouvelle ligne avant de crier
ZEROS_REMPARTS = 4       # attaques d'affilee sans rempart avant de signaler
RESUME_TOUS = 10         # frequence des points de situation, en attaques


def programme_tourne():
    """Le programme est-il en cours ?

    On ne cherche pas la ligne de commande exacte : en se relancant pour
    prendre une correction, le programme reconstruit ses arguments dans un
    autre ordre. Chercher "coc_attack.py --rounds" tel quel faisait alors
    croire a un arret alors qu'il tournait toujours.
    """
    out = subprocess.run(["ps", "-eo", "pid,cmd"], capture_output=True, text=True).stdout
    for ligne in out.splitlines():
        if "grep" in ligne or "watch.py" in ligne:
            continue
        if "coc_attack.py" in ligne and "--rounds" in ligne:
            return True
    return False


def emet(msg):
    print(msg, flush=True)


def main():
    attaque = 0
    zeros = 0
    faites = 0
    troupes_ok = 0
    remparts = 0
    derniere_ligne = time.time()
    silence_signale = False
    absences = 0

    with open(LOG, "r", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        while True:
            ligne = f.readline()

            if not ligne:
                # Rien de neuf : le programme est-il encore la, et avance-t-il ?
                if programme_tourne():
                    absences = 0
                else:
                    # Le programme se remplace lui-meme quand il recharge une
                    # correction : il est brievement invisible. On ne conclut
                    # a l'arret qu'apres plusieurs constats.
                    absences += 1
                    if absences >= 5:
                        emet(f"PROGRAMME ARRETE apres {faites} attaque(s) terminee(s) "
                             f"({troupes_ok} deploiements complets, "
                             f"{remparts} remparts)")
                        return
                mute = time.time() - derniere_ligne
                if mute > SILENCE_MAX and not silence_signale:
                    emet(f"FIGE : aucune ligne depuis {int(mute // 60)} min "
                         f"(attaque {attaque})")
                    silence_signale = True
                time.sleep(2)
                continue

            derniere_ligne = time.time()
            silence_signale = False
            ligne = ligne.rstrip()

            m = re.match(r"#+ Attaque (\d+)/", ligne)
            if m:
                attaque = int(m.group(1))
                if attaque > 1 and (attaque - 1) % RESUME_TOUS == 0:
                    emet(f"point de situation : {faites} attaques, "
                         f"{troupes_ok} deploiements complets, {remparts} remparts")
                continue

            m = re.search(r"\[\+\] (\d+)/(\d+) slots vides", ligne)
            if m:
                vides, total = int(m.group(1)), int(m.group(2))
                faites += 1
                if vides == total:
                    troupes_ok += 1
                else:
                    emet(f"attaque {attaque} : seulement {vides}/{total} slots "
                         f"deployes, des troupes sont restees au depot")
                continue

            m = re.search(r"\[\+\] (\d+) rempart", ligne)
            if m:
                n = int(m.group(1))
                remparts += n
                zeros = zeros + 1 if n == 0 else 0
                if zeros == ZEROS_REMPARTS:
                    emet(f"{zeros} attaques d'affilee sans ameliorer un rempart "
                         f"(attaque {attaque})")
                continue

            # Tout ce qui signale un imprevu est relaye tel quel.
            if re.search(r"ecran inconnu archive|Traceback|Error|Exception|"
                         r"\[!\]|aucune fenetre de confirmation|"
                         r"^Termine|code mis a jour", ligne):
                emet(f"attaque {attaque} : {ligne.strip()}")


if __name__ == "__main__":
    main()
