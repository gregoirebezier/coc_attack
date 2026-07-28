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
# Un rempart se reconnait a deux couleurs, selon son niveau : le creme clair
# de son dessus (240,240,200), et la coiffe doree (240,224,80) qui apparait
# aux niveaux eleves. Chercher le seul creme laissait de cote les remparts
# deja montes, qu'il faut pourtant continuer a ameliorer.
WALL_CREAM = dict(r_min=215, g_min=198, b_min=165, rb_min=28, rb_max=80, rg_max=32)
WALL_GOLD = dict(r_min=215, g_min=195, b_max=125, rb_min=130, rg_max=45)
WALL_AREA_MIN = 400
WALL_MIN_RATIO = 1.8     # allongement minimal pour distinguer un mur d'un toit
# Le village ne bouge pas d'une attaque a l'autre : ce que l'on a identifie une
# fois reste vrai. On retient donc les points ou un rempart a repondu, et ceux
# ou l'on est tombe sur autre chose (une decoration doree, un batiment clair),
# pour ne plus y revenir. Chaque essai inutile coute cinq secondes.
WALL_CACHE = os.path.join(HERE, "walls.json")
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
# Seuil de presence d'une icone de ressource. Il doit laisser passer un vrai
# bouton d'amelioration (3.9 pour la piece, 5.9 pour la goutte) sans retenir
# "Choisir rangee", dont les fleches vertes tirent assez sur le jaune pour
# marquer 2.6. Tapant ce bouton par erreur, le programme selectionnait une
# rangee entiere de remparts et se retrouvait dans un mode ou ses reperes ne
# valaient plus rien.
ICON_MIN = 3.2
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

    def back(self):
        self._adb("shell", "input", "keyevent", "4")

    def screenshot(self):
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


def read_loot(img):
    """Lectures du butin, plusieurs par ressource.

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
    out = {}
    for name, (y0, y1) in LOOT_LINES.items():
        attendu = compte_chiffres(img, y0, y1)
        lectures = []
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


def loot_is_good(loot, minimum):
    """Ce village vaut-il l'attaque ?

    Il ne s'agit pas de connaitre le butin au chiffre pres, mais de savoir de
    quel cote du seuil il tombe. Deux sources y suffisent souvent sans que
    l'OCR ait besoin d'etre exact.

    Le nombre de chiffres visibles encadre d'abord la valeur : six chiffres,
    c'est moins d'un million, donc sous un seuil d'un million et demi, quoi
    qu'en dise l'OCR. Ce seul comptage tranche la plupart des cas.

    Restent les valeurs dont la longueur enjambe le seuil : les lectures y
    servent alors, et suffisent tant qu'elles tombent toutes du meme cote.
    Quand elles l'encadrent, ou qu'aucune n'a abouti, on attaque : passer un
    village correct coute plus cher qu'en attaquer un pauvre.
    """
    detail, riche, pauvre = [], False, True
    for nom in ("or", "elixir"):
        info = loot.get(nom) or {}
        lectures = info.get("lectures") or []
        chiffres = info.get("chiffres") or 0

        if chiffres:
            if 10 ** chiffres - 1 < minimum:        # trop court pour atteindre
                detail.append(f"{nom}<{10 ** chiffres}")
                continue
            if 10 ** (chiffres - 1) >= minimum:     # trop long pour rester sous
                detail.append(f"{nom}>{10 ** (chiffres - 1)}")
                riche = True
                continue

        if not lectures:
            detail.append(f"{nom}=?")
            pauvre = False
            continue
        bas, haut = min(lectures), max(lectures)
        detail.append(f"{nom}={bas}" if bas == haut else f"{nom}={bas}~{haut}")
        if bas >= minimum:
            riche = True
        elif haut >= minimum:
            pauvre = False      # le seuil tombe entre deux lectures
    texte = " ".join(detail)
    if riche:
        return True, texte
    if pauvre:
        return False, texte
    return True, texte + " (lecture douteuse, on attaque)"


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
                phone.select_and_burst(cx, SLOT_TAP_Y, spell_points(args.spell_taps, rng))
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

def wall_candidates(img):
    """Points a taper pour selectionner un rempart, du plus gros au plus petit.

    On cherche le dessus creme des murs plutot que leur liseré doré : le doré
    est trop fin et se confond avec les dorures des batiments, alors que le
    creme forme de larges rubans que seuls les remparts presentent.
    """
    import cv2
    x0, y0, x1, y1 = VILLAGE_AREA
    z = img[y0:y1, x0:x1]
    r, g, b = z[:, :, 0], z[:, :, 1], z[:, :, 2]
    c = WALL_CREAM
    creme = ((r > c["r_min"]) & (g > c["g_min"]) & (b > c["b_min"]) &
             (r - b > c["rb_min"]) & (r - b < c["rb_max"]) &
             (abs(r - g) < c["rg_max"]))
    o = WALL_GOLD
    coiffe = ((r > o["r_min"]) & (g > o["g_min"]) & (b < o["b_max"]) &
              (r - b > o["rb_min"]) & (abs(r - g) < o["rg_max"]))
    mask = (creme | coiffe).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    keep = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] <= WALL_AREA_MIN:
            continue
        w, h = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        # Un rempart est un ruban : long dans un sens, mince dans l'autre. Un
        # toit de batiment, lui, est trapu. Ce seul critere ecarte l'essentiel
        # des batiments clairs, que le controle du titre rejetait ensuite au
        # prix de plusieurs secondes perdues a chaque fois.
        if max(w, h) / max(1, min(w, h)) >= WALL_MIN_RATIO:
            keep.append(i)
    keep.sort(key=lambda i: -stats[i, cv2.CC_STAT_AREA])
    return [(cent[i][0] + x0, cent[i][1] + y0) for i in keep]


def load_wall_cache():
    try:
        with open(WALL_CACHE) as f:
            data = json.load(f)
        return ([tuple(p) for p in data.get("murs", [])],
                [tuple(p) for p in data.get("autres", [])],
                [tuple(p) for p in data.get("suspects", [])])
    except (OSError, ValueError):
        return [], [], []


def save_wall_cache(murs, autres, suspects):
    try:
        with open(WALL_CACHE, "w") as f:
            json.dump({"murs": [list(p) for p in murs[-400:]],
                       "autres": [list(p) for p in autres[-400:]],
                       "suspects": [list(p) for p in suspects[-400:]]}, f)
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
    """Nom de l'objet selectionne, lu par OCR. Chaine vide si illisible."""
    try:
        import pytesseract
    except ImportError:
        return ""
    x0, y0, x1, y1 = TITLE_BOX
    c = img[y0:y1, x0:x1]
    mask = (c.min(axis=2) > 150).astype(np.uint8) * 255
    big = Image.fromarray(255 - mask).resize(((x1 - x0) * 3, (y1 - y0) * 3), Image.LANCZOS)
    return ocr(big, "--psm 7").strip()


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
        bandeau = img[750:798, cx + 35:cx + 125]
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


