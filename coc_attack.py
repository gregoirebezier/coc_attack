#!/usr/bin/env python3
"""Clash of Clans : lance des attaques et deploie toutes les troupes sur un cote du village.

Pilote un telephone Android via ADB.

Points cles :

  - Zone rouge. Le jeu refuse les largages trop pres des batiments, et la
    bordure rouge est trop fine et translucide pour etre detectee de facon
    fiable a l'image. On la trouve donc en la sondant : on tape un point, et
    si le compteur de la carte de troupe ne bouge pas, c'est que le point est
    interdit. Un tap refuse ne coute rien (aucune troupe perdue), donc le
    sondage est gratuit. Seuls les points valides sont ensuite utilises.

  - Butin. Le butin affiche en haut a gauche est lu par OCR ; les villages
    trop pauvres sont passes avec le bouton "Suivant".

  - Fin de combat. On laisse le combat se terminer tout seul : les troupes
    ont besoin de temps pour casser les remparts. "Terminer la bataille"
    n'est utilise qu'en dernier recours (ou avec --end-early).

Usage :
    python3 coc_attack.py --rounds 10
    python3 coc_attack.py --side right --min-loot 900000
    python3 coc_attack.py --probe             # deploie puis laisse le combat ouvert
"""

import argparse
import json
import os
import random
import math
import re
import struct
import subprocess
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(HERE, "templates")

# Resolution de reference (Pixel 6 en paysage). Tout est exprime dedans, puis
# mis a l'echelle si le telephone a une autre definition.
REF_W, REF_H = 2400, 1080

# Region a comparer pour reconnaitre chaque ecran, et point a taper ensuite.
SCREENS = {
    "home":    {"box": (150, 880, 320, 1050),   "tap": (234, 966)},   # "Attaquer"
    "menu":    {"box": (330, 745, 545, 840),    "tap": (436, 792)},   # "Trouver une partie"
    "army":    {"box": (1870, 940, 2045, 1012), "tap": (1956, 976)},  # "Attaquer" vert
    # Le bouton rouge en bas a gauche change de libelle une fois les troupes
    # posees : "Terminer la bataille" devient "Capituler". Meme position, mais
    # deux images differentes (57.8 d'ecart), d'ou les deux variantes.
    "battle":  {"box": (150, 775, 350, 845),    "tap": (246, 808),
                "templates": ["battle", "battle_capituler"]},
    "result":  {"box": (1120, 890, 1285, 962),  "tap": (1200, 924)},  # "Rentrer"
}
MATCH_THRESHOLD = 25.0   # score max pour considerer un ecran reconnu

NEXT_BUTTON = (2120, 770)  # "Suivant" : passe au village suivant

# Barre de troupes, en bas de l'ecran.
SLOT_ROW_Y = (1050, 1064)   # lignes ou le cadre bas des cartes ressort
SLOT_TAP_Y = 975            # ou taper pour selectionner une carte
SLOT_CARD_Y = (900, 1050)   # zone de carte servant a mesurer l'etat
SLOT_MIN_W, SLOT_MAX_W = 70, 140
SLOT_ACTIVE_SAT = 0.15      # au-dessus = carte encore utilisable

# Badge "xNN" en haut a droite de la carte. Les troupes et les sorts en ont un,
# les heros et les machines de siege non. Mesures reelles : troupes 37 %,
# sorts 32-39 %, heros 0.3-5.4 %, siege 1.6 %.
BADGE_BOX = (10, 48, 902, 934)   # dx0, dx1, y0, y1 autour du centre de carte
BADGE_MIN = 15.0
TITRE_SEUILS = (150, 180, 210, 240)   # clartes essayees pour lire un titre
# Difference min. prouvant qu'une unite est sortie. Mesures sur captures
# reelles : bruit d'animation 0.0-2.1, une seule unite sortie (x50 -> x49)
# ~11, carte videe 60-88. On se cale juste au-dessus du bruit, sinon le
# sondage (qui ne sort qu'une unite a la fois) ne detecte plus rien.
CARD_CHANGED_MAD = 4.0
IDLE_ABANDON = 4         # passes sans effet avant d'abandonner un slot

PLAY_AREA = (0, 0, 2400, 870)     # zone de jeu, au-dessus de la barre de troupes
# Zone de largage des sorts : large, pour les disperser sur tout le village
# plutot que de les empiler au centre.
SPELL_ZONE = (720, 170, 1800, 730)

# Boutons poses par-dessus la carte : y larguer une troupe revient a cliquer
# dessus. "Terminer la bataille" en bas a gauche est le plus dangereux, il
# interrompt l'attaque ; "Suivant" en bas a droite coute de l'or et change de
# village. Aucun point de largage ne doit tomber dedans.
UI_ZONES = [
    (110, 745, 390, 870),    # "Terminer la bataille" / "Capituler"
    (390, 755, 930, 870),    # icones "Armee boostee" et "Heros boostes"
    (1945, 680, 2320, 870),  # "Suivant"
    (1945, 0, 2400, 225),    # ressources en haut a droite
    (0, 0, 540, 120),        # nom du defenseur
]

# Fenetre "Il y a quelqu'un ?", affichee apres une longue inactivite. Elle
# laisse le village visible derriere elle, si bien que la reconnaissance le
# voit toujours : le programme croirait etre au village et cliquerait dans le
# vide indefiniment. On la reconnait a son panneau sombre - 96 % de pixels
# sombres contre au plus 37 % sur tout autre ecran - puis on confirme par son
# texte, avant de recharger le jeu.
IDLE_PANEL = (560, 330, 2010, 750)
IDLE_TEXT_BOX = (580, 380, 1700, 560)
IDLE_DARK_MIN = 70.0
IDLE_RELOAD = (728, 674)          # "Recharger le jeu"

# Compteur de gemmes. Aucune action du programme ne doit en depenser : une
# recherche de rempart tombee sur le bouton "+" du bandeau a suffi a en couter
# sept cent quarante-trois. On le releve donc avant et apres chaque passage sur
# les remparts, et la moindre baisse coupe la fonctionnalite pour de bon.
GEM_BOX = (2000, 335, 2210, 392)
# Reserves du village, en haut a droite. Leur montee continue alors qu'aucun
# rempart ne s'ameliore est le signe d'une panne silencieuse : le programme
# attaque et ramene du butin, mais ne trouve plus de mur ou ne parvient plus a
# le selectionner. Rien dans le journal ne le trahit autrement.
# La case va jusqu'a 2185 : a 2170, un montant a sept chiffres larges debordait
# et perdait le dernier, 5 668 572 se lisant 566 857. Les icones de ressource
# ne genent pas, leur jaune et leur magenta n'ayant pas les trois composantes
# claires qu'exige le masque des chiffres.
STOCK_LINES = {"or": (1975, 42, 2170, 84), "elixir": (1975, 146, 2170, 188)}
STOCK_BORDS = (2170, 2185)   # bords droits essayes pour la case des chiffres
PRIX_ROUGE_MAX = 4       # prix rouges avant de juger une reserve insuffisante
# Les chiffres sont blancs, mais la barre de ressources se detache sur le decor
# du village. Un crane pale y a suffi : a cent soixante-dix, son gris passait
# pour du texte, cinquante-neuf pour cent de la case s'allumait et les chiffres
# s'y noyaient - l'or devenait illisible, l'elixir perdait son premier chiffre.
# Facons d'isoler les chiffres, essayees dans cet ordre. Les clartes absolues
# suffisent sur un fond uni. Les deux dernieres travaillent au contraste local,
# seul recours quand le fond change au milieu du nombre : la barre de ressource
# se remplit, et sa portion pleine s'arrete parfois entre deux chiffres - les
# premiers sur clair, les derniers sur sombre. Le petit voisinage suit ce
# changement de plus pres que le grand.
STOCK_SEUILS = (200, 215, 230, 245, (41, -12), (15, -16))
# On n'ouvre la phase des remparts qu'a partir de vingt millions dans l'une des
# deux reserves ; en dessous, on enchaine les attaques. Choix du joueur :
# accumuler puis depenser d'un coup, plutot que de payer une minute de phase a
# chaque attaque pour un mur ou deux. Vingt millions valent trois remparts a
# six, et les reserves plafonnent a vingt-neuf.
# Un combat continue de courir apres que le village est vide : sur une mesure,
# il restait deux minutes sept de chronometre pour un butin deja tombe a
# 631 948 d'or. Ces minutes sont du farm perdu, alors on rend la main des que
# le village ne rapporte plus rien.
# Nombre de doigts poses en meme temps lors d'un depot de troupes.
CAPTURES = 0             # captures d'ecran depuis le debut de l'attaque
MULTI_DOIGTS = 4
# None : jamais essaye. True : en service. False : renonce apres un echec.
MULTI_ETAT = None
GOTO_APRES_TAP = 1.0     # pause apres un tap de navigation
GOTO_PATIENCE = 4.5      # au-dela, l'ecran s'obstine et on retape
BATAILLE_CALME = 45.0       # debut de combat ou rien ne peut encore le finir
BATAILLE_POLL_CALME = 6.0   # cadence de surveillance pendant ce debut
BATAILLE_POLL = 3.0         # cadence ensuite
BUTIN_RATIO_FIN = 0.10   # part du butin initial en deca de laquelle on arrete
BUTIN_INTERVALLE = 12.0  # secondes entre deux lectures du butin restant
BUTIN_CONFIRMATIONS = 2  # lectures basses de suite avant d'arreter
MUR_PRIX_MIN = 20_000_000
MUR_MARGE = 1.00         # seuil applique tel quel, sans marge
STOCK_ALERTE = 4         # phases sans rempart avant de crier
# Un stockage ne depasse pas trente millions par ressource. Au-dela, c'est que
# l'OCR a ajoute un chiffre : une lecture a quarante et un millions a fait
# croire que l'elixir couvrait largement un mur a quatre millions, alors qu'il
# n'y en avait qu'un peu plus d'un.
STOCK_MAX = 30_000_000

# Butin affiche en haut a gauche. L elixir noir n entre pas dans la decision
# et n est donc pas lu.
LOOT_LINES = {"or": (148, 188), "elixir": (204, 244)}
# Cadrage serre sur les chiffres : plus large, l'OCR mordait sur le decor du
# village et ajoutait un chiffre parasite (1 466 348 lu 14663485).
LOOT_X = (200, 380)

# --- Amelioration des remparts, au village ---
# Le village, hors barres d'interface. La limite basse s'arrete au-dessus de
# la rangee de boutons d'un objet selectionne : leur fond clair passait pour
# des remparts, et comme ce sont les plus grosses taches de l'image, le
# programme cliquait sur ses propres boutons au lieu d'un mur.
VILLAGE_AREA = (300, 130, 2120, 740)
# Elements d'interface poses sur le village. Le bandeau de ressources en haut
# a droite porte un bouton "+" par ressource qui ouvre la boutique de gemmes :
# un point de recherche tombant dessus a suffi pour lancer un achat Google Play
# et depenser sept cent quarante-trois gemmes. Aucun candidat ne doit y tomber.
VILLAGE_UI_ZONES = [
    (1890, 0, 2400, 440),    # or, elixir, elixir noir, gemmes et leurs "+"
    (0, 0, 300, 1080),       # colonne de boutons a gauche
    (2050, 440, 2400, 1080),  # boutons et boutique a droite
    (0, 850, 900, 1080),     # rangee du bas : attaquer, potions, calendrier
]
# Un rempart se reconnait a deux couleurs, selon son niveau : le creme clair
# de son dessus (240,240,200), et la coiffe doree (240,224,80) qui apparait
# aux niveaux eleves. Chercher le seul creme laissait de cote les remparts
# deja montes, qu'il faut pourtant continuer a ameliorer.
WALL_CREAM = dict(r_min=215, g_min=198, b_min=165, rb_min=28, rb_max=80, rg_max=32)
WALL_GOLD = dict(r_min=215, g_min=195, b_max=125, rb_min=130, rg_max=45)
WALL_DILATE = 13         # fusionne le damier des murs avant l'echantillonnage
WALL_GRID_STEP = 60      # espacement des points candidats, en pixels
WALL_GRID_FILL = 0.6     # part de mur exigee autour d'un point
# Taille minimale d'une etendue de mur, rapportee a la plus grande. Trop haut,
# seuls les remparts de haut niveau - groupes en gros bloc - etaient vus, et
# les beiges, moins chers et disperses, restaient ignores.
WALL_MAJOR_RATIO = 0.12

# Reconnaissance par motif. Plutot que de decrire la couleur d'un rempart -
# ce qui suppose de connaitre a l'avance celle de chaque niveau, et a echoue
# trois fois - le programme decoupe une imagette autour d'un mur qu'il vient
# de selectionner avec succes, puis cherche ce motif dans le village. Il
# apprend ainsi de lui-meme a quoi ressemblent les remparts du joueur, quels
# qu'ils soient. Sur un village reel : trente-trois murs trouves, chacun sur
# un segment, sans un seul faux positif, la ou la couleur en donnait vingt-deux
# approximatifs dont certains sur des rochers.
MOTIF_DIR = os.path.join(HERE, "motifs")
MOTIF_DEMI = (26, 22)    # demi-largeur et demi-hauteur d'une imagette
MOTIF_SEUIL = 0.78       # correlation minimale pour retenir une correspondance
MOTIF_MAX = 30           # nombre d'imagettes conservees
MOTIF_ECART = 45         # distance en deca de laquelle deux trouvailles se valent
MOTIF_ECHELLE = 0.5      # reduction appliquee avant la recherche
MOTIF_DEJA_VU = 0.90     # au-dela, un mur n'apprend rien de nouveau
MOTIF_VOISINAGE = 130    # distance ou l'on cherche les remparts voisins
MOTIF_VOISINS_MIN = 2    # voisins exiges : un mur seul n'est pas un mur
MOTIF_COUVERT = 70       # rayon ou un motif rend un point de couleur inutile
MUR_DECALAGE = 18        # descente du second essai, vers le pied du rempart
SOURCE_POINT = {}        # d'ou vient chaque candidat, pour le diagnostic
# Points d'exploration, tires d'une grille couvrant tout le village et
# independants de toute signature de couleur. Chercher les remparts a leur
# teinte suppose de connaitre a l'avance celle de chaque niveau : trois fois de
# suite, des murs parfaitement ameliorables sont restes invisibles parce que
# leur couleur n'avait pas ete prevue. Quelques points au hasard par phase
# suffisent a les decouvrir, et le cache retient ce qui a repondu.
# Recentrage de la vue avant de chercher les remparts. Le jeu laisse le joueur
# deplacer sa carte, et le programme ne voit que ce qui est a l'ecran : une vue
# restee de travers lui cachait la moitie des murs. Le glissement en diagonale
# pousse la carte contre sa butee, ce qui ramene les remparts au centre et,
# surtout, donne un cadrage toujours identique - sans quoi les positions
# retenues d'une fois sur l'autre ne voudraient rien dire.
RECENTRE_DEPART = (1850, 250)
RECENTRE_ARRIVEE = (1050, 850)
RECENTRE_MIN = 3         # glissements au moins, un seul pouvant ne pas prendre
RECENTRE_MAX = 8         # glissements au plus pour atteindre la butee
RECENTRE_STABLE = 3.0    # ecart moyen en deca duquel la vue ne bouge plus

EXPLORE_STEP = 90        # espacement de la grille d'exploration
EXPLORE_PAR_PHASE = 6    # points explores a chaque passage
EXPLORE_JUSQUA = 8       # au-dela de tant de murs connus, on cesse d'explorer

# Portion du village ou chercher les remparts. Les concentrer d'un cote fait
# gagner un temps reel : chaque essai coute cinq secondes, et ratisser un
# village entier pour des murs tous ranges a droite en gaspille l'essentiel.
ZONES_MURS = {
    "tout":   (0.00, 1.00),
    "droite": (0.48, 1.00),
    "gauche": (0.00, 0.52),
}
ZONE_MURS = "tout"       # fixe par --wall-zone


def zone_recherche():
    """Bornes horizontales de la recherche de remparts."""
    x0, y0, x1, y1 = VILLAGE_AREA
    f0, f1 = ZONES_MURS[ZONE_MURS]
    return int(x0 + f0 * (x1 - x0)), y0, int(x0 + f1 * (x1 - x0)), y1
