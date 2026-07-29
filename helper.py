"""Injection de gestes a plusieurs doigts, sans racine.

Poser plusieurs troupes en meme temps demande d'envoyer plusieurs contacts
simultanes. `input tap` n'en sait poser qu'un, et ecrire directement dans
/dev/input est refuse par SELinux sur ce telephone - la voie que j'avais
exploree et declaree sans issue.

Il en reste une : `app_process` lance une machine Java sur le telephone avec
les droits du shell, et de la on peut appeler `InputManagerGlobal.injectInputEvent`,
qui accepte des MotionEvent a autant de doigts qu'on veut. C'est la methode du
bot de Theo, reprise telle quelle avec sa classe Java.

Le programme compile la classe a la premiere utilisation, la depose sur le
telephone, et garde le resultat en cache : la compilation ne se refait que si
la source change.
"""

import hashlib
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "android_helper", "GestureInjector.java")
CACHE = os.path.join(HERE, "helper_cache")
DISTANT = "/data/local/tmp/coc-gestes.zip"
CLASSE = "coc.farm2.GestureInjector"
API_MIN = 24
SDK = os.path.expanduser("~/Android/Sdk")


class HelperIndisponible(RuntimeError):
    """La chaine de compilation ou le telephone n'ont pas suivi."""


def _outillage():
    """javac, d8 et le android.jar, ou une explication de ce qui manque."""
    javac = _premier_existant(["/usr/bin/javac", "javac"])
    if javac is None:
        raise HelperIndisponible("javac introuvable")
    d8 = _plus_recent(os.path.join(SDK, "build-tools"), "d8")
    if d8 is None:
        raise HelperIndisponible(f"d8 introuvable sous {SDK}/build-tools")
    jar = _plus_recent(os.path.join(SDK, "platforms"), "android.jar")
    if jar is None:
        raise HelperIndisponible(f"android.jar introuvable sous {SDK}/platforms")
    return javac, d8, jar


def _premier_existant(chemins):
    for c in chemins:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        if not os.path.isabs(c):
            trouve = subprocess.run(["which", c], capture_output=True,
                                    text=True).stdout.strip()
            if trouve:
                return trouve
    return None


def _plus_recent(racine, nom):
    """Le fichier `nom` de la version la plus recente sous `racine`."""
    if not os.path.isdir(racine):
        return None
    for version in sorted(os.listdir(racine), reverse=True):
        chemin = os.path.join(racine, version, nom)
        if os.path.exists(chemin):
            return chemin
    return None


def construit():
    """Compile la classe en archive dex. Renvoie son chemin local.

    Le nom porte l'empreinte de la source : une modification donne un nouveau
    fichier, et rien ne se recompile tant qu'elle ne bouge pas.
    """
    try:
        source = open(SOURCE, "rb").read()
    except OSError as e:
        raise HelperIndisponible(f"source du helper illisible : {e}") from e
    empreinte = hashlib.sha256(source).hexdigest()[:16]
    archive = os.path.join(CACHE, f"gestes-{empreinte}.zip")
    if os.path.isfile(archive) and os.path.getsize(archive) > 0:
        return archive

    javac, d8, jar = _outillage()
    os.makedirs(CACHE, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
        classes = os.path.join(tmp, "classes")
        os.makedirs(classes)
        _lance([javac, "-encoding", "UTF-8", "-source", "8", "-target", "8",
                "-Xlint:-options", "-classpath", jar, "-d", classes, SOURCE],
               "javac")
        produits = [os.path.join(r, f) for r, _, fs in os.walk(classes)
                    for f in fs if f.endswith(".class")]
        if not produits:
            raise HelperIndisponible("javac n'a produit aucune classe")
        provisoire = os.path.join(tmp, "gestes.zip")
        _lance([d8, "--min-api", str(API_MIN), "--output", provisoire, *produits],
               "d8")
        if not os.path.isfile(provisoire) or os.path.getsize(provisoire) == 0:
            raise HelperIndisponible("d8 n'a produit aucune archive")
        os.replace(provisoire, archive)
    return archive


def _lance(commande, nom):
    r = subprocess.run(commande, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise HelperIndisponible(
            f"{nom} a echoue : {(r.stderr or r.stdout).strip()[:300]}")


def installe(device, adb="adb"):
    """Depose l'archive sur le telephone. Renvoie le chemin distant."""
    archive = construit()
    r = subprocess.run([adb, "-s", device, "push", archive, DISTANT],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise HelperIndisponible(
            f"envoi du helper refuse : {(r.stderr or r.stdout).strip()[:200]}")
    return DISTANT


def commande_multi(doigts):
    """Ligne shell qui pose plusieurs doigts a la fois.

    `doigts` est une liste de trajets, un par doigt : [[(x, y, t_ms), ...], ...]
    ou t_ms compte a partir du poser. Le dernier point donne la duree.
    """
    if not doigts or len(doigts) > 10:
        raise ValueError("de un a dix doigts")
    morceaux = []
    for trajet in doigts:
        if not trajet:
            raise ValueError("un trajet vide n'a pas de sens")
        morceaux.append(str(len(trajet)))
        for x, y, t in trajet:
            morceaux.extend((str(int(x)), str(int(y)), str(int(t))))
    return (f"CLASSPATH={DISTANT} app_process / {CLASSE} multi "
            + " ".join(morceaux))


SESSION_DISTANTE = "/data/local/tmp/coc-session.txt"


def timeline_depot(carte, points, doigts=4, appui_ms=40, ecart_ms=28,
                   avant_ms=120):
    """Chronologie d'un depot : selectionner la carte, puis poser par accords.

    Renvoie le texte a donner au mode `session` : une ligne par evenement,
    "t_ms doigt x y phase". Tout tient dans une seule machine Java, y compris
    la selection de la carte et les pauses entre accords - c'est ce qui rend
    l'operation aussi rapide que la rafale de `input tap`, et non le fait de
    poser plusieurs doigts.

    Mesure sur soixante points : deux secondes vingt pour la rafale, deux
    secondes dix-neuf ici a quatre doigts, une seconde quatre-vingt-huit a six.
    Le vrai apport n'est donc pas la vitesse mais la simultaneite - quatre
    troupes qui tombent au meme instant en quatre endroits, ce qui etait
    demande des le debut et que `input tap` ne sait pas faire.
    """
    lignes = []
    t = 0
    if carte is not None:
        cx, cy = carte
        lignes.append(f"{t} 0 {int(cx)} {int(cy)} down")
        lignes.append(f"{t + appui_ms} 0 {int(cx)} {int(cy)} up")
        t += avant_ms
    for debut in range(0, len(points), doigts):
        accord = points[debut:debut + doigts]
        for i, (x, y) in enumerate(accord):
            lignes.append(f"{t} {i} {int(x)} {int(y)} down")
            lignes.append(f"{t + appui_ms} {i} {int(x)} {int(y)} up")
        t += appui_ms + ecart_ms
    return "\n".join(lignes) + "\n"


def commande_session(distant=SESSION_DISTANTE):
    return f"CLASSPATH={DISTANT} app_process / {CLASSE} session {distant}"