def back_to_home(phone, templates, tries=4):
    """Referme ce qui traine jusqu'a retrouver le village."""
    for i in range(tries):
        img = phone.screenshot()
        ecran = identify(img, templates)[0]
        if ecran == "home":
            return True
        if ecran == "cancel":
            # C'est notre propre retour arriere qui l'a ouvert : on annule.
            phone.tap(*CANCEL_BUTTON)
            time.sleep(1.2)
            continue
        if ecran == "confirm":
            phone.back()        # fenetre d'amelioration : on la referme
            time.sleep(1.2)
            continue
        if is_transition(img):
            time.sleep(1.0)     # fondu : rien a refermer, il faut attendre
            continue
        if i == tries - 1:
            record_unknown(img, "retour-village")
        phone.back()
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
        print(f"    [{resource}] bouton absent du menu")
        return "indisponible"

    if not cost_affordable(menu, boutons[resource][0]):
        # Le prix s'affiche en rouge : inutile de cliquer. On s'epargne la
        # fenetre d'achat de gemmes et les quinze secondes qu'elle coute.
        print(f"    [{resource}] prix en rouge, reserve insuffisante")
        return "refuse"

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
            if menu_open(shot) and not confirm_dialog_open(shot) \
                    and not cancel_dialog_open(shot):
                return "refuse"
            phone.back()
            time.sleep(1.0)
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
    upgraded = 0
    tried = set()
    # Ordre d'essai des ressources. Un essai qui echoue coute une quinzaine de
    # secondes, alors on retient celle qui vient de marcher et on relegue celle
    # qui manque.
    order = ["or", "elixir"]
    epuisees = set()        # reserves dont le paiement a deja ete refuse
    # Les suspects se conservent d'une attaque a l'autre, sans quoi la regle
    # des deux echecs ne se declenche jamais : un point rate une fois par
    # phase, la liste repart de zero, et le meme leurre coute cinq secondes a
    # chaque attaque de la nuit.
    connus, ecartes, suspects = load_wall_cache()
    for _ in range(args.walls * 3):
        if upgraded >= args.walls:
            break
        img = phone.screenshot()
        if identify(img, templates)[0] != "home":
            break

        # Les points deja reconnus comme remparts passent devant ; ceux ou
        # l'on est tombe sur autre chose sont ecartes d'office.
        detectes = [p for p in wall_candidates(img) if not proche(p, ecartes)]
        # Les points connus sont eux aussi soumis a la liste des ecartes :
        # sans quoi un point devenu muet y restait en tete et etait reessaye a
        # chaque phase, jusqu'a epuiser tout le budget en echecs.
        cands = [p for p in connus if not proche(p, tried) and not proche(p, ecartes)] + \
                [p for p in detectes if not proche(p, tried) and not proche(p, connus)]
        if not cands:
            # Le vivier s'est vide : a force d'ecarter, la liste finit par
            # couvrir tout le village et plus aucun candidat ne passe. On
            # repart des observations plutot que de renoncer.
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
                phone.tap(*point)
                time.sleep(1.2)
                if is_wall_selected():
                    return True
                time.sleep(0.8)     # laisser l'animation se terminer
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
            if verbose:
                shot = phone.screenshot()
                print(f"    mur ({int(point[0])},{int(point[1])}) non selectionne "
                      f"(menu={menu_open(shot)}, titre={selected_title(shot)!r})")
            # Le tap a manque le mur. S'il n'a rien selectionne du tout, il ne
            # faut surtout pas appuyer sur retour : au village, cela ouvre la
            # confirmation de sortie du jeu.
            if menu_open(phone.screenshot()):
                phone.back()
                time.sleep(0.7)
            continue

        # Le prix rouge est lu dans pay_upgrade, une fois le mur selectionne :
        # hors de son menu, les boutons n'existent pas et l'information n'est
        # pas disponible.
        if not proche(point, connus):
            connus.append(point)     # ce point repond bien comme un rempart

        paye = False
        for resource in [r for r in order if r not in epuisees]:
            # Selon la facon dont l'essai precedent s'est termine, le mur est
            # encore selectionne ou non. Retaper un mur deja selectionne le
            # deselectionne : il faut donc verifier avant, pas re-cliquer a
            # l'aveugle.
            if not is_wall_selected() and not select():
                break
            issue = pay_upgrade(phone, templates, resource)
            if issue == "paye":
                upgraded += 1
                paye = True
                order.remove(resource)
                order.insert(0, resource)
                if verbose:
                    print(f"[+] rempart ameliore en {resource}")
                break
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

    save_wall_cache(connus, ecartes, suspects)
    if verbose:
        print(f"[i] repere : {len(connus)} remparts connus, {len(ecartes)} points ecartes")
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
    """Depuis le village : Attaquer -> Trouver une partie -> Attaquer."""
    deadline = time.time() + timeout
    unknown = 0
    while time.time() < deadline:
        img = phone.screenshot()
        screen, score = identify(img, templates)

        if screen == "battle":
            return True
        if screen == "cancel":
            phone.tap(*CANCEL_BUTTON)
            time.sleep(1.2)
            continue
        if screen == "confirm":
            # Fenetre d'amelioration restee ouverte : elle n'a rien a faire
            # ici, on la referme sans y toucher.
            phone.back()
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
                phone.back()
                unknown = 0
            time.sleep(1.0)
            continue

        unknown = 0
        print(f"[>] {screen}")
        phone.tap(*SCREENS[screen]["tap"])
        time.sleep(5.0 if screen == "army" else 3.0)

    return False