# Le village ne bouge pas d'une attaque a l'autre : ce que l'on a identifie une
# fois reste vrai. On retient donc les points ou un rempart a repondu, et ceux
# ou l'on est tombe sur autre chose (une decoration doree, un batiment clair),
# pour ne plus y revenir. Chaque essai inutile coute cinq secondes.
# Change des que la facon de trouver les remparts change : le cache des points
# ecartes se purge alors de lui-meme, au lieu de faire porter a la detection
# actuelle les erreurs de la precedente.
DETECTEUR_VERSION = "motif-5-recalage-chaine"
# Change des que la regle qui declare un mur au maximum change. Ce classement
# etant definitif, une regle trop laxative laisse derriere elle des remparts
# retires du vivier pour toujours : il faut pouvoir les rendre.
REGLE_MAXIMES = "complet-5-recalage-chaine"
WALL_CACHE = os.path.join(HERE, "walls.json")
VUE_PHASE = os.path.join(HERE, "vue.png")   # la vue recentree de la derniere phase
VUE_PRECEDENTE = os.path.join(HERE, "vue_prec.png")   # celle d'avant, pour comparer
# Plusieurs reperes pris a des endroits differents. Un seul ne suffit pas : le
# bloc de remparts est un motif repetitif, et un repere qui y tombe s'accroche
# n'importe ou avec un score honorable. Celui qui servait jusqu'ici donnait un
# decalage qui n'ameliorait rien - cinquante-neuf virgule six de residu contre
# cinquante-neuf virgule trois sans recalage.
REPERES = [(520, 200, 780, 360), (900, 180, 1160, 340), (1400, 200, 1660, 360),
           (600, 560, 860, 700), (1500, 560, 1760, 700)]
REPERE_MIN = 0.55        # correlation en deca de laquelle un repere ne compte pas
REPERE_ACCORD = 12       # ecart en pixels sous lequel deux reperes s'accordent
REPERE_VOIX = 3          # reperes d'accord exiges pour retenir un decalage
REPERE_SUR = 0.80        # score au-dela duquel un seul repere fait foi
WALL_SAME_POINT = 40     # distance en deca de laquelle deux points se valent
# Quand un objet est selectionne, une rangee de boutons s'affiche en bas. La
# proportion de pixels blancs (le texte des boutons) y passe de ~3 % a ~37 %.
MENU_BOX = (900, 775, 1075, 915)
MENU_WHITE_MIN = 20.0
TITLE_BOX = (850, 690, 1600, 748)      # "Rempart (Niveau 17)"

# Boutons du menu d'un objet selectionne. Leurs positions ne sont pas fixes :
# la rangee est centree, donc elle se decale des qu'un bouton manque, ce qui
# arrive quand une reserve ne permet plus l'amelioration. Taper des
# coordonnees figees revenait alors a cliquer a cote. On repere donc le bord
# superieur des boutons (segments clairs bien nets vers y=790), puis la
# ressource de chacun a son icone : piece jaune ou goutte violette, presentes
# a 3.9 % et 5.9 % sur les deux boutons "Ameliorer" contre au plus 1.3 %
# ailleurs.
BUTTON_ROW_Y = (782, 798)
BUTTON_MIN_W, BUTTON_MAX_W = 140, 215
BUTTON_TAP_Y = 855
# Seuil de presence d'une icone de ressource. Avec la fenetre resserree, un
# vrai bouton marque 4.4 pour la piece et 10.0 pour la goutte, contre 0.0 pour
# tout le reste : la separation ne tient plus a un reglage fin.
ICON_MIN = 2.5
# "Ameliorer" ouvre une fenetre de confirmation avant de debiter. Son grand
# panneau de texte est presque entierement blanc (88 % contre moins de 5 %
# partout ailleurs), ce qui la rend impossible a confondre.
CONFIRM_PANEL = (1200, 200, 1950, 800)
CONFIRM_WHITE_MIN = 50.0
CONFIRM_BUTTON = (1588, 930)
# Compteurs d'or et d'elixir, en haut a droite. Leur variation est la seule
# preuve fiable qu'une amelioration a ete payee : apres un achat reussi le jeu
# rouvre aussitot la fenetre pour le niveau suivant, si bien que la presence
# d'une fenetre ne dit rien du resultat. Mesures : 0.00 d'ecart sur 2.5 s sans
# depense, 10 a 24 des qu'une ressource bouge.
HUD_BOX = (1990, 35, 2165, 200)
HUD_CHANGED_MAD = 3.0


class Phone:
    """Enrobage ADB minimal : taps, appuis longs, captures d'ecran."""

    def __init__(self, device=None):
        self.device = device or self._autodetect()
        w, h = self._screen_size()
        self.sx = w / REF_W
        self.sy = h / REF_H
        if abs(self.sx - 1) > 0.01 or abs(self.sy - 1) > 0.01:
            print(f"[i] ecran {w}x{h}, mise a l'echelle x{self.sx:.3f} y{self.sy:.3f}")

    @staticmethod
    def _autodetect():
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
        ids = [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]
        if not ids:
            sys.exit("Aucun telephone ADB connecte.")
        if len(ids) > 1:
            print(f"[i] plusieurs telephones, utilisation de {ids[0]} (--device pour choisir)")
        return ids[0]

    def _adb(self, *args, **kw):
        return subprocess.run(["adb", "-s", self.device, *args], **kw)

    def _screen_size(self):
        out = self._adb("shell", "wm", "size", capture_output=True, text=True).stdout
        # "Physical size: 1080x2400" -> le jeu tourne en paysage, on remet a plat
        dims = sorted(int(v) for v in out.strip().split(":")[-1].strip().split("x"))
        return dims[1], dims[0]

    def _pt(self, x, y):
        return int(round(x * self.sx)), int(round(y * self.sy))

    def tap(self, x, y):
        px, py = self._pt(x, y)
        self._adb("shell", "input", "tap", str(px), str(py))

    def select_and_burst(self, sx, sy, points):
        """Selectionne une carte puis enchaine les taps, en un seul appel ADB.

        Un `input tap` coute ~40 ms (lancement de la JVM), le round-trip ADB
        seulement 22 ms : tout regrouper dans une commande evite de payer le
        round-trip a chaque tap.
        """
        cx, cy = self._pt(sx, sy)
        taps = ";".join(f"input tap {x} {y}" for x, y in (self._pt(*p) for p in points))
        self._adb("shell", f"input tap {cx} {cy};{taps}")

    def pose_simultanee(self, sx, sy, points, doigts=MULTI_DOIGTS):
        """Selectionne une carte puis pose les troupes par accords de doigts.

        Renvoie False si l'injection n'a pas abouti, a charge de l'appelant de
        retomber sur `select_and_burst` : le deploiement marche a cent pour
        cent aujourd'hui, il ne doit pas dependre d'une piece nouvelle.

        Le gain ne vient pas seulement des doigts simultanes. Un `input tap`
        demarre une machine Java par tap, une soixantaine par carte ; ici toute
        la chronologie, selection comprise, tient dans une seule.
        """
        global MULTI_ETAT
        if MULTI_ETAT is False:
            return False
        try:
            import helper
            if MULTI_ETAT is None:
                helper.installe(self.device)
                MULTI_ETAT = True
            texte = helper.timeline_depot(
                self._pt(sx, sy), [self._pt(*p) for p in points], doigts)
            envoi = self._adb("shell", f"cat > {helper.SESSION_DISTANTE}",
                              input=texte.encode(), capture_output=True)
            if envoi.returncode != 0:
                raise RuntimeError("ecriture de la chronologie refusee")
            r = self._adb("shell", helper.commande_session(),
                          capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip()[:200])
            return True
        except Exception as e:      # noqa: BLE001 - tout echec doit se replier
            if MULTI_ETAT is not False:
                print(f"[i] pose simultanee indisponible ({e}), "
                      "retour aux taps un par un", flush=True)
            MULTI_ETAT = False
            return False

    def glisse(self, x1, y1, x2, y2, ms=500):
        """Fait glisser un doigt : deplace la carte du village."""
        ax, ay = self._pt(x1, y1)
        bx, by = self._pt(x2, y2)
        self._adb("shell", "input", "swipe", str(ax), str(ay), str(bx), str(by), str(int(ms)))

    def back(self):
        self._adb("shell", "input", "keyevent", "4")

    def screenshot(self):
        # Chaque capture coute pres de neuf cents millisecondes : leur nombre
        # decide du rythme bien plus que la vitesse des taps. On les compte
        # pour savoir ou elles se depensent.
        global CAPTURES
        CAPTURES += 1
        return self._screenshot()

    def _screenshot(self):
        """Capture l'ecran et la ramene a la resolution de reference.

        On prend le format brut plutot que du PNG : encoder le PNG sur le
        telephone coute 1.7 s, contre 0.9 s pour les pixels bruts. Comme une
        passe de deploiement vaut une capture, c'est le poste de depense
        principal du programme.
        """
        raw = self._adb("exec-out", "screencap", capture_output=True).stdout
        w, h, _fmt = struct.unpack("<III", raw[:12])
        header = len(raw) - w * h * 4          # 12 octets, ou 16 depuis Android 13
        arr = np.frombuffer(raw, dtype=np.uint8, offset=header).reshape(h, w, 4)[:, :, :3]
        if (w, h) != (REF_W, REF_H):
            arr = np.asarray(Image.fromarray(arr).resize((REF_W, REF_H)))
        return arr.astype(np.int16)

    def app_running(self):
        out = self._adb("shell", "dumpsys", "window", capture_output=True, text=True).stdout
        return "com.supercell.clashofclans" in out

    def launch(self):
        self._adb("shell", "monkey", "-p", "com.supercell.clashofclans",
                  "-c", "android.intent.category.LAUNCHER", "1", capture_output=True)


# --------------------------------------------------------------------------
# Reconnaissance d'ecran
# --------------------------------------------------------------------------

def load_templates():
    """Charge les imagettes de reference, une ou plusieurs par ecran."""
    tpl = {}
    for name, cfg in SCREENS.items():
        variantes = []
        for fichier in cfg.get("templates", [name]):
            path = os.path.join(TEMPLATE_DIR, f"{fichier}.png")
            if not os.path.exists(path):
                sys.exit(f"Template manquant : {path}")
            variantes.append(np.asarray(Image.open(path).convert("RGB")).astype(np.int16))
        tpl[name] = variantes
    return tpl


def ocr(image, config):
    """Reconnaissance de texte, jamais fatale.

    Tesseract est un programme externe : il lui arrive d'echouer sur une image
    donnee. L'exception remontait alors jusqu'a interrompre l'attaque en cours,
    alors qu'une lecture manquee se traite tres bien - on attaque le village
    dans le doute, on passe au mur suivant.
    """
    try:
        import pytesseract
        return pytesseract.image_to_string(image, config=config)
    except Exception:                                    # noqa: BLE001
        return ""


UNKNOWN_DIR = None      # dossier d'archivage des ecrans non reconnus
_unknown_seen = {}

# Entre deux vues, le jeu passe par un fondu au blanc ou l'image se delave.
# Ces images ne correspondent a aucun ecran, mais ce ne sont pas des imprevus :
# il faut simplement attendre, pas appuyer sur retour. Le delavage est
# progressif : de 6 au plus fort du fondu a 26 quand le decor reapparait,
# contre 57 a 86 sur tout ecran reel. Le seuil se place dans cet ecart, assez
# haut pour couvrir toute la transition sans jamais mordre sur un vrai ecran.
TRANSITION_STD_MAX = 40.0


def is_transition(img):
    """L'image est-elle un fondu entre deux vues plutot qu'un vrai ecran ?"""
    return float(img.std()) < TRANSITION_STD_MAX