def pick_village(phone, templates, args):
    """Passe les villages trop pauvres. Renvoie True si un village convient."""
    for attempt in range(args.max_skips + 1):
        img = phone.screenshot()
        loot = read_loot(img)
        good, detail = loot_is_good(loot, args.min_loot)
        if good and "douteuse" in detail:
            # L'affichage du butin se met en place avec un temps de retard :
            # une seconde lecture leve souvent le doute, et evite d'attaquer
            # un village pauvre par prudence.
            time.sleep(1.2)
            loot = read_loot(phone.screenshot())
            good, detail = loot_is_good(loot, args.min_loot)
        if good or args.min_loot <= 0:
            print(f"[i] village retenu ({detail})")
            return True
        print(f"[i] village passe ({detail} < {args.min_loot})")
        phone.tap(*NEXT_BUTTON)
        time.sleep(4.0)
        if not wait_for(phone, templates, {"battle"}, 30):
            print("[!] plus dans un village apres 'Suivant'")
            return False
    print(f"[i] {args.max_skips} villages passes, on attaque celui-ci")
    return True


def end_battle(phone, templates, args):
    """Laisse le combat se conclure, puis rentre au village.

    On ne touche pas a "Terminer la bataille" tant que le combat tourne : les
    troupes ont besoin de temps pour casser les remparts.
    """
    print(f"=== Combat en cours (max {args.max_battle}s) ===")
    screen = wait_for(phone, templates, {"result", "home"}, args.max_battle, poll=3.0)
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
        if screen == "cancel":
            phone.tap(*CANCEL_BUTTON)
            time.sleep(1.2)
            continue
        if screen == "confirm":
            phone.back()
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
        phone.back()
        time.sleep(2.0)
    return False


def one_round(phone, templates, args, rng):
    print("=== Recherche d'un adversaire ===")
    if not goto_battle(phone, templates):
        print("[!] impossible d'atteindre le combat")
        return False

    if not pick_village(phone, templates, args):
        return False

    print(f"=== Deploiement (cote {args.side}) ===")
    emptied, total = deploy_all(phone, templates, args, rng)
    print(f"[+] {emptied}/{total} slots vides")

    if args.probe:
        print("[i] mode probe : on s'arrete la, le combat reste ouvert")
        return True

    if args.end_early:
        phone.tap(*SCREENS["battle"]["tap"])
        time.sleep(2.5)

    if not end_battle(phone, templates, args):
        print("[!] retour au village incertain")
        return False
    print("[+] rentre au village")

    if args.walls > 0:
        print(f"=== Remparts (max {args.walls}) ===")
        n = upgrade_walls(phone, templates, args, rng)
        print(f"[+] {n} rempart(s) ameliore(s)")
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

    global UNKNOWN_DIR
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