def record_unknown(img, tag, limit=300):
    """Archive un ecran non reconnu, pour pouvoir le traiter ensuite.

    Un meme incident peut se repeter des centaines de fois sur une longue
    serie : on garde donc au plus quelques exemplaires par type, et un nombre
    total borne, pour ne pas saturer le disque.
    """
    if not UNKNOWN_DIR:
        return
    seen = _unknown_seen.get(tag, 0)
    if seen >= 5 or sum(_unknown_seen.values()) >= limit:
        return
    _unknown_seen[tag] = seen + 1
    os.makedirs(UNKNOWN_DIR, exist_ok=True)
    path = os.path.join(UNKNOWN_DIR,
                        f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png")
    Image.fromarray(img.astype("uint8")).save(path)
    print(f"[dbg] ecran inconnu archive : {path}", flush=True)


# Fenetre generique Annuler / OK du jeu. Elle sert aussi bien a confirmer la
# sortie de Clash of Clans qu'a valider une amelioration groupee de remparts a
# plusieurs millions : dans les deux cas, OK engage quelque chose qu'on ne veut
# pas, et Annuler se trouve au meme endroit. On annule donc toujours.
# Elle se reconnait a son bouton Annuler orange (10.7 % d'orange dans cette
# case, contre moins de 2 % sur tout autre ecran) ; le seul panneau blanc ne
# suffisait pas, la fenetre d'amelioration d'un mur lui ressemble trop.
CANCEL_PANEL = (760, 240, 1660, 800)
CANCEL_BOX = (880, 630, 1120, 755)
CANCEL_BUTTON = (990, 690)
CANCEL_OK = (1406, 690)
# Texte de la fenetre : il dit si l'on s'apprete a quitter le jeu ou a
# ameliorer des remparts, ce que les boutons seuls ne distinguent pas.
DIALOG_TEXT_BOX = (780, 430, 1680, 560)


def cancel_dialog_open(img):
    """Une fenetre Annuler / OK est-elle affichee ?"""
    x0, y0, x1, y1 = CANCEL_PANEL
    c = img[y0:y1, x0:x1]
    blanc = ((c[:, :, 0] > 228) & (c[:, :, 1] > 222) & (c[:, :, 2] > 212)).mean()
    if float(blanc) * 100 < 55:
        return False
    x0, y0, x1, y1 = CANCEL_BOX
    c = img[y0:y1, x0:x1]
    r, g, b = c[:, :, 0], c[:, :, 1], c[:, :, 2]
    orange = ((r > 195) & (g > 90) & (g < 175) & (b < 95) &
              (r - g > 60) & (g - b > 25)).mean()
    return float(orange) * 100 > 5


def idle_popup_open(img):
    """La fenetre de deconnexion pour inactivite est-elle affichee ?"""
    x0, y0, x1, y1 = IDLE_PANEL
    c = img[y0:y1, x0:x1]
    r, g, b = c[:, :, 0], c[:, :, 1], c[:, :, 2]
    sombre = float(((r < 95) & (g < 70) & (b < 75) & (r >= g)).mean()) * 100
    if sombre < IDLE_DARK_MIN:
        return False
    # Le panneau sombre ne suffit pas : on verifie ce qu'il dit.
    x0, y0, x1, y1 = IDLE_TEXT_BOX
    c = img[y0:y1, x0:x1]
    mask = (c.min(axis=2) > 150).astype(np.uint8) * 255
    big = Image.fromarray(255 - mask).resize(((x1 - x0) * 2, (y1 - y0) * 2),
                                             Image.LANCZOS)
    texte = ocr(big, "--psm 6").lower()
    # Le jeu coupe la partie pour plusieurs raisons, et n'emploie pas les memes
    # mots selon laquelle. "Connexion perdue - Un autre appareil se connecte a
    # ce village" a bloque le programme un quart d'heure, quatre attaques
    # perdues : le panneau etait pourtant bien reconnu, sombre a
    # quatre-vingt-seize pour cent, mais son texte
    # ne parlait ni d'inactivite ni de quelqu'un d'autre. Tous ces panneaux se
    # refermant par le meme bouton Recharger, on les traite ensemble.
    return any(mot in texte for mot in
               ("inactivit", "quelqu", "connexion perdue", "autre appareil",
                "recharger", "reconnect"))


def identify(img, templates):
    """Renvoie (nom_ecran, score) du meilleur template, ou (None, score).

    La comparaison est centree sur la moyenne : le jeu assombrit tout l'ecran
    pendant les fondus entre deux vues, et le village en plein fondu manquait
    de peu le seuil (41.9 pour 40). En annulant l'ecart de luminosite globale
    il retombe a 32.9, loin devant le suivant (62.9) : la marge redevient
    confortable sans rapprocher les ecrans entre eux.
    """
    # A verifier avant les templates : ce dialogue laisse le village visible
    # derriere lui et passe pour l'ecran d'accueil (score 37.6 pour un seuil
    # a 40), ce qui ferait agir a l'aveugle par-dessus.
    # Ces deux fenetres assombrissent le village sans le masquer : en annulant
    # l'ecart de luminosite, la comparaison les prend pour l'ecran d'accueil.
    # Elles sont donc reconnues a leur contenu propre, avant les templates.
    if idle_popup_open(img):
        return "idle", 0.0
    if cancel_dialog_open(img):
        return "cancel", 0.0
    if confirm_dialog_open(img):
        return "confirm", 0.0

    best, best_score = None, 1e9
    for name, cfg in SCREENS.items():
        x0, y0, x1, y1 = cfg["box"]
        region = img[y0:y1, x0:x1].astype(np.float32)
        reduite = (region - region.mean()) / max(float(region.std()), 1.0)
        for tpl in templates[name]:
            tpl = tpl.astype(np.float32)
            ref = (tpl - tpl.mean()) / max(float(tpl.std()), 1.0)
            score = float(np.abs(reduite - ref).mean()) * 50
            if score < best_score:
                best, best_score = name, score
    return (best, best_score) if best_score < MATCH_THRESHOLD else (None, best_score)


# --------------------------------------------------------------------------
# Butin
# --------------------------------------------------------------------------

def compte_chiffres(img, y0, y1):
    """Nombre de chiffres visibles sur une ligne de butin.

    Les chiffres du jeu ne se touchent pas : chaque colonne claire isolee en
    est un. Ce comptage ne depend pas de l'OCR et permet de rejeter ses
    lectures d'une longueur impossible, comme ce 668 649 rendu en sept
    chiffres.

    Le fond varie d'un village a l'autre, et un seuil unique laissait
    parfois la ligne entierement vide. On compte donc a plusieurs seuils et
    on retient le decompte le plus frequent, en ignorant les seuils muets.
    """
    x0, x1 = LOOT_X
    comptes = []
    for seuil in (150, 165, 180, 195):
        colonnes = (img[y0:y1, x0:x1].min(axis=2) > seuil).any(axis=0)
        n, largeur = 0, 0
        for pleine in colonnes:
            if pleine:
                largeur += 1
            else:
                if largeur >= 8:
                    n += 1
                largeur = 0
        if largeur >= 8:
            n += 1
        if n:
            comptes.append(n)
    return max(set(comptes), key=comptes.count) if comptes else 0


def read_loot(img, minimum=0):
    """Lectures du butin, plusieurs par ressource.

    Quand un seuil est donne, l'OCR n'est lance que pour les ressources dont
    le nombre de chiffres ne suffit pas a decider : six chiffres, c'est moins
    d'un million, donc sous un seuil d'un million et demi quoi qu'en dise
    l'OCR. Ce comptage coute une milliseconde la ou l'OCR en coute quinze
    cents, et il tranchait deja plus d'une decision sur deux - quarante sur
    soixante et onze dans le journal - mais on payait l'OCR avant meme de lui
    poser la question.

    Renvoie par exemple {"or": [1412423, 1412423, 1409423], "elixir": [...]}.
    On ne cherche pas a trancher ici : une lecture unique se trompait parfois
    avec aplomb, en ajoutant un chiffre, si bien que 668 649 devenait
    6 656 495. Trois lectures menees avec des seuils differents ne commettent
    pas la meme erreur, et c'est a la decision de dire si leur desaccord
    l'empeche de conclure.
    """
    try:
        import pytesseract
    except ImportError:
        return {}

    x0, x1 = LOOT_X
    comptes = {n: compte_chiffres(img, y0, y1)
               for n, (y0, y1) in LOOT_LINES.items()}
    # Le seuil porte sur le total : c'est donc l'encadrement des deux
    # ressources reunies qui dit si l'OCR a quelque chose a apporter. Le
    # decider ressource par ressource, comme avant, faisait lire a l'OCR des
    # villages que la somme des comptages tranchait deja.
    au_moins = sum(10 ** (c - 1) for c in comptes.values() if c)
    au_plus = sum(10 ** c - 1 for c in comptes.values() if c)
    tranche = (minimum > 0 and all(comptes.values())
               and (au_moins >= minimum or au_plus < minimum))
    out = {}
    for name, (y0, y1) in LOOT_LINES.items():
        attendu = comptes[name]
        lectures = []
        if tranche:
            out[name] = {"lectures": [], "chiffres": attendu}
            continue
        for seuil, echelle in ((160, 6), (175, 6), (160, 8)):
            crop = img[y0:y1, x0:x1]
            mask = (crop.min(axis=2) > seuil).astype(np.uint8) * 255
            big = Image.fromarray(255 - mask).resize(
                ((x1 - x0) * echelle, (y1 - y0) * echelle), Image.LANCZOS)
            txt = ocr(big, "--psm 7 -c tessedit_char_whitelist=0123456789")
            chiffres = re.sub(r"\D", "", txt)
            valeur = int(chiffres) if chiffres else None
            # Une lecture dont la longueur ne colle pas aux chiffres visibles
            # est fausse, meme si l'OCR la repete a l'identique.
            if (valeur is not None and valeur <= 20_000_000
                    and (attendu == 0 or len(chiffres) == attendu)):
                lectures.append(valeur)
        out[name] = {"lectures": lectures, "chiffres": attendu}
    return out


def butin_par_ressource(loot):
    """Valeur lue pour chaque ressource, les illisibles en moins."""
    out = {}
    for nom in ("or", "elixir"):
        lectures = (loot.get(nom) or {}).get("lectures") or []
        if lectures:
            out[nom] = sorted(lectures)[len(lectures) // 2]
    return out


def butin_total(loot):
    """Somme des ressources lues, ou None si aucune ne l'est."""
    valeurs = butin_par_ressource(loot)
    return sum(valeurs.values()) if valeurs else None


def part_restante(depart, reste):
    """Part du butin encore a prendre, sur les seules ressources comparables.

    On ne compare qu'une ressource lue des deux cotes. Reclamer les deux, comme
    le fait le bot dont vient cette logique, ecarte des combats ou l'or seul est
    illisible et l'elixir parfaitement net - c'est arrive sur une attaque sur
    deux. Mais melanger les cotes serait pire : une ressource absente du
    restant ferait paraitre le village vide alors qu'il ne l'est pas.
    """
    a, b = butin_par_ressource(depart), butin_par_ressource(reste)
    communes = [n for n in a if n in b]
    total = sum(a[n] for n in communes)
    if not communes or total <= 0:
        return None
    return sum(b[n] for n in communes) / total


def bornes_butin(info):
    """Encadrement d'une ressource : (au moins, au plus), None si inconnu.

    Le nombre de chiffres suffit souvent : six chiffres, c'est entre cent mille
    et un million moins un, sans que l'OCR ait a se prononcer.
    """
    lectures = (info or {}).get("lectures") or []
    chiffres = (info or {}).get("chiffres") or 0
    if lectures:
        return min(lectures), max(lectures)
    if chiffres:
        return 10 ** (chiffres - 1), 10 ** chiffres - 1
    return None


def loot_is_good(loot, minimum):
    """Ce village vaut-il l'attaque ?

    Le seuil porte sur le total or + elixir, non sur chaque ressource prise a
    part. Un village a 1 181 739 d'or et 1 356 789 d'elixir etait ecarte devant
    un seuil d'un million et demi, alors qu'il offrait deux millions et demi :
    aucune de ses deux reserves n'atteignait le seuil a elle seule.

    Il ne s'agit pas de connaitre le butin au chiffre pres, mais de savoir de
    quel cote du seuil il tombe. Le nombre de chiffres visibles encadre chaque
    valeur, et cet encadrement tranche
    le plus souvent sans OCR. Quand il
    enjambe le seuil, on attaque : passer un village correct coute plus cher
    qu'en attaquer un pauvre.
    """
    detail, bas, haut, inconnu = [], 0, 0, False
    for nom in ("or", "elixir"):
        info = loot.get(nom) or {}
        bornes = bornes_butin(info)
        if bornes is None:
            detail.append(f"{nom}=?")
            inconnu = True
            continue
        b, h = bornes
        lectures = info.get("lectures") or []
        if lectures:
            detail.append(f"{nom}={b}" if b == h else f"{nom}={b}~{h}")
        else:
            detail.append(f"{nom}<{h + 1}")
        bas += b
        haut += h
    texte = " ".join(detail) + f" (total {bas}~{haut})"
    if bas >= minimum:
        return True, texte
    if not inconnu and haut < minimum:
        return False, texte
    return True, texte + " (encadrement incertain, on attaque)"


# --------------------------------------------------------------------------
# Barre de troupes
# --------------------------------------------------------------------------

def find_slots(img):
    """Centres x des cartes de troupes, de gauche a droite.

    Le cadre bas des cartes forme des segments clairs bien nets sur le fond
    sombre de la barre ; on retient la ligne qui en revele le plus.
    """
    best = []
    for y in range(SLOT_ROW_Y[0], SLOT_ROW_Y[1] + 1):
        lum = img[y:y + 3].mean(axis=(0, 2))
        runs, start = [], None
        for x, bright in enumerate(lum > 70):
            if bright and start is None:
                start = x
            elif not bright and start is not None:
                if SLOT_MIN_W <= x - start <= SLOT_MAX_W:
                    runs.append((start + x) // 2)
                start = None
        if len(runs) > len(best):
            best = runs
    return sans_fantomes(best)


def sans_fantomes(xs):
    """Ecarte les faux slots detectes a cote de la barre.

    Le decor du village se voit par transparence derriere la barre de troupes
    et produit parfois un segment clair de la bonne largeur, juste a gauche de
    la premiere carte. Le programme le prenait pour un slot, tentait d'y
    deployer et n'obtenait evidemment rien : une attaque s'est terminee a neuf
    slots sur dix apres six passes perdues sur ce fantome.

    Les vraies cartes sont regulierement espacees, avec un ecart un peu plus
    grand entre familles. Un voisin nettement trop proche n'en est donc pas
    une.
    """
    if len(xs) < 3:
        return xs
    ecarts = sorted(xs[i + 1] - xs[i] for i in range(len(xs) - 1))
    median = ecarts[len(ecarts) // 2]
    garde = []
    for i, x in enumerate(xs):
        voisin = xs[i + 1] - x if i + 1 < len(xs) else median
        precedent = x - xs[i - 1] if i else median
        if voisin < 0.85 * median and precedent >= 0.85 * median:
            continue        # colle a la carte suivante : c'est le fantome
        garde.append(x)
    return garde


def card(img, cx):
    return img[SLOT_CARD_Y[0]:SLOT_CARD_Y[1], max(0, cx - 45):cx + 45]


def card_empty(img, cx):
    """Une carte epuisee est grisee : sa saturation moyenne s'effondre."""
    c = card(img, cx).astype(float)
    mx, mn = c.max(axis=2), c.min(axis=2)
    return float(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0).mean()) <= SLOT_ACTIVE_SAT


def has_count_badge(img, cx):
    """La carte porte-t-elle un compteur "xNN" ? Vrai pour troupes et sorts,
    faux pour heros et machines de siege."""
    dx0, dx1, y0, y1 = BADGE_BOX
    region = img[y0:y1, cx + dx0:cx + dx1]
    return float((region.min(axis=2) > 170).mean()) * 100 > BADGE_MIN


def classify_slots(img, slots):
    """Repartit les slots en 'troupe', 'heros' et 'sort'.

    Le deck suit toujours le meme ordre : troupes, machines de siege, heros,
    puis sorts. Les heros et les sieges n'ont pas de compteur ; les sorts sont
    donc les cartes a compteur situees apres le dernier slot sans compteur.

    La distinction sert au dosage : une pile de 56 troupes reclame une longue
    rafale, un heros ne sort qu'une fois et n'a besoin que de quelques taps.
    """
    badges = [has_count_badge(img, cx) for cx in slots]
    last_without = max((i for i, b in enumerate(badges) if not b), default=-1)
    kinds = []
    for i, b in enumerate(badges):
        if not b:
            kinds.append("heros")      # heros ou machine de siege : une unite
        elif i > last_without:
            kinds.append("sort")
        else:
            kinds.append("troupe")
    return kinds


def card_changed(before, after):
    """Le compteur de la carte a bouge => des unites sont bien sorties.

    On compare les pixels plutot que la saturation : passer de x50 a x40 ne
    desature presque pas la carte, mais change visiblement le chiffre.

    Attention : la comparaison n'est valable que si la carte est dans le meme
    etat de selection sur les deux captures, car une carte selectionnee est
    dessinee plus grande et decalee.
    """
    return float(np.abs(before.astype(np.int32) - after.astype(np.int32)).mean()) > CARD_CHANGED_MAD


# --------------------------------------------------------------------------
# Points de largage
# --------------------------------------------------------------------------

def probe_lanes(side, n_lanes, n_steps):
    """Rayons de sondage : pour chaque couloir, des points allant de
    l'interieur vers l'exterieur. On retient le premier accepte, donc le point
    le plus proche possible de la base tout en restant hors zone rouge.

    Les couloirs balaient tout le cote (et non son seul milieu), sinon les
    troupes finissent toutes agglutinees au meme endroit.
    """
    if side == "all":
        lanes = []
        for s in ("left", "top", "right", "bottom"):
            lanes.extend(probe_lanes(s, n_lanes, n_steps))
        return lanes

    x0, y0, x1, y1 = PLAY_AREA
    W, H = x1 - x0, y1 - y0
    lanes = []
    if side in ("left", "right"):
        depths = (np.linspace(0.26, 0.06, n_steps) if side == "left"
                  else np.linspace(0.74, 0.94, n_steps))
        for fy in np.linspace(0.14, 0.90, n_lanes):
            lanes.append([(x0 + fx * W, y0 + fy * H) for fx in depths])
    else:
        depths = (np.linspace(0.32, 0.06, n_steps) if side == "top"
                  else np.linspace(0.68, 0.94, n_steps))
        for fx in np.linspace(0.14, 0.88, n_lanes):
            lanes.append([(x0 + fx * W, y0 + fy * H) for fy in depths])
    return lanes


def calibrate_points(phone, img, slot_cx, args, verbose=True):
    """Sonde les points de largage et ne retient que ceux que le jeu accepte.

    Renvoie (points_valides, derniere_capture).
    """
    valid = []
    # La carte reste selectionnee pendant tout le sondage : sans cela son
    # aspect change (elle se souleve) et fausse la comparaison.
    phone.tap(slot_cx, SLOT_TAP_Y)
    time.sleep(0.25)
    img = phone.screenshot()

    hit = None      # profondeur qui a marche au couloir precedent
    for lane in probe_lanes(args.side, args.points, args.probe_steps):
        lane = [p for p in lane if is_safe(*p)]
        if not lane:
            continue
        # Demarrage a chaud : la bordure bouge peu d'un couloir au suivant,
        # donc on retente d'abord la profondeur qui vient de fonctionner. Cela
        # ramene le sondage a ~1 capture par couloir au lieu de 3.
        order = sorted(range(len(lane)), key=lambda i: abs(i - hit) if hit is not None else i)
        for i in order:
            px, py = lane[i]
            phone.tap(px, py)
            time.sleep(0.15)
            new = phone.screenshot()
            ok = card_changed(card(img, slot_cx), card(new, slot_cx))
            img = new
            if ok:
                valid.append((px, py))
                hit = i
                break
            if card_empty(img, slot_cx):
                break
        if card_empty(img, slot_cx):
            break

    # Sonder coute une capture d'ecran par essai (~0.8 s, incompressible) :
    # on en fait peu, puis on complete le pourtour par geometrie.
    dense = densify(valid)
    if verbose:
        pts = ", ".join(f"({int(x)},{int(y)})" for x, y in dense)
        print(f"[i] {len(valid)} points sondes -> {len(dense)} points de largage : "
              f"{pts or 'aucun'}")
    return dense, img


def is_safe(x, y):
    """Le point est-il sur la carte, hors des boutons superposes ?"""
    px0, py0, px1, py1 = PLAY_AREA
    if not (px0 <= x <= px1 and py0 <= y <= py1):
        return False
    return not any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in UI_ZONES)


def jitter(point, amount, rng):
    """Petit decalage aleatoire : evite de larguer toujours au pixel pres.

    On retente si le tirage tombe sur un bouton, et a defaut on garde le point
    d'origine, qui a deja ete valide.
    """
    x, y = point
    for _ in range(6):
        jx = x + rng.uniform(-amount, amount)
        jy = y + rng.uniform(-amount, amount)
        if is_safe(jx, jy):
            return jx, jy
    return x, y


def densify(points, push=45):
    """Intercale un point entre chaque paire de points valides voisins.

    Les points valides se trouvent sur le pourtour de la zone interdite, qui
    est un losange : le milieu de deux d'entre eux coupe donc vers
    l'interieur. On le repousse vers l'exterieur pour rester largable. On
    trie d'abord par angle autour du barycentre, afin que « voisins » veuille
    bien dire voisins sur le pourtour.
    """
    points = [p for p in points if is_safe(*p)]
    if len(points) < 3:
        return points
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    ring = sorted(points, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))

    out = []
    for i, p in enumerate(ring):
        out.append(p)
        q = ring[(i + 1) % len(ring)]
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        vx, vy = mx - cx, my - cy
        norm = math.hypot(vx, vy) or 1.0
        cand = (mx + vx / norm * push, my + vy / norm * push)
        if is_safe(*cand):
            out.append(cand)
    return out


def spell_points(n, rng):
    """Points de lancement des sorts, tires au hasard dans tout le village."""
    x0, y0, x1, y1 = SPELL_ZONE
    out = []
    while len(out) < n:
        p = (rng.uniform(x0, x1), rng.uniform(y0, y1))
        if is_safe(*p):
            out.append(p)
    return out


# --------------------------------------------------------------------------
# Deploiement
# --------------------------------------------------------------------------

def deploy_all(phone, templates, args, rng, verbose=True):
    """Vide tous les slots : troupes reparties sur le cote, sorts au centre.

    On procede par passes : chaque passe touche une fois chaque slot encore
    plein et ne prend qu'une seule capture (a la fin). Une capture coute
    ~0.9 s, c'est le poste le plus cher du programme.

    Le type de chaque slot est decide sur l'image (presence du compteur), pas
    sur son comportement : une pile de troupes qui semble ne pas bouger etait
    prise pour un sort et abandonnee, laissant des dizaines de troupes au
    depot. Un slot de troupes n'est donc jamais abandonne.

    Renvoie (nb_slots_vides, nb_slots_total).
    """
    img = phone.screenshot()
    slots = find_slots(img)
    if not slots:
        print("[!] aucun slot de troupes detecte")
        return 0, 0

    # Le sondage doit se faire avec une pile de troupes, jamais avec un heros :
    # un heros ne sort qu'une fois, si bien que le premier point accepte epuise
    # la carte et que le sondage s'arrete la, souvent sans avoir rien trouve.
    # Le deck ne commence pas toujours par une troupe, d'ou ce tri.
    kinds_init = classify_slots(img, slots)
    dispo = [cx for cx, k in zip(slots, kinds_init)
             if k == "troupe" and not card_empty(img, cx)]
    if not dispo:
        dispo = [cx for cx in slots if not card_empty(img, cx)]
    first = dispo[0] if dispo else None
    if first is None:
        return 0, len(slots)

    points, img = calibrate_points(phone, img, first, args, verbose)
    if not points:
        # Repli : le bord de la map est presque toujours largable.
        points = [p[-1] for p in probe_lanes(args.side, args.points, args.probe_steps)]
        print("[!] sondage infructueux, repli sur le bord de la map")

    kinds = dict(zip(slots, classify_slots(img, slots)))
    if verbose:
        resume = ", ".join(f"{k[:2]}" for k in classify_slots(img, slots))
        print(f"[i] slots : {resume}")

    prev = {cx: card(img, cx) for cx in slots}
    done = {cx for cx in slots if card_empty(img, cx)}
    idle = {cx: 0 for cx in slots}     # passes consecutives sans rien sortir

    for rnd in range(1, args.max_passes + 1):
        active = [cx for cx in slots if cx not in done]
        if not active:
            break

        # L'ordre des slots et celui des points changent a chaque passe : sans
        # cela, cent attaques se ressembleraient trait pour trait.
        ordre = list(enumerate(active))
        pool = list(points)
        if args.random > 0:
            rng.shuffle(ordre)
            rng.shuffle(pool)

        for i, cx in ordre:
            if kinds[cx] == "sort":
                # Les sorts se lancent par taps secs, disperses au hasard en
                # plein milieu du village.
                pts = spell_points(args.spell_taps, rng)
                if not phone.pose_simultanee(cx, SLOT_TAP_Y, pts):
                    phone.select_and_burst(cx, SLOT_TAP_Y, pts)
                continue
            # Un heros ne sort qu'une fois : inutile de lui servir la rafale
            # complete, ce serait deux secondes perdues par heros.
            n = args.hero_taps if kinds[cx] == "heros" else args.burst
            if args.random > 0 and kinds[cx] != "heros":
                n = max(1, int(n * rng.uniform(1 - 0.2 * args.random,
                                              1 + 0.2 * args.random)))
            # Le point change a chaque tap, a chaque slot et a chaque passe :
            # les troupes se repartissent sur tout le pourtour au lieu de
            # s'entasser au meme endroit.
            start = (rnd - 1) * len(active) + i + idle[cx]
            pts = [jitter(pool[(start + k) % len(pool)], args.jitter * args.random, rng)
                   for k in range(n)]
            if not phone.pose_simultanee(cx, SLOT_TAP_Y, pts):
                phone.select_and_burst(cx, SLOT_TAP_Y, pts)
            if args.random > 0:
                time.sleep(rng.uniform(0, 0.20 * args.random))

        # On reselectionne toujours la meme carte avant de mesurer : ainsi
        # l'etat de selection est identique d'une passe a l'autre et ne
        # pollue pas la comparaison (une carte selectionnee change d'aspect).
        phone.tap(active[0], SLOT_TAP_Y)
        time.sleep(0.3)
        img = phone.screenshot()

        screen, _ = identify(img, templates)
        if screen is not None and screen != "battle":
            if verbose:
                print(f"    combat termine pendant le deploiement (ecran {screen})")
            break

        for cx in active:
            now = card(img, cx)
            if card_empty(img, cx):
                done.add(cx)
            elif card_changed(prev[cx], now):
                idle[cx] = 0
            else:
                # Rien n'est sorti : le point courant est sans doute refuse.
                # On decale le slot vers un autre point plutot que de
                # l'abandonner.
                idle[cx] += 1
                if verbose and idle[cx] == 1:
                    print(f"    slot x={cx} sans effet, changement de point")
                # Une pile de troupes finit toujours par bouger si on change de
                # point de largage. Apres plusieurs passes sans le moindre
                # effet, c'est que ce slot ne repond pas du tout : insister
                # coutait six passes sur une seule attaque.
                if idle[cx] >= IDLE_ABANDON:
                    done.add(cx)
                    if verbose:
                        print(f"    slot x={cx} sans reponse, abandonne")
            prev[cx] = now

        if verbose:
            restants = [cx for cx in slots if cx not in done]
            print(f"    passe {rnd} : {len(done)}/{len(slots)} slots vides"
                  + (f", restants {restants}" if restants else ""))

    return len(done), len(slots)


# --------------------------------------------------------------------------
# Remparts
# --------------------------------------------------------------------------

def charge_motifs():
    """Imagettes de remparts apprises lors des selections reussies."""
    if not os.path.isdir(MOTIF_DIR):
        return []
    motifs = []
    for nom in sorted(os.listdir(MOTIF_DIR))[:MOTIF_MAX]:
        try:
            motifs.append(np.asarray(Image.open(os.path.join(MOTIF_DIR, nom))
                                     .convert("RGB")).astype(np.uint8))
        except (OSError, ValueError):
            pass
    return motifs


def apprend_motif(img, point):
    """Retient l'aspect d'un rempart dont la selection vient de reussir.

    Un rempart qui ressemble deja a une imagette connue n'apprend rien : le
    garder remplirait les quelques places disponibles avec le meme mur repete,
    quand ce qu'il faut couvrir, ce sont les aspects differents - niveaux,
    orientations, angles du bloc.
    """
    import cv2
    dx, dy = MOTIF_DEMI
    x, y = int(point[0]), int(point[1])
    if x - dx < 0 or y - dy < 0 or x + dx > REF_W or y + dy > REF_H:
        return
    try:
        os.makedirs(MOTIF_DIR, exist_ok=True)
        patch = img[y - dy:y + dy, x - dx:x + dx].astype(np.uint8)
        noms = sorted(os.listdir(MOTIF_DIR))
        connus = charge_motifs()
        for connu in connus:
            if connu.shape == patch.shape and cv2.matchTemplate(
                    patch, connu, cv2.TM_CCOEFF_NORMED)[0, 0] >= MOTIF_DEJA_VU:
                return
        if len(noms) >= MOTIF_MAX:
            # Plein, mais ce mur ne ressemble a aucun de ceux qu'on connait :
            # cesser d'apprendre serait le pire choix. La lumiere du village
            # change au fil des heures, et les places s'etaient remplies de
            # murs sombres pendant que les beiges, quatre fois plus nombreux a
            # monter, n'y avaient plus droit. On remplace donc le plus
            # redondant : celui qui ressemble le plus a un autre.
            pires, rang = -2.0, 0
            for i, a in enumerate(connus):
                for j, b in enumerate(connus):
                    if i != j and a.shape == b.shape:
                        v = float(cv2.matchTemplate(a, b,
                                                    cv2.TM_CCOEFF_NORMED)[0, 0])
                        if v > pires:
                            pires, rang = v, i
            os.remove(os.path.join(MOTIF_DIR, noms[rang]))
        Image.fromarray(patch).save(os.path.join(MOTIF_DIR, f"{x}-{y}.png"))
    except (OSError, IndexError, cv2.error):
        pass


def cherche_motifs(img, motifs):
    """Positions des remparts ressemblant aux imagettes apprises.

    La recherche se fait sur une image reduite de moitie : un rempart occupe
    une cinquantaine de pixels, il en reste vingt-cinq, largement de quoi le
    reconnaitre. Mesure faite sur le village : resultats identiques au pixel
    pres, pour six fois moins de calcul - vingt et une millisecondes par
    imagette au lieu de cent trente.
    """
    import cv2
    zx0, zy0, zx1, zy1 = zone_recherche()
    e = MOTIF_ECHELLE
    scene = cv2.resize(cv2.cvtColor(img[zy0:zy1, zx0:zx1].astype(np.uint8),
                                    cv2.COLOR_RGB2BGR),
                       None, fx=e, fy=e, interpolation=cv2.INTER_AREA)
    dx, dy = MOTIF_DEMI
    trouves = []
    for motif in motifs:
        tpl = cv2.resize(cv2.cvtColor(motif, cv2.COLOR_RGB2BGR),
                         None, fx=e, fy=e, interpolation=cv2.INTER_AREA)
        if tpl.shape[0] >= scene.shape[0] or tpl.shape[1] >= scene.shape[1]:
            continue
        res = cv2.matchTemplate(scene, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= MOTIF_SEUIL)
        # Du meilleur au moins bon, et non dans l'ordre de balayage. Un mur
        # depasse le seuil sur toute une tache de pixels ; en gardant le
        # premier rencontre, on retenait toujours son coin haut-gauche, a une
        # vingtaine de pixels du centre. Assez pour que le tap tombe entre deux
        # remparts et ne selectionne rien.
        for x, y in sorted(zip(xs, ys), key=lambda p: -res[p[1], p[0]]):
            p = (float(x / e + dx + zx0), float(y / e + dy + zy0))
            if any(zzx0 <= p[0] <= zzx1 and zzy0 <= p[1] <= zzy1
                   for zzx0, zzy0, zzx1, zzy1 in VILLAGE_UI_ZONES):
                continue
            if not proche(p, trouves, MOTIF_ECART):
                trouves.append(p)
    # Un rempart ne vit jamais seul : il appartient a une rangee. Un point sans
    # voisin est donc autre chose qui partage la teinte des murs - une mine
    # d'or a bien ete selectionnee ainsi, son dore etant celui des coiffes.
    # Sur le village, ce filtre n'ecarte aucun des vingt-huit vrais candidats.
    return [p for p in trouves
            if sum(1 for q in trouves
                   if q is not p and math.dist(p, q) <= MOTIF_VOISINAGE)
            >= MOTIF_VOISINS_MIN]


def wall_candidates(img):
    """Points a taper pour selectionner un rempart.

    Les deux methodes se completent. Les motifs sont les plus surs, mais ils ne
    connaissent que les remparts deja selectionnes avec succes : un niveau de
    mur jamais tape leur est invisible. Sur le village, ils trouvaient les
    trente murs sombres et pas un seul des beiges, pourtant a monter eux aussi.
    On leur ajoute donc ce que la couleur voit ailleurs - huit murs beiges, tous
    reels. Le premier beige selectionne fera un motif, et la couleur n'aura plus
    a le rattraper.
    """
    trouves = cherche_motifs(img, charge_motifs())
    couleur = [p for p in candidats_couleur(img)
               if not proche(p, trouves, MOTIF_COUVERT)]
    # Chaque point retient d'ou il vient : sans cela, on ne peut pas savoir
    # laquelle des deux methodes fait rater les selections, et on corrige au
    # jugé.
    SOURCE_POINT.clear()
    for p in trouves:
        SOURCE_POINT[(round(p[0]), round(p[1]))] = "motif"
    for p in couleur:
        SOURCE_POINT[(round(p[0]), round(p[1]))] = "couleur"
    return trouves + couleur


def candidats_couleur(img):
    """Points ou la teinte des remparts domine.

    On cherche le dessus creme des murs et leur coiffe doree, puis on
    echantillonne une grille de points la ou la couleur domine. Les reperer
    par leur forme ne marchait que sur des remparts isoles : regroupes en bloc
    compact, ils formaient une tache trapue que le filtre d'allongement
    rejetait, et le programme n'en trouvait plus un seul.

    Le masque est dilate avant l'echantillonnage : les murs se dessinent en
    damier de petits carres separes de creux sombres, et sans cela aucun
    voisinage n'atteint la densite voulue.
    """
    import cv2
    x0, y0, x1, y1 = zone_recherche()
    z = img[y0:y1, x0:x1]
    r, g, b = z[:, :, 0], z[:, :, 1], z[:, :, 2]
    c = WALL_CREAM
    creme = ((r > c["r_min"]) & (g > c["g_min"]) & (b > c["b_min"]) &
             (r - b > c["rb_min"]) & (r - b < c["rb_max"]) &
             (abs(r - g) < c["rg_max"]))
    o = WALL_GOLD
    coiffe = ((r > o["r_min"]) & (g > o["g_min"]) & (b < o["b_max"]) &
              (r - b > o["rb_min"]) & (abs(r - g) < o["rg_max"]))
    mask = cv2.dilate((creme | coiffe).astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                (WALL_DILATE, WALL_DILATE)))

    # Les remparts forment de vastes etendues d'un seul tenant, la ou les
    # dorures d'un batiment ne font que quelques centaines de pixels. En ne
    # gardant que les etendues comparables a la plus grande, on ecarte
    # l'essentiel des batiments : sur un village reel, cinquante candidats
    # eparpilles sont devenus vingt-huit, tous sur des murs.
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n > 1:
        aires = [(stats[i, cv2.CC_STAT_AREA], i) for i in range(1, n)]
        plus_grande = max(a for a, _ in aires)
        gardees = [i for a, i in aires if a >= WALL_MAJOR_RATIO * plus_grande]
        mask = np.isin(labels, gardees).astype(np.uint8)

    points = []
    demi = WALL_GRID_STEP // 2
    for yy in range(demi, mask.shape[0], WALL_GRID_STEP):
        for xx in range(demi, mask.shape[1], WALL_GRID_STEP):
            voisinage = mask[max(0, yy - 10):yy + 10, max(0, xx - 10):xx + 10]
            if not (voisinage.size and voisinage.mean() > WALL_GRID_FILL):
                continue
            px, py = float(xx + x0), float(yy + y0)
            if any(zx0 <= px <= zx1 and zy0 <= py <= zy1
                   for zx0, zy0, zx1, zy1 in VILLAGE_UI_ZONES):
                continue
            points.append((px, py))
    return points

def read_gems(img, templates):
    """Nombre de gemmes affiche au village. None hors du village ou illisible.

    Le compteur n'existe qu'a l'ecran du village : le lire ailleurs renvoyait
    des valeurs inventees, et la securite qui s'appuie dessus criait alors a
    tort. Une alarme qui se declenche sans raison finit par etre ignoree.
    """
    if identify(img, templates)[0] != "home":
        return None
    x0, y0, x1, y1 = GEM_BOX
    c = img[y0:y1, x0:x1]
    mask = (c.min(axis=2) > 170).astype(np.uint8) * 255
    big = Image.fromarray(255 - mask).resize(((x1 - x0) * 6, (y1 - y0) * 6),
                                             Image.LANCZOS)
    chiffres = re.sub(r"\D", "",
                      ocr(big, "--psm 7 -c tessedit_char_whitelist=0123456789"))
    return int(chiffres) if chiffres and len(chiffres) <= 6 else None


MURS_INTERDITS = False     # coupe-circuit arme si des gemmes ont ete depensees
_STOCKS = []               # historique (or, elixir) des phases sans amelioration


def masque_chiffres(z, methode):
    """Isole les chiffres d'une case.

    `methode` est soit une clarte absolue, soit un couple (voisinage, marge)
    pour un seuillage au contraste local.

    La barre de ressources se remplit : sa portion pleine est claire, et quand
    elle passe sous les chiffres blancs, aucune clarte absolue ne les separe
    plus - c'est pourquoi la panne allait et venait avec le niveau des
    reserves. Le contraste local, lui, voit encore le liseré sombre qui borde
    chaque chiffre.
    """
    if isinstance(methode, int):
        return (z.min(axis=2) > methode).astype(np.uint8) * 255
    import cv2
    voisinage, marge = methode
    gris = cv2.cvtColor(z.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    return cv2.adaptiveThreshold(gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, voisinage, marge)


def compte_colonnes(z):
    """Combien de chiffres une case en montre, sans passer par l'OCR."""
    comptes = []
    for seuil in STOCK_SEUILS:
        colonnes = (masque_chiffres(z, seuil) > 0).any(axis=0)
        n, largeur = 0, 0
        for pleine in colonnes:
            if pleine:
                largeur += 1
            else:
                if largeur >= 4:
                    n += 1
                largeur = 0
        if largeur >= 4:
            n += 1
        if n:
            comptes.append(n)
    return max(set(comptes), key=comptes.count) if comptes else 0


def read_stocks(img, templates):
    """Or et elixir du village. None hors du village ou si illisible."""
    if identify(img, templates)[0] != "home":
        return None
    out = {}
    for nom, (x0, y0, x1, y1) in STOCK_LINES.items():
        lectures = []
        # Deux largeurs de case. La plus large recupere le dernier chiffre des
        # montants a sept chiffres larges, que l'etroite coupait - 5 668 572 se
        # lisait 566 857. Mais elle fait entrer un bord d'icone que le
        # contraste local prend parfois pour un chiffre de plus. Aucune ne
        # convient seule ; on les essaie l'une apres l'autre.
        attendus = 0
        exacte = None
        for bord in STOCK_BORDS:
            z = img[y0:y1, x0:bord]
            attendus = attendus or compte_colonnes(z)
            for seuil in STOCK_SEUILS:
                mask = masque_chiffres(z, seuil)
                big = Image.fromarray(255 - mask).resize(((bord - x0) * 6,
                                                          (y1 - y0) * 6),
                                                         Image.LANCZOS)
                chiffres = re.sub(r"\D", "",
                                  ocr(big, "--psm 7 -c "
                                           "tessedit_char_whitelist=0123456789"))
                if chiffres and int(chiffres) <= STOCK_MAX:
                    lectures.append((int(chiffres), len(chiffres)))
                    # Une lecture de la bonne longueur suffit : on arrete la.
                    # Les essayer toutes coutait vingt-quatre passes d'OCR par
                    # ecran, cinq secondes a chaque attaque, alors que la
                    # premiere clarte repond juste dans le cas courant. Les
                    # suivantes n'existent que pour les fonds difficiles - un
                    # decor pale derriere la barre, un remplissage a mi-course
                    # - et ne servent que si aucune n'a encore abouti.
                    if attendus and len(chiffres) == attendus:
                        exacte = int(chiffres)
                        break
            if exacte is not None:
                break
        if not lectures:
            return None
        if exacte is not None:
            out[nom] = exacte
            continue
        # Le nombre de chiffres se compte sans OCR : ceux du jeu ne se touchent
        # pas, chaque colonne claire isolee en est un. Mais ce comptage se
        # trompe lui aussi - il a rendu six sur un 4 959 274 dont deux chiffres
        # se touchaient - et l'avoir traite en filtre absolu faisait alors
        # rejeter toutes les lectures justes d'un coup. Il ne sert donc plus
        # qu'a departager : on prefere les lectures de la bonne longueur, et
        # s'il n'y en a aucune, la valeur qui revient le plus souvent.
        exactes = [v for v, n in lectures if attendus and n == attendus]
        if exactes:
            out[nom] = exactes[0]
        else:
            valeurs = [v for v, _ in lectures]
            out[nom] = max(valeurs,
                           key=lambda v: (valeurs.count(v), -valeurs.index(v)))
    return out


def surveille_stocks(stocks, ameliores, prix_hors_portee, verbose=True):
    """Crie si les reserves montent sans qu'aucun rempart ne s'ameliore.

    C'est la panne qui ne laisse aucune trace : le programme attaque, ramene du
    butin, et le butin s'accumule. Ni erreur, ni message d'echec - seulement des
    reserves qui gonflent pendant que rien ne se construit.

    Encore faut-il que le blocage soit technique. Quand le jeu a lui-meme
    signale que le prix depassait les reserves - bouton masque ou prix en rouge
    - il n'y a rien a corriger : les remparts coutent simplement plus que ce
    qu'une attaque rapporte. Crier dans ce cas ferait de l'alarme un bruit de
    fond, et le jour ou elle aurait raison, plus personne ne l'ecouterait.
    """
    if stocks is not None and verbose:
        # Sans cette ligne, le journal ne dit jamais ou en sont les reserves,
        # alors que c'est ce qui decide de tout : un rempart coute plusieurs
        # millions quand une attaque en rapporte un ou deux. Une phase sans
        # amelioration ne se lit pas de la meme facon selon que le compte monte
        # ou stagne.
        print(f"[i] reserves : or={stocks['or']} elixir={stocks['elixir']}")
    if ameliores > 0 or prix_hors_portee:
        _STOCKS.clear()
        return
    if stocks is None:
        return
    _STOCKS.append((stocks["or"], stocks["elixir"]))
    if len(_STOCKS) < STOCK_ALERTE:
        return
    debut, fin = _STOCKS[0], _STOCKS[-1]
    if fin[0] + fin[1] > debut[0] + debut[1]:
        if verbose:
            print(f"[!] ALERTE : {len(_STOCKS)} phases sans le moindre rempart "
                  f"alors que les reserves montent "
                  f"({debut[0]}+{debut[1]} -> {fin[0]}+{fin[1]}). "
                  "La detection des murs est probablement en cause.", flush=True)
        _STOCKS.clear()


def explore_points(rng, n=None):
    """Points tires au hasard sur tout le village, sans critere de couleur."""
    x0, y0, x1, y1 = zone_recherche()
    grille = [(float(x), float(y))
              for y in range(y0 + EXPLORE_STEP // 2, y1, EXPLORE_STEP)
              for x in range(x0 + EXPLORE_STEP // 2, x1, EXPLORE_STEP)
              if not any(zx0 <= x <= zx1 and zy0 <= y <= zy1
                         for zx0, zy0, zx1, zy1 in VILLAGE_UI_ZONES)]
    rng.shuffle(grille)
    return grille[:n if n is not None else EXPLORE_PAR_PHASE]


def point_taquable(p):
    """Ce point peut-il etre tape sans risque ?

    Le recalage deplace les points memorises de plusieurs centaines de pixels.
    Rien ne garantit qu'ils restent dans la zone de village : l'un s'est
    retrouve a y=768, sous la limite de recherche, et un cran de plus l'aurait
    pose sur la rangee de boutons du bas. Un tap egare sur celle-ci a deja
    coute sept cent quarante-trois gemmes.
    """
    x0, y0, x1, y1 = VILLAGE_AREA
    if not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1):
        return False
    return not any(zx0 <= p[0] <= zx1 and zy0 <= p[1] <= zy1
                   for zx0, zy0, zx1, zy1 in VILLAGE_UI_ZONES)


def mesure_decalage(img):
    """De combien la vue courante est decalee par rapport a la reference.

    La comparaison se fait avec la phase precedente, non avec une vue fixe.
    Une reference vieille de quelques heures ne ressemble plus au village :
    cent remparts y ont change d'aspect, et aucun repere ne s'y retrouvait
    plus. Deux phases voisines, elles, different de moins de deux points.

    Renvoie (dx, dy), ou None si les reperes ne s'accordent pas. Le decalage
    est une translation pure : verifie par correspondance a quarante et une
    echelles, le meilleur accord tombe exactement a l'echelle un.
    """
    import cv2
    if not os.path.exists(VUE_PRECEDENTE):
        return (0.0, 0.0)
    try:
        ref = np.asarray(Image.open(VUE_PRECEDENTE).convert("RGB"))
    except (OSError, ValueError):
        return (0.0, 0.0)
    scene = cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_RGB2BGR)
    propositions = []
    for x0, y0, x1, y1 in REPERES:
        repere = cv2.cvtColor(ref[y0:y1, x0:x1], cv2.COLOR_RGB2BGR)
        if repere.shape[0] >= scene.shape[0] or repere.shape[1] >= scene.shape[1]:
            continue
        _, score, _, (px, py) = cv2.minMaxLoc(
            cv2.matchTemplate(scene, repere, cv2.TM_CCOEFF_NORMED))
        if score >= REPERE_MIN:
            propositions.append((float(score), float(px - x0), float(py - y0)))
    # Un repere tres sur se passe d'appui. Les autres sont souvent faibles pour
    # de bonnes raisons - leur contenu a change, ou le decalage les a pousses
    # hors du cadre - et exiger trois voix ecartait la bonne reponse : sur une
    # mesure reelle, un repere a 0,92 donnait le decalage exact que la
    # correlation de phase confirmait, les quatre autres plafonnant a 0,54.
    if propositions:
        meilleur = max(propositions)
        if meilleur[0] >= REPERE_SUR:
            return (meilleur[1], meilleur[2])
    propositions = [(x, y) for _, x, y in propositions]
    # On ne retient qu'un decalage sur lequel plusieurs reperes tombent
    # d'accord. Un motif repetitif fait dire n'importe quoi a un repere isole,
    # mais il faudrait une coincidence pour qu'il fasse mentir trois reperes
    # pris a des endroits differents de la meme facon.
    for p in propositions:
        accord = [q for q in propositions
                  if abs(q[0] - p[0]) <= REPERE_ACCORD
                  and abs(q[1] - p[1]) <= REPERE_ACCORD]
        if len(accord) >= REPERE_VOIX:
            return (sum(q[0] for q in accord) / len(accord),
                    sum(q[1] for q in accord) / len(accord))
    return None


def load_wall_cache():
    """Points retenus des phases precedentes.

    Les points ecartes ne valent que pour le detecteur qui les a produits :
    ceux de la detection par couleur bloquaient deux vrais remparts que les
    motifs trouvent. On les oublie donc des que la detection change de nature,
    en gardant les remparts confirmes, qui eux restent vrais.
    """
    try:
        with open(WALL_CACHE) as f:
            data = json.load(f)
        if data.get("detecteur") != DETECTEUR_VERSION:
            # Tout ce cache est fait de coordonnees, et elles ne valent que
            # sous la vue qui les a produites. Un changement de detecteur ou de
            # facon de recentrer les rend caduques, remparts confirmes compris.
            return [], [], [], []
        murs = [tuple(p) for p in data.get("murs", [])]
        maximes = ([tuple(p) for p in data.get("maximes", [])]
                   if data.get("regle_maximes") == REGLE_MAXIMES else [])
        return (murs,
                [tuple(p) for p in data.get("autres", [])],
                [tuple(p) for p in data.get("suspects", [])],
                maximes)
    except (OSError, ValueError):
        return [], [], [], []


def save_wall_cache(murs, autres, suspects, maximes):
    try:
        with open(WALL_CACHE, "w") as f:
            json.dump({"detecteur": DETECTEUR_VERSION,
                       "murs": [list(p) for p in murs[-400:]],
                       "autres": [list(p) for p in autres[-400:]],
                       "suspects": [list(p) for p in suspects[-400:]],
                       "regle_maximes": REGLE_MAXIMES,
                       "maximes": [list(p) for p in maximes[-800:]]}, f)
    except OSError:
        pass


def proche(point, liste, seuil=WALL_SAME_POINT):
    return any(math.dist(point, q) <= seuil for q in liste)


def menu_open(img):
    """Un objet est-il selectionne ? La rangee de boutons se remplit de texte."""
    x0, y0, x1, y1 = MENU_BOX
    c = img[y0:y1, x0:x1]
    white = ((c[:, :, 0] > 210) & (c[:, :, 1] > 210) & (c[:, :, 2] > 200)).mean()
    return float(white) * 100 > MENU_WHITE_MIN


def selected_title(img):
    """Nom de l'objet selectionne, lu par OCR. Chaine vide si illisible.

    Plusieurs clartes sont essayees, car ce texte est incruste dans le decor du
    village et un seuil unique le rend parfois illisible : un rempart de niveau
    dix-sept s'est lu "a | 4 ( i ea U- 17)" et a donc ete rejete, alors qu'il
    etait bel et bien selectionne. On s'arrete des qu'une lecture donne un
    rempart, ce qui ne coute rien dans le cas courant.
    """
    try:
        import pytesseract
    except ImportError:
        return ""
    x0, y0, x1, y1 = TITLE_BOX
    z = img[y0:y1, x0:x1]
    lectures = []
    for seuil in TITRE_SEUILS:
        mask = (z.min(axis=2) > seuil).astype(np.uint8) * 255
        big = Image.fromarray(255 - mask).resize(((x1 - x0) * 3, (y1 - y0) * 3),
                                                 Image.LANCZOS)
        lu = ocr(big, "--psm 7").strip()
        if titre_est_rempart(lu):
            return lu
        if lu:
            lectures.append(lu)
    return max(lectures, key=len) if lectures else ""


def menu_buttons(img):
    """Centres x des boutons du menu de l'objet selectionne."""
    best = []
    for y in range(BUTTON_ROW_Y[0], BUTTON_ROW_Y[1], 2):
        lum = img[y:y + 3, 620:1820].mean(axis=(0, 2))
        runs, start = [], None
        for x, clair in enumerate(lum > 150):
            if clair and start is None:
                start = x
            elif not clair and start is not None:
                if BUTTON_MIN_W <= x - start <= BUTTON_MAX_W:
                    runs.append(620 + (start + x) // 2)
                start = None
        if start is not None and BUTTON_MIN_W <= len(lum) - start <= BUTTON_MAX_W:
            runs.append(620 + (start + len(lum)) // 2)
        if len(runs) > len(best):
            best = runs
    return sans_fantomes(best)


def sans_fantomes(xs):
    """Ecarte les faux slots detectes a cote de la barre.

    Le decor du village se voit par transparence derriere la barre de troupes
    et produit parfois un segment clair de la bonne largeur, juste a gauche de
    la premiere carte. Le programme le prenait pour un slot, tentait d'y
    deployer et n'obtenait evidemment rien : une attaque s'est terminee a neuf
    slots sur dix apres six passes perdues sur ce fantome.

    Les vraies cartes sont regulierement espacees, avec un ecart un peu plus
    grand entre familles. Un voisin nettement trop proche n'en est donc pas
    une.
    """
    if len(xs) < 3:
        return xs
    ecarts = sorted(xs[i + 1] - xs[i] for i in range(len(xs) - 1))
    median = ecarts[len(ecarts) // 2]
    garde = []
    for i, x in enumerate(xs):
        voisin = xs[i + 1] - x if i + 1 < len(xs) else median
        precedent = x - xs[i - 1] if i else median
        if voisin < 0.85 * median and precedent >= 0.85 * median:
            continue        # colle a la carte suivante : c'est le fantome
        garde.append(x)
    return garde


def cost_affordable(img, cx):
    """Le prix affiche sur ce bouton est-il payable ?

    Le jeu ecrit le cout en rouge quand la reserve ne suffit pas. C'est une
    reponse directe et exacte, la ou lire les reserves par OCR se trompait :
    le cadrage perdait un chiffre sur les grands nombres, et le bandeau change
    d'aspect selon l'ecran. Mesures : 24.6 % de rouge sur un prix hors de
    portee, 0.0 % sur un prix payable.
    """
    b = img[758:796, max(0, cx - 70):cx + 60]
    r, g, bl = b[:, :, 0], b[:, :, 1], b[:, :, 2]
    rouge = float(((r > 150) & (r - g > 55) & (r - bl > 55)).mean()) * 100
    return rouge < 8.0


def upgrade_buttons(img):
    """Position des boutons "Ameliorer", par ressource.

    Renvoie par exemple {"or": (1402, 855), "elixir": (1628, 855)}. Une
    ressource absente signifie que le jeu ne propose pas cette amelioration.
    """
    trouves = {}
    # Seuls les deux derniers boutons ameliorent le mur selectionne. Celui qui
    # les precede, "Ameliorer plus", porte lui aussi une piece d'or : il etait
    # pris pour le bouton en or et lancait une amelioration groupee a plusieurs
    # millions, que le jeu proposait alors de confirmer.
    for cx in menu_buttons(img)[-2:]:
        # La fenetre reste dans la largeur du bouton. Debordant de trente-cinq
        # pixels, elle mordait sur le village pour le dernier bouton du menu :
        # les remparts jaunes du decor y marquaient 3.8, autant qu'une vraie
        # piece d'or. "Choisir rangee" passait ainsi pour un bouton
        # d'amelioration, et le taper selectionnait une rangee entiere.
        bandeau = img[750:798, cx + 35:cx + 88]
        r, g, b = bandeau[:, :, 0], bandeau[:, :, 1], bandeau[:, :, 2]
        piece = float(((r > 200) & (g > 150) & (g < 225) & (b < 110)).mean()) * 100
        goutte = float(((r > 150) & (b > 150) & (g < 130)).mean()) * 100
        if max(piece, goutte) < ICON_MIN:
            continue
        trouves["or" if piece > goutte else "elixir"] = (cx, BUTTON_TAP_Y)
    return trouves


def texte_dialogue(img):
    """Message d'une fenetre Annuler / OK, lu par OCR. Vide si illisible."""
    try:
        import pytesseract
    except ImportError:
        return ""
    x0, y0, x1, y1 = DIALOG_TEXT_BOX
    c = img[y0:y1, x0:x1]
    mask = (c.max(axis=2) < 120).astype(np.uint8) * 255
    big = Image.fromarray(255 - mask).resize(((x1 - x0) * 2, (y1 - y0) * 2),
                                             Image.LANCZOS)
    return ocr(big, "--psm 6")


def titre_est_rempart(titre):
    """Le titre lu designe-t-il un rempart ?

    L'OCR se trompe regulierement d'une lettre sur ce texte incruste dans le
    decor : "Rempahrt", "Rempart" avec un accent parasite... Exiger le mot
    exact faisait rejeter des murs parfaitement selectionnes, d'ou la
    comparaison approchee.
    """
    import difflib
    for mot in re.findall(r"[a-z]+", titre.lower()):
        if difflib.SequenceMatcher(None, mot, "rempart").ratio() > 0.7:
            return True
    return False


def confirm_dialog_open(img):
    """La fenetre "Ameliorer votre Rempart au niveau N ?" est-elle affichee ?"""
    x0, y0, x1, y1 = CONFIRM_PANEL
    c = img[y0:y1, x0:x1]
    white = ((c[:, :, 0] > 225) & (c[:, :, 1] > 225) & (c[:, :, 2] > 215)).mean()
    return float(white) * 100 > CONFIRM_WHITE_MIN


DERNIER_RETOUR = "aucun"


def retour(phone, raison):
    """Appuie sur retour en retenant pourquoi.

    Une confirmation de sortie du jeu ne peut naitre que d'un de ces appuis.
    Sans savoir lequel, on ne corrige qu'a l'aveugle : trois tentatives s'y
    sont deja usees.
    """
    global DERNIER_RETOUR
    DERNIER_RETOUR = raison
    phone.back()


def back_to_home(phone, templates, tries=4):
    """Referme ce qui traine jusqu'a retrouver le village."""
    for i in range(tries):
        img = phone.screenshot()
        ecran = identify(img, templates)[0]
        if ecran == "home":
            # Un menu de batiment ouvert, c'est encore le village : la boucle
            # rendait la main sans le refermer. Le tap suivant servait alors a
            # fermer ce menu au lieu de selectionner ce qu'il visait, et deux
            # ratages de cette sorte ecartent un vrai rempart du vivier. On ne
            # touche au bouton retour qu'apres avoir vu le menu, car au village
            # sans menu il ouvre la confirmation de sortie du jeu.
            # On ne referme pas les menus de batiment ici. L'avoir tente a
            # produit des confirmations de sortie du jeu a repetition : au
            # village, le bouton retour n'a d'effet inoffensif que si un menu
            # est bien ouvert, et la moindre erreur de lecture fait osciller la
            # boucle - retour ouvre la fenetre, Annuler la referme, retour la
            # rouvre - jusqu'a la laisser ouverte, bouton OK en pleine zone de
            # tap des remparts. Un menu qui traine ne coute qu'un tap ; cette
            # fenetre-la peut fermer le jeu.
            return True
        if ecran == "idle":
            phone.tap(*IDLE_RELOAD)
            time.sleep(12.0)
            continue
        if ecran == "cancel":
            # C'est notre propre retour arriere qui l'a ouvert : on annule.
            phone.tap(*CANCEL_BUTTON)
            time.sleep(1.2)
            continue
        if ecran == "confirm":
            retour(phone, "retour-village/confirm")
            time.sleep(1.2)
            continue
        if is_transition(img):
            time.sleep(1.0)     # fondu : rien a refermer, il faut attendre
            continue
        if i == tries - 1:
            record_unknown(img, "retour-village")
        retour(phone, f"retour-village/ecran={ecran}")
        time.sleep(1.0)
    return identify(phone.screenshot(), templates)[0] == "home"


def pay_upgrade(phone, templates, resource):
    """Clique "Ameliorer" puis confirme. Renvoie True si le paiement est passe.

    Quand la ressource manque, le jeu propose d'acheter le complement contre
    des gemmes. On ne clique jamais dans cette fenetre : le succes se juge au
    retour effectif au village, et on ressort au bouton retour.
    """
    menu = phone.screenshot()
    boutons = upgrade_buttons(menu)
    if resource not in boutons:
        # Le jeu ne propose pas cette amelioration : mur au maximum, rangee
        # deja en chantier, ou reserve trop juste.
        # Ambigu : le mur peut etre au maximum, ou la detection avoir echoue.
        # Ne pas confondre avec un prix hors de portee, sous peine de desarmer
        # l'alarme avec le symptome meme du defaut qu'elle doit signaler.
        print(f"    [{resource}] bouton absent du menu")
        record_unknown(menu, f"sans-bouton-{resource}")
        return "indisponible"

    if not cost_affordable(menu, boutons[resource][0]):
        # Le prix s'affiche en rouge : inutile de cliquer. On s'epargne la
        # fenetre d'achat de gemmes et les quinze secondes qu'elle coute.
        print(f"    [{resource}] prix en rouge, reserve insuffisante")
        return "hors-portee"

    phone.tap(*boutons[resource])
    time.sleep(1.5)
    img = phone.screenshot()
    if not confirm_dialog_open(img) and not cancel_dialog_open(img):
        # La fenetre met parfois plus d'une seconde et demie a s'afficher :
        # conclure trop tot faisait renoncer a un mur parfaitement payable.
        time.sleep(1.3)
        img = phone.screenshot()
    if not confirm_dialog_open(img) and not cancel_dialog_open(img):
        # Toujours rien : le menu s'animait encore et a avale le clic. C'est
        # systematiquement la premiere ressource tentee qui en faisait les
        # frais, la seconde reussissant sur le meme mur au meme prix.
        phone.tap(*boutons[resource])
        time.sleep(1.8)
        img = phone.screenshot()
    if cancel_dialog_open(img):
        # Fenetre Annuler / OK : le jeu propose autre chose que l'amelioration
        # du seul mur vise, typiquement un lot a plusieurs millions. On refuse.
        # Aux niveaux eleves, le menu du rempart n'a plus de fenetre de
        # confirmation propre : sa validation est cette fenetre Annuler / OK.
        # La refuser revenait a ne jamais monter ces murs. On ne valide qu'apres
        # avoir lu son message : il doit parler de remparts, jamais de quitter
        # le jeu, dont le bouton OK ferme Clash of Clans.
        texte = texte_dialogue(img)
        # Le message doit annoncer un prix : une amelioration en affiche
        # toujours un, la confirmation de sortie du jeu jamais. C'est le
        # garde-fou decisif, car son bouton OK ferme Clash of Clans.
        montant = re.search(r"\d[\d\s]{5,}", texte)
        if ("quitter" in texte.lower() or not titre_est_rempart(texte)
                or not montant):
            print(f"    [{resource}] fenetre inattendue, refusee")
            record_unknown(img, f"dialogue-{resource}")
            phone.tap(*CANCEL_BUTTON)
            time.sleep(1.2)
            return "indisponible"

        x0, y0, x1, y1 = HUD_BOX
        avant = img[y0:y1, x0:x1]
        phone.tap(*CANCEL_OK)
        time.sleep(2.2)
        img = phone.screenshot()
        ecart = float(np.abs(avant.astype(np.int32)
                             - img[y0:y1, x0:x1].astype(np.int32)).mean())
        paye = ecart > HUD_CHANGED_MAD
        print(f"    [{resource}] validee, ecart des reserves = {ecart:.1f}"
              f" -> {'paye' if paye else 'refuse'}")
        if not paye:
            back_to_home(phone, templates)
        return "paye" if paye else "refuse"
    if not confirm_dialog_open(img):
        print(f"    [{resource}] aucune fenetre de confirmation")
        record_unknown(img, f"ameliorer-{resource}")
        return "indisponible"

    x0, y0, x1, y1 = HUD_BOX
    avant = img[y0:y1, x0:x1]
    phone.tap(*CONFIRM_BUTTON)
    time.sleep(2.2)
    img = phone.screenshot()
    ecart = float(np.abs(avant.astype(np.int32)
                         - img[y0:y1, x0:x1].astype(np.int32)).mean())
    paye = ecart > HUD_CHANGED_MAD
    print(f"    [{resource}] confirme, ecart des reserves = {ecart:.1f}"
          f" -> {'paye' if paye else 'refuse'}")

    if not paye:
        # Un refus empile la fenetre d'achat de gemmes par-dessus celle de
        # confirmation. Remonter jusqu'au village deselectionnerait le mur et
        # ferait echouer l'essai avec l'autre reserve : on ne referme donc que
        # ce qui est ouvert, en s'arretant des que le menu du mur reapparait.
        record_unknown(menu, f"refus-{resource}")
        for _ in range(3):
            shot = phone.screenshot()
            if cancel_dialog_open(shot):
                # Fenetre Annuler / OK : on sort par Annuler, jamais par OK,
                # qui selon la fenetre achete des gemmes ou ferme le jeu.
                phone.tap(*CANCEL_BUTTON)
                time.sleep(1.0)
                continue
            if confirm_dialog_open(shot):
                retour(phone, "refus/confirm")
                time.sleep(1.0)
                continue
            # Plus aucune fenetre par-dessus : on est ressorti, que le menu du
            # mur soit encore la ou non. Appuyer encore sur retour ouvrirait la
            # confirmation de sortie du jeu - et la boucle se mettait alors a
            # osciller, l'appui suivant la refermant, le troisieme la rouvrant,
            # si bien qu'elle restait ouverte une fois sur deux. C'est de la
            # que venaient les fenetres de sortie trouvees en fin de phase.
            return "refuse"
        return "refuse"

    if (confirm_dialog_open(img) or menu_open(img)
            or identify(img, templates)[0] != "home"):
        back_to_home(phone, templates)
    return "paye"


def upgrade_walls(phone, templates, args, rng, verbose=True):
    """Ameliore des remparts au village. Renvoie le nombre d'ameliorations.

    On paie en or, et si l'or ne suffit pas on retente en elixir : cela evite
    d'avoir a lire les reserves, le jeu refusant simplement l'operation quand
    la ressource manque.
    """
    global MURS_INTERDITS
    if MURS_INTERDITS:
        print("[!] remparts desactives : des gemmes ont ete depensees plus tot")
        return 0
    # Rien a tenter si aucune reserve n'atteint le prix du rempart le moins
    # cher. La phase entiere se resumerait a des prix en rouge, une minute
    # perdue par attaque, soit une attaque de plus toutes les trois. On ne
    # saute que sur une lecture reussie : une lecture manquee ne prouve rien.
    # La marge protege d'une erreur de lecture : l'OCR se trompe parfois d'un
    # chiffre, et un 4 500 000 lu 1 500 000 ferait sauter une phase ou un mur
    # etait payable. Ne pas sauter coute une minute, sauter a tort coute une
    # amelioration.
    stocks_avant = read_stocks(phone.screenshot(), templates)
    if stocks_avant and max(stocks_avant.values()) < MUR_PRIX_MIN * MUR_MARGE:
        if verbose:
            print(f"[i] remparts sautes : or={stocks_avant['or']} "
                  f"elixir={stocks_avant['elixir']}, moins que {MUR_PRIX_MIN}")
        # Le jeu n'a pas eu a le dire, mais c'est bien un prix hors de portee :
        # l'alarme des reserves n'a pas a s'en emouvoir.
        surveille_stocks(stocks_avant, 0, True, verbose=False)
        return 0

    # La vue peut avoir ete laissee de travers : on la ramene contre sa butee
    # pour voir tous les remparts, et toujours sous le meme angle.
    #
    # On glisse jusqu'a ce que l'image cesse de bouger, au lieu d'un nombre
    # fixe de fois. Deux glissements ne suffisaient pas toujours a atteindre la
    # butee, et tout le cache est positionnel : le bloc de remparts s'est
    # retrouve a x 660-1400 lors d'une phase et a x 400-1120 lors d'une autre.
    # Les points memorises tombaient alors a cote - trente des quarante echecs
    # de selection venaient de la, contre dix de la detection - et les murs
    # notes au maximum risquaient d'ecarter de vrais remparts glisses a leur
    # place.
    depart = phone.screenshot()
    immobile = False
    mouvements = []
    for n in range(RECENTRE_MAX):
        phone.glisse(*RECENTRE_DEPART, *RECENTRE_ARRIVEE)
        time.sleep(1.2)
        apres = phone.screenshot()
        bouge = float(np.abs(apres.astype(np.int32)
                             - depart.astype(np.int32)).mean())
        depart = apres
        # Deux immobilites de suite, et jamais avant trois glissements. Un
        # seul glissement qui ne prend pas - le jeu est encore occupe au retour
        # au village - suffisait sinon a faire conclure "stable" alors qu'on
        # n'avait pas bouge. Deux phases voisines se sont ainsi retrouvees
        # l'une contre la butee, l'autre au milieu du village : un ecart moyen
        # de soixante entre elles, quand le seuil d'immobilite est de trois.
        mouvements.append(round(bouge, 1))
        if bouge < RECENTRE_STABLE and n + 1 >= RECENTRE_MIN:
            if immobile:
                break
            immobile = True
        else:
            immobile = False
    else:
        print(f"[i] vue non stabilisee apres {RECENTRE_MAX} glissements")
    # Ce que chaque glissement a reellement deplace. Deux phases voisines
    # s'obstinent a finir a des endroits differents, et supposer pourquoi m'a
    # deja coute deux correctifs sans effet.
    print(f"[i] recentrage : {mouvements}")
    # La vue recentree, telle que la phase l'a vue. Sans elle, on ne peut pas
    # verifier apres coup ou tombaient les candidats : la vue derive entre deux
    # phases, et comparer des coordonnees a un ecran pris plus tard fait passer
    # des points justes pour des points en pleine foret.
    try:
        if os.path.exists(VUE_PHASE):
            # L'ecart avec la phase precedente mesure la reproductibilite du
            # recentrage. Tout le cache des remparts est fait de coordonnees :
            # si la vue ne revient pas au meme endroit, les points memorises
            # tombent a cote, et c'est bien d'eux que viennent la plupart des
            # selections ratees.
            ancienne = np.asarray(Image.open(VUE_PHASE).convert("RGB"))
            os.replace(VUE_PHASE, VUE_PRECEDENTE)
            if ancienne.shape == depart.shape:
                ecart = float(np.abs(ancienne.astype(np.int32)
                                     - depart.astype(np.int32)).mean())
                print(f"[i] vue : ecart avec la phase precedente = {ecart:.1f}")
        Image.fromarray(depart.astype(np.uint8)).save(VUE_PHASE)
    except (OSError, ValueError):
        pass

    # Le cache est exprime dans la vue de la phase precedente. Le recentrage ne
    # ramene pas toujours la carte au meme endroit - le jeu avale les
    # glissements qui suivent le premier - alors on mesure l'ecart de proche en
    # proche et on y recale les points, plutot que de s'acharner a forcer la
    # camera. De proche en proche, car une vue de reference figee vieillit :
    # cent remparts ont change d'aspect en trois heures, plus aucun repere ne
    # s'y retrouvait, et le decalage rendu n'ameliorait rien du tout.
    decalage = mesure_decalage(depart)
    if decalage is None:
        print("[i] repere de vue introuvable : cache ignore pour cette phase")
    else:
        print(f"[i] decalage de la vue : {decalage[0]:+.0f} {decalage[1]:+.0f}")
    gemmes_avant = read_gems(depart, templates)

    upgraded = 0
    tried = set()
    # Ordre d'essai des ressources. Un essai qui echoue coute une quinzaine de
    # secondes, alors on retient celle qui vient de marcher et on relegue celle
    # qui manque.
    order = ["or", "elixir"]
    epuisees = set()        # reserves dont le paiement a deja ete refuse
    rouges = {}             # murs vus au prix rouge, par ressource
    hors_portee = False     # le jeu a signale un prix superieur aux reserves
    # Les suspects se conservent d'une attaque a l'autre, sans quoi la regle
    # des deux echecs ne se declenche jamais : un point rate une fois par
    # phase, la liste repart de zero, et le meme leurre coute cinq secondes a
    # chaque attaque de la nuit.
    sans_repere = False
    connus, ecartes, suspects, maximes = load_wall_cache()
    if decalage is None:
        # Sans repere, les coordonnees memorisees ne veulent rien dire ici. On
        # travaille a la detection seule plutot que de taper au hasard, et on
        # ne reecrira pas le cache avec des points qu'on ne saurait pas situer.
        connus, ecartes, suspects, maximes = [], [], [], []
        sans_repere = True
    else:
        # Recales sans etre filtres : un point sorti du cadre sous cette vue y
        # reviendra sous la suivante, la camera alternant entre deux positions.
        # L'ecarter pour de bon vidait le cache a chaque bascule - trente-deux
        # murs au maximum tombes a dix-huit en deux phases. Le controle de
        # sureté se fait au moment de taper.
        dx, dy = decalage
        vers_ecran = lambda l: [(x + dx, y + dy) for x, y in l]
        connus, ecartes = vers_ecran(connus), vers_ecran(ecartes)
        suspects, maximes = vers_ecran(suspects), vers_ecran(maximes)
    # Le budget doit couvrir les essais infructueux, pas seulement les
    # ameliorations : quinze essais s'epuisaient sur des points rates avant
    # d'atteindre un mur payable, et la phase se terminait sans rien monter
    # alors que des remparts bon marche attendaient ailleurs. Chaque point
    # rate deux fois est ensuite ecarte pour de bon, si bien que ce budget se
    # consomme de moins en moins au fil des attaques.
    for _ in range(args.walls * 8):
        if upgraded >= args.walls:
            break
        img = phone.screenshot()
        ecran = identify(img, templates)[0]
        if ecran == "cancel":
            # La confirmation de sortie du jeu, ouverte par notre propre retour
            # arriere quand le menu qu'il visait s'etait deja referme. Y voir la
            # fin de la phase coutait cher : l'une s'est arretee la avec vingt-
            # neuf remparts detectes et un seul essai fait. On la referme par
            # Annuler - jamais par OK, qui ferme Clash of Clans - et on reprend.
            if verbose:
                print("[i] confirmation de sortie refermee, on reprend")
            phone.tap(*CANCEL_BUTTON)
            time.sleep(1.2)
            continue
        if ecran != "home":
            if verbose:
                print(f"[i] phase interrompue : ecran {ecran}")
            record_unknown(img, f"phase-{ecran}")
            break

        # Les points deja reconnus comme remparts passent devant ; ceux ou
        # l'on est tombe sur autre chose sont ecartes d'office.
        detectes = [p for p in wall_candidates(img)
                    if not proche(p, ecartes) and not proche(p, maximes)]
        # Les points connus sont eux aussi soumis a la liste des ecartes :
        # sans quoi un point devenu muet y restait en tete et etait reessaye a
        # chaque phase, jusqu'a epuiser tout le budget en echecs.
        # A la detection par couleur s'ajoutent quelques points d'exploration :
        # c'est le seul moyen de trouver un rempart dont la teinte n'a pas ete
        # prevue, et ce qui repond enrichit le cache pour les fois suivantes.
        # L'exploration ne sert qu'a decouvrir : une fois assez de remparts
        # connus, elle ne fait plus que perdre cinq secondes par point tire au
        # hasard. On la coupe alors, et on la reprend si le cache se vide.
        # On n'explore pas au hasard quand le cache a seulement ete mis de
        # cote faute de repere : sa vacuite est alors un artefact, pas un aveu
        # d'ignorance. Six tirages aleatoires par phase, cinq secondes chacun,
        # echouaient ainsi alors que la detection avait dix remparts a offrir.
        explores = []
        if len(connus) < EXPLORE_JUSQUA and not sans_repere:
            explores = [p for p in explore_points(rng)
                        if not proche(p, ecartes) and not proche(p, connus)
                        and not proche(p, maximes)]
        # Le compte des candidats, par provenance : sans lui, une phase qui
        # n'en trouve qu'un ne se distingue pas d'une phase ou tout a rate.
        if verbose and not tried:
            print(f"[i] candidats : {len(detectes)} detectes, {len(connus)} "
                  f"connus, {len(explores)} explores")
        cands = [p for p in connus if not proche(p, tried)
                 and not proche(p, ecartes) and not proche(p, maximes)
                 and point_taquable(p)] + \
                [p for p in detectes if not proche(p, tried) and not proche(p, connus)] + \
                [p for p in explores if not proche(p, tried)]
        if not cands:
            # Le vivier s'est vide : a force d'ecarter, la liste finit par
            # couvrir tout le village et plus aucun candidat ne passe. On
            # repart des observations plutot que de renoncer - mais sans
            # rendre au vivier les murs deja au maximum, qui ne redeviendront
            # jamais ameliorables. Les redecouvrir coutait cinq secondes
            # chacun, quarante et un d'affilee apres une seule remise a zero.
            if ecartes:
                if verbose:
                    print(f"[i] plus aucun candidat, on oublie les {len(ecartes)}"
                          " points ecartes et on recommence")
                ecartes.clear()
                suspects.clear()
                continue
            if verbose:
                print("[i] plus de rempart a ameliorer")
            break
        # On pioche parmi tous les murs reperes, et non parmi les plus gros :
        # se limiter aux premiers revenait a remonter sans cesse les memes
        # remparts en laissant les autres au niveau d'origine.
        point = rng.choice(cands)
        tried.add(point)

        def is_wall_selected(shot=None):
            # Le titre suffit, et vaut mieux que la presence d'un menu : la
            # rangee de boutons change selon le niveau du mur et le mode de
            # selection, si bien qu'un rempart clairement nomme etait rejete
            # parce que la case temoin ne contenait pas ce qu'on attendait.
            shot = phone.screenshot() if shot is None else shot
            return titre_est_rempart(selected_title(shot))

        def select(essais=2):
            """Selectionne le mur vise et confirme que c'en est bien un.

            On reessaie une fois : quand la selection suit la fermeture d'une
            fenetre, le jeu est encore en train de l'escamoter et avale le
            premier tap. C'est ce qui faisait renoncer a l'essai en elixir
            juste apres un refus en or.
            """
            for essai in range(essais):
                # Le second essai vise un peu plus bas. En vue isometrique, la
                # case sensible d'un rempart est a son pied, quand la detection
                # le repere a sa coiffe doree : retaper au meme pixel ne faisait
                # que repeter l'echec. Verification faite sur la vue recentree
                # d'une phase reelle, les trente-trois candidats tombaient tous
                # sur un mur - ce n'est donc pas la detection qui rate, c'est le
                # tap qui n'accroche pas.
                phone.tap(point[0], point[1] + essai * MUR_DECALAGE)
                time.sleep(1.2)
                if is_wall_selected():
                    return True
                time.sleep(0.8)     # laisser l'animation se terminer
                # Relire l'ecran avant de retaper, et non apres. Retaper un mur
                # deja selectionne le deselectionne : une verification prise
                # trop tot, pendant que le menu s'ouvre encore, faisait donc
                # defaire par le second tap ce que le premier avait reussi. Le
                # mur etait alors compte comme non selectionne, et deux echecs
                # de cette sorte l'ecartaient pour de bon.
                if is_wall_selected():
                    return True
            return False

        if not select():
            # Un point n'est ecarte qu'apres deux echecs : une selection peut
            # rater pour une raison passagere, une animation en cours par
            # exemple, et bannir un vrai rempart des le premier essai
            # appauvrirait le vivier nuit apres nuit.
            if proche(point, suspects):
                if not proche(point, ecartes):
                    ecartes.append(point)
                connus[:] = [p for p in connus if not proche(p, [point])]
            else:
                suspects.append(point)
            # Une seule capture sert au journal et a la decision qui suit :
            # elle coute pres de neuf cents millisecondes, et ce chemin est le
            # plus frequent d'une phase de remparts - celle qui en demandait
            # cent vingt-huit a elle seule.
            shot = phone.screenshot()
            if verbose:
                venu = SOURCE_POINT.get((round(point[0]), round(point[1])),
                                        "cache/exploration")
                print(f"    mur ({int(point[0])},{int(point[1])}) [{venu}] non "
                      f"selectionne (menu={menu_open(shot)}, "
                      f"titre={selected_title(shot)!r})")
            # Le tap a manque le mur. S'il n'a rien selectionne du tout, il ne
            # faut surtout pas appuyer sur retour : au village, cela ouvre la
            # confirmation de sortie du jeu.
            if menu_open(shot):
                retour(phone, "selection-ratee/menu")
                time.sleep(0.7)
            continue

        # Le prix rouge est lu dans pay_upgrade, une fois le mur selectionne :
        # hors de son menu, les boutons n'existent pas et l'information n'est
        # pas disponible.
        if not proche(point, connus):
            connus.append(point)     # ce point repond bien comme un rempart
        apprend_motif(depart, point)

        paye = False
        issues = set()
        # Quelles ressources ce mur aura reellement vu passer : une reserve
        # deja jugee insuffisante est sautee, et conclure sur un examen
        # partiel reviendrait a condamner un mur sans l'avoir teste.
        ignorees = set(epuisees)
        for resource in [r for r in order if r not in epuisees]:
            # Selon la facon dont l'essai precedent s'est termine, le mur est
            # encore selectionne ou non. Retaper un mur deja selectionne le
            # deselectionne : il faut donc verifier avant, pas re-cliquer a
            # l'aveugle.
            if not is_wall_selected() and not select():
                break
            issue = pay_upgrade(phone, templates, resource)
            issues.add(issue)
            if issue == "paye":
                upgraded += 1
                paye = True
                order.remove(resource)
                order.insert(0, resource)
                if verbose:
                    print(f"[+] rempart ameliore en {resource}")
                break
            if issue == "hors-portee":
                # Le jeu a ecrit le prix en rouge : signal economique explicite.
                hors_portee = True
                # Mais ce prix est celui de ce mur-la. Les remparts du village
                # ne sont pas tous au meme niveau, donc pas au meme prix : un
                # mur inabordable ne dit rien de son voisin moins avance.
                # Renoncer des le premier rouge terminait la phase sans rien
                # monter alors que des murs payables attendaient. Le rouge se
                # lit dans le menu deja ouvert, sans un seul tap de plus : en
                # essayer quelques-uns ne coute que la selection.
                rouges[resource] = rouges.get(resource, 0) + 1
                if rouges[resource] >= PRIX_ROUGE_MAX:
                    epuisees.add(resource)
            if issue == "refuse":
                # Le paiement a bien ete refuse : cette reserve ne suffit plus
                # et ne remontera pas d'ici la fin de la serie. Inutile de la
                # represser mur apres mur, chaque essai coutant une quinzaine
                # de secondes et laissant le mur deselectionne.
                epuisees.add(resource)
            # "indisponible" ne dit rien de la reserve : le jeu n'a pas propose
            # l'amelioration sur ce mur-la. La marquer epuisee ecartait une
            # ressource pourtant abondante pour tout le reste de la serie.
            order.remove(resource)
            order.append(resource)
        if not paye:
            # Un mur rate ne condamne pas les suivants. Un refus en or referme
            # la fenetre d'achat de gemmes, ce qui deselectionne le mur : la
            # tentative en elixir sur ce mur-la echouait donc aussi, et sortir
            # de la boucle ici revenait a ne jamais essayer l'elixir, meme avec
            # des dizaines de millions en reserve. On passe au mur suivant,
            # cette fois en commencant par la ressource qui reste.
            if issues == {"indisponible"} and not ignorees:
                # Le mur s'est bien selectionne - son menu s'est ouvert et
                # portait son nom - mais le jeu n'y propose aucune
                # amelioration, dans aucune des deux ressources : il est au
                # niveau maximum. Y revenir phase apres phase coutait cinq
                # secondes a chaque fois, et le village en compte des rangees
                # entieres. On l'ecarte sans rien conclure sur les reserves :
                # c'est ce mur-la qui n'a plus rien a monter, pas l'or ou
                # l'elixir qui manquent. L'alarme reste donc armee, et
                # parlerait quand meme si la lecture des boutons tombait en
                # panne au point de faire passer tous les murs pour maximes.
                #
                # Encore faut-il l'avoir vraiment constate : la conclusion ne
                # vaut que si toutes les ressources ont ete essayees. Un mur a
                # ete classe au maximum apres le seul examen de l'elixir, l'or
                # ayant ete mis de cote plus tot dans la phase - alors qu'il y
                # en avait sept millions. Ce classement etant definitif, il
                # aurait retire ce rempart du vivier pour toujours.
                if not proche(point, maximes):
                    maximes.append(point)
                connus[:] = [p for p in connus if not proche(p, [point])]
                if verbose:
                    print(f"    mur ({int(point[0])},{int(point[1])}) au maximum, ecarte")
            if len(epuisees) >= len(order):
                if verbose:
                    print(f"[i] plus assez de {' ni de '.join(sorted(epuisees))}"
                          " pour ameliorer")
                break
            if verbose:
                print(f"[i] echec sur ce mur, on en essaie un autre "
                      f"(reste : {', '.join(r for r in order if r not in epuisees)})")
            time.sleep(1.0)     # laisser le jeu se stabiliser
            continue

    back_to_home(phone, templates)
    fin = phone.screenshot()
    # Les reserves ne se lisent qu'au village. Le bilan est pris juste apres y
    # etre revenu, quand l'ecran peut encore etre en train de s'installer : une
    # lecture ratee la, et la surveillance saute son tour sans rien dire. C'est
    # la panne qui laisse une alarme muette toute une nuit.
    stocks = read_stocks(fin, templates)
    for _ in range(3):
        if stocks is not None:
            break
        # Recapturer ne suffit pas : ce qui rend l'ecran illisible, c'est une
        # fenetre restee ouverte, et elle ne se fermera pas toute seule. Les
        # deux cas rencontres etaient la confirmation de sortie du jeu, dont le
        # bouton OK tombe en pleine zone ou l'on tape des remparts. Il faut la
        # refermer, pas attendre.
        time.sleep(1.5)
        back_to_home(phone, templates)
        fin = phone.screenshot()
        stocks = read_stocks(fin, templates)
    if stocks is None:
        print(f"[!] reserves illisibles ({identify(fin, templates)[0]}, dernier "
              f"retour : {DERNIER_RETOUR}) : la surveillance ne peut rien "
              "conclure sur cette phase", flush=True)
        record_unknown(fin, "reserves-illisibles")
    gemmes_apres = read_gems(fin, templates)
    surveille_stocks(stocks, upgraded, hors_portee, verbose)
    if (gemmes_avant is not None and gemmes_apres is not None
            and gemmes_apres < gemmes_avant):
        MURS_INTERDITS = True
        print(f"[!] GEMMES DEPENSEES : {gemmes_avant} -> {gemmes_apres}. "
              "Amelioration des remparts desactivee.", flush=True)

    if decalage is not None:
        # Deja exprimes dans la vue de cette phase, qui sera la precedente de
        # la prochaine.
        save_wall_cache(connus, ecartes, suspects, maximes)
    if verbose:
        print(f"[i] repere : {len(connus)} remparts connus, {len(ecartes)} points"
              f" ecartes, {len(maximes)} murs au maximum")
    back_to_home(phone, templates)
    return upgraded


# --------------------------------------------------------------------------
# Enchainement des ecrans
# --------------------------------------------------------------------------

def wait_for(phone, templates, wanted, timeout, poll=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        screen, _ = identify(phone.screenshot(), templates)
        if screen in wanted:
            return screen
        time.sleep(poll)
    return None


def goto_battle(phone, templates, timeout=150):
    """Depuis le village : Attaquer -> Trouver une partie -> Attaquer.

    On attend que l'ecran change, on ne dort plus un temps fixe. Trois secondes
    apres chaque tap, cinq pour l'ecran d'armee, faisaient onze secondes de
    pauses aveugles par attaque, que le jeu reponde en une seconde ou en
    quatre. Retaper le meme ecran serait le seul danger - un second appui sur
    Attaquer peut refermer ce qu'on vient d'ouvrir - alors on ne retape qu'un
    ecran qui s'obstine.
    """
    deadline = time.time() + timeout
    unknown = 0
    agi_sur, agi_a = None, 0.0
    while time.time() < deadline:
        img = phone.screenshot()
        screen, score = identify(img, templates)

        if screen == "battle":
            return True
        if screen == "idle":
            print("[i] deconnexion pour inactivite, rechargement du jeu")
            phone.tap(*IDLE_RELOAD)
            time.sleep(12.0)
            continue
        if screen == "cancel":
            phone.tap(*CANCEL_BUTTON)
            time.sleep(1.2)
            continue
        if screen == "confirm":
            # Fenetre d'amelioration restee ouverte : elle n'a rien a faire
            # ici, on la referme sans y toucher.
            retour(phone, "goto-battle/confirm")
            time.sleep(1.2)
            continue
        if screen is None:
            if is_transition(img):
                time.sleep(0.8)     # fondu en cours : laisser l'ecran arriver
                continue
            unknown += 1
            # Popup (noter l'appli, offre du jour...) : un retour arriere suffit
            if unknown >= 3:
                print(f"[i] ecran inconnu (score {score:.0f}), retour arriere")
                record_unknown(img, "navigation")
                retour(phone, "navigation/inconnu")
                unknown = 0
            time.sleep(1.0)
            continue

        unknown = 0
        if screen == agi_sur and time.time() - agi_a < GOTO_PATIENCE:
            time.sleep(0.25)        # le jeu n'a pas encore bascule
            continue
        print(f"[>] {screen}")
        phone.tap(*SCREENS[screen]["tap"])
        agi_sur, agi_a = screen, time.time()
        time.sleep(GOTO_APRES_TAP)

    return False


def pick_village(phone, templates, args):
    """Passe les villages trop pauvres.

    Renvoie (convient, butin) : le butin du village retenu sert de reference
    pour savoir, pendant le combat, ce qu'il en reste.
    """
    for attempt in range(args.max_skips + 1):
        img = phone.screenshot()
        loot = read_loot(img, args.min_loot)
        good, detail = loot_is_good(loot, args.min_loot)
        if good and "douteuse" in detail:
            # L'affichage du butin se met en place avec un temps de retard :
            # une seconde lecture leve souvent le doute, et evite d'attaquer
            # un village pauvre par prudence.
            time.sleep(1.2)
            img = phone.screenshot()
            loot = read_loot(img, args.min_loot)
            good, detail = loot_is_good(loot, args.min_loot)
        if good or args.min_loot <= 0:
            print(f"[i] village retenu ({detail})")
            # Le village retenu, lui, se lit en entier : c'est la reference a
            # laquelle on comparera le butin restant pendant le combat. La
            # lecture paresseuse ne rend rien quand le comptage a suffi, et
            # sans reference la reddition ne peut plus se declencher - elle a
            # cesse de le faire des l'heure ou j'ai introduit cette paresse.
            # Le cout d'une lecture complete se paie une fois par attaque, pas
            # une fois par village examine.
            # Le repli doit lire l'image la plus recente : celle d'avant la
            # relecture montrait un butin pas encore affiche, et rendait un
            # butin de reference vide - donc pas de reddition possible.
            reference = loot if butin_total(loot) else read_loot(img)
            if butin_total(reference) is None:
                time.sleep(1.0)
                reference = read_loot(phone.screenshot())
            return True, reference
        print(f"[i] village passe ({detail} < {args.min_loot})")
        phone.tap(*NEXT_BUTTON)
        time.sleep(4.0)
        if not wait_for(phone, templates, {"battle"}, 30):
            print("[!] plus dans un village apres 'Suivant'")
            return False, {}
    print(f"[i] {args.max_skips} villages passes, on attaque celui-ci")
    return True, read_loot(phone.screenshot())   # lecture complete, cf. plus haut


def confirme_fin_combat(phone, templates):
    """Repond a la demande de confirmation de fin de bataille, s'il y en a une.

    Le jeu la demande parfois, parfois non - une fin provoquee a la main est
    passee directement au village. C'est la seule fenetre Annuler / OK a
    laquelle on repond OK : partout ailleurs ce bouton achete des gemmes ou
    ferme Clash of Clans, et la regle du programme est de toujours refuser. On
    ne fait donc exception que dans les secondes qui suivent notre propre
    demande, et jamais si le texte parle de quitter le jeu.

    Sans cela la reddition ne servait a rien : le tap sur "Terminer la
    bataille" partait bien, puis sa confirmation etait annulee.
    """
    for _ in range(3):
        img = phone.screenshot()
        if not cancel_dialog_open(img):
            time.sleep(1.0)
            continue
        texte = texte_dialogue(img).lower()
        record_unknown(img, "confirmation-fin-combat")
        if "quitter" in texte:
            print("[!] confirmation de sortie du jeu, refusee")
            phone.tap(*CANCEL_BUTTON)
            return False
        phone.tap(*CANCEL_OK)
        print("[i] fin de bataille confirmee")
        time.sleep(1.5)
        return True
    return False


def attend_fin_combat(phone, templates, args, butin_depart):
    """Attend la fin du combat, en l'abregeant quand le village est vide.

    Renvoie l'ecran atteint, ou None si le chronometre du programme expire.

    Le butin affiche pendant l'attaque est celui qui reste a prendre, pas celui
    deja pris : il decroit a mesure qu'on pille, et sa chute sous un dixieme du
    depart dit que le village ne rapportera plus rien. Une lecture incomplete
    ne conclut jamais - mieux vaut laisser le chronometre courir que rendre la
    main sur un village encore plein parce qu'une ressource n'a pas ete lue.
    """
    depart = butin_depart or {}
    if not butin_par_ressource(depart):
        print("[i] butin de depart illisible : le combat ira a son terme")
    debut = time.time()
    fin = debut + args.max_battle
    prochaine_lecture = time.time() + BUTIN_INTERVALLE
    demande = False
    bas = 0                 # lectures consecutives sous le seuil
    while time.time() < fin:
        img = phone.screenshot()
        ecran = identify(img, templates)[0]
        if ecran in ("result", "home"):
            return ecran
        if (not demande and butin_par_ressource(depart) and ecran == "battle"
                and time.time() >= prochaine_lecture):
            prochaine_lecture = time.time() + BUTIN_INTERVALLE
            part = part_restante(depart, read_loot(img))
            if part is not None:
                print(f"[i] butin restant {part:.0%}")
                # Deux lectures basses de suite avant de conclure. La premiere
                # reddition s'est declenchee sur la toute premiere lecture,
                # douze secondes apres le debut du combat : un village vide en
                # douze secondes est peu vraisemblable, et une lecture fausse
                # coute du butin reel. Le prix de la prudence est douze
                # secondes de combat en plus.
                bas = bas + 1 if part <= BUTIN_RATIO_FIN else 0
                if bas >= BUTIN_CONFIRMATIONS:
                    print("[i] village vide, on termine le combat")
                    phone.tap(*SCREENS["battle"]["tap"])
                    time.sleep(1.5)
                    confirme_fin_combat(phone, templates)
                    demande = True
        # Une capture coute pres de neuf cents millisecondes, et elles pesent
        # vingt-neuf pour cent du cycle. Les premieres secondes d'un combat ne
        # peuvent rien terminer - les troupes viennent d'etre posees - alors on
        # y regarde deux fois moins souvent. Le reste du temps, la cadence
        # habituelle, pour ne pas laisser trainer un combat fini.
        calme = (time.time() - debut) < BATAILLE_CALME
        time.sleep(BATAILLE_POLL_CALME if calme else BATAILLE_POLL)
    return None


def end_battle(phone, templates, args, butin_depart=None):
    """Laisse le combat se conclure, puis rentre au village.

    On ne touche pas a "Terminer la bataille" tant que le village rapporte
    encore : les troupes ont besoin de temps pour casser les remparts. Mais un
    combat continue de courir apres que le village est vide - deux minutes sept
    de chronometre restant pour un butin deja tombe a 631 948 d'or, sur la
    mesure qui a motive ce changement - et ces minutes sont du farm perdu.
    """
    print(f"=== Combat en cours (max {args.max_battle}s) ===")
    screen = attend_fin_combat(phone, templates, args, butin_depart)
    if screen is None:
        print("[i] combat toujours en cours, on le termine")
        phone.tap(*SCREENS["battle"]["tap"])
        time.sleep(2.5)

    deadline = time.time() + 90
    unknown = 0
    while time.time() < deadline:
        img = phone.screenshot()
        screen, score = identify(img, templates)
        if screen == "home":
            return True
        if screen in ("battle", "result"):
            phone.tap(*SCREENS[screen]["tap"])
            unknown = 0
            time.sleep(3.0 if screen == "battle" else 2.0)
            continue
        if screen == "idle":
            phone.tap(*IDLE_RELOAD)
            time.sleep(12.0)
            continue
        if screen == "cancel":
            phone.tap(*CANCEL_BUTTON)
            time.sleep(1.2)
            continue
        if screen == "confirm":
            retour(phone, "fin-combat/confirm")
            time.sleep(1.2)
            continue
        if is_transition(img):
            time.sleep(1.0)     # fondu de sortie de combat : laisser passer
            continue
        unknown += 1
        print(f"[i] ecran inconnu en fin de combat (score {score:.0f})")
        record_unknown(img, "fin-combat")
        # On ne tape plus au jugé au centre de l'ecran : le point utilise
        # jusqu'ici tombait a cinquante pixels du bouton OK qui ferme le jeu.
        # Le retour arriere referme les fenetres sans ce risque, et la
        # confirmation de sortie qu'il declencherait est geree juste au-dessus.
        retour(phone, "fin-combat/fermeture")
        time.sleep(2.0)
    return False


def one_round(phone, templates, args, rng):
    global CAPTURES
    CAPTURES = 0
    debut_tour = time.time()
    print("=== Recherche d'un adversaire ===")
    t0 = time.time()
    if not goto_battle(phone, templates):
        print("[!] impossible d'atteindre le combat")
        return False

    t_nav = time.time() - t0
    t0 = time.time()
    convient, butin_depart = pick_village(phone, templates, args)
    t_choix = time.time() - t0
    if not convient:
        return False

    print(f"=== Deploiement (cote {args.side}) ===")
    t0 = time.time()
    emptied, total = deploy_all(phone, templates, args, rng)
    t_depot = time.time() - t0
    print(f"[+] {emptied}/{total} slots vides")

    if args.probe:
        print("[i] mode probe : on s'arrete la, le combat reste ouvert")
        return True

    if args.end_early:
        phone.tap(*SCREENS["battle"]["tap"])
        time.sleep(2.5)

    t0 = time.time()
    if not end_battle(phone, templates, args, butin_depart):
        print("[!] retour au village incertain")
        return False
    t_combat = time.time() - t0
    print("[+] rentre au village")

    if args.walls > 0:
        print(f"=== Remparts (max {args.walls}) ===")
        n = upgrade_walls(phone, templates, args, rng)
        print(f"[+] {n} rempart(s) ameliore(s)")
    duree = time.time() - debut_tour
    print(f"[i] tour en {duree:.0f}s, {CAPTURES} captures "
          f"({CAPTURES * 0.88:.0f}s a l'ecran) | navigation {t_nav:.0f}s, "
          f"choix {t_choix:.0f}s, depot {t_depot:.0f}s, combat {t_combat:.0f}s, "
          f"remparts {time.time() - t0 - t_combat:.0f}s")
    return True


def source_mtime():
    return os.path.getmtime(os.path.abspath(__file__))


def relance_si_code_modifie(depart, restantes):
    """Redemarre le programme si son source a change depuis le lancement.

    Python ne recharge pas un module deja en memoire : sans cela, une
    correction n'est prise en compte qu'en tuant le processus, ce qui gache
    l'attaque en cours. On ne se relance donc qu'entre deux attaques, jamais
    pendant un combat, en reportant le nombre d'attaques restantes.
    """
    if restantes <= 0 or source_mtime() == depart:
        return
    # Se relancer sur un fichier a moitie ecrit tue le programme au demarrage
    # suivant : une edition en deux temps a ainsi coute une attaque, l'appel a
    # une variable etant deja la et sa creation pas encore. On verifie donc que
    # le source tient debout avant de lui confier la suite ; sinon on continue
    # avec le code en memoire et on reessaiera apres l'attaque suivante.
    try:
        with open(os.path.abspath(__file__)) as f:
            compile(f.read(), __file__, "exec")
    except (OSError, SyntaxError, ValueError) as e:
        print(f"[i] source en cours d'ecriture ({type(e).__name__}), "
              "relance reportee", flush=True)
        return
    argv, saute = [], False
    for a in sys.argv[1:]:
        if saute:
            saute = False
            continue
        if a == "--rounds":
            saute = True
            continue
        if a.startswith("--rounds="):
            continue
        argv.append(a)
    print(f"[i] code mis a jour : relance pour les {restantes} attaques restantes",
          flush=True)
    os.execv(sys.executable,
             [sys.executable, "-u", os.path.abspath(__file__),
              *argv, "--rounds", str(restantes)])


def main():
    p = argparse.ArgumentParser(description="Attaque automatique Clash of Clans")
    p.add_argument("--device", help="identifiant ADB du telephone")
    p.add_argument("--side", default="all",
                   choices=["all", "left", "right", "top", "bottom"],
                   help="ou deployer : un cote precis, ou 'all' pour repartir "
                        "sur tout le pourtour (defaut: all)")
    p.add_argument("--points", type=int, default=2,
                   help="couloirs sondes par cote ; les intermediaires sont deduits (defaut: 2)")
    p.add_argument("--probe-steps", type=int, default=3,
                   help="profondeurs sondees par couloir (defaut: 3)")
    p.add_argument("--jitter", type=float, default=25,
                   help="dispersion aleatoire autour d'un point, en pixels (defaut: 25)")
    p.add_argument("--rounds", type=int, default=1, help="nombre d'attaques")
    p.add_argument("--burst", type=int, default=60,
                   help="taps par slot de troupes et par passe (defaut: 60). "
                        "Chaque tap pose une unite et coute ~40 ms ; un appui "
                        "maintenu, lui, n'en pose que deux ou trois.")
    p.add_argument("--hero-taps", type=int, default=3,
                   help="taps pour un heros ou une machine de siege (defaut: 3)")
    p.add_argument("--max-passes", type=int, default=8,
                   help="nb max de passes de deploiement (defaut: 8)")
    p.add_argument("--spell-taps", type=int, default=8,
                   help="taps par passe pour les sorts (defaut: 8)")
    p.add_argument("--min-loot", type=int, default=1500000,
                   help="or OU elixir minimum, sinon on passe au village suivant "
                        "(0 pour desactiver, defaut: 1500000)")
    p.add_argument("--max-skips", type=int, default=8,
                   help="nb max de villages passes d'affilee (defaut: 8)")
    p.add_argument("--max-battle", type=int, default=210,
                   help="duree max d'un combat avant de le terminer (defaut: 210)")
    p.add_argument("--end-early", action="store_true",
                   help="terminer le combat des le deploiement fini")
    p.add_argument("--probe", action="store_true",
                   help="deploie puis s'arrete sans terminer le combat")
    p.add_argument("--wall-zone", default="tout", choices=list(ZONES_MURS),
                   help="ou chercher les remparts : 'droite', 'gauche' ou "
                        "'tout' (defaut). Les concentrer d'un cote evite de "
                        "perdre cinq secondes par essai a l'autre bout du village")
    p.add_argument("--walls", type=int, default=5,
                   help="remparts a ameliorer apres chaque attaque, en or ou en "
                        "elixir (0 pour desactiver, defaut: 5)")
    p.add_argument("--random", type=float, default=1.0,
                   help="dose d'aleatoire : 0 = deroulement identique a chaque "
                        "attaque, 1 = normal, 2 = tres disperse (defaut: 1)")
    p.add_argument("--seed", type=int,
                   help="graine aleatoire ; a ne fixer que pour rejouer une "
                        "attaque a l'identique")
    p.add_argument("--unknown-dir", default="unknown",
                   help="dossier ou archiver les ecrans non reconnus, pour "
                        "pouvoir les traiter ensuite (vide pour desactiver)")
    args = p.parse_args()

    global UNKNOWN_DIR, ZONE_MURS
    ZONE_MURS = args.wall_zone
    UNKNOWN_DIR = args.unknown_dir or None
    if UNKNOWN_DIR and not os.path.isabs(UNKNOWN_DIR):
        UNKNOWN_DIR = os.path.join(HERE, UNKNOWN_DIR)

    rng = random.Random(args.seed)
    phone = Phone(args.device)
    templates = load_templates()

    if not phone.app_running():
        print("[i] lancement de Clash of Clans")
        phone.launch()
        time.sleep(15)

    echecs = 0
    mtime_depart = source_mtime()
    for n in range(1, args.rounds + 1):
        print(f"\n########## Attaque {n}/{args.rounds} ##########")
        try:
            ok = one_round(phone, templates, args, rng)
        except Exception as exc:                      # noqa: BLE001
            # Une serie de cent attaques ne doit pas tomber sur un imprevu :
            # on note, on remet le jeu d'aplomb, et on repart.
            print(f"[!] erreur pendant l'attaque : {exc!r}")
            ok = False

        if ok:
            echecs = 0
        else:
            echecs += 1
            print(f"[!] attaque ratee ({echecs} d'affilee), tentative de reprise")
            if not back_to_home(phone, templates, tries=6):
                print("[i] relance de Clash of Clans")
                phone.launch()
                time.sleep(25)
            if echecs >= 5:
                print("[!] cinq echecs consecutifs, arret")
                break
        if n < args.rounds:
            relance_si_code_modifie(mtime_depart, args.rounds - n)
            time.sleep(3)

    print("\nTermine.")


if __name__ == "__main__":
    main()
