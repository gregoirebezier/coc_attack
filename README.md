# coc_attack

Lance des attaques Clash of Clans en boucle et déploie toutes les troupes
autour du village ennemi. Pilote un téléphone Android branché en USB via ADB.

```bash
python3 coc_attack.py --rounds 10
```

## Prérequis

- Le téléphone en USB, débogage ADB activé (`adb devices` doit le lister).
- Clash of Clans lancé, sur le village d'accueil.
- `pip install numpy pillow pytesseract` et `apt install tesseract-ocr`
  (l'OCR ne sert qu'à lire le butin ; sans lui, tous les villages sont attaqués).

## Ce que fait le programme

1. Village → **Attaquer** → **Trouver une partie** → **Attaquer**.
2. Lit le butin du village. S'il est trop pauvre, appuie sur **Suivant**.
3. Sonde les points de largage valides autour de la base.
4. Vide tous les slots : troupes en rafales de taps réparties sur tout le
   pourtour, sorts au milieu du village à des endroits tirés au hasard.
5. Laisse le combat se terminer, puis **Rentrer**.
6. De retour au village, améliore des remparts avec le butin ramené, en or ou
   en élixir. Et recommence.

## Options utiles

| Option | Défaut | Rôle |
|---|---|---|
| `--rounds N` | 1 | nombre d'attaques enchaînées |
| `--side` | `all` | `all`, `left`, `right`, `top`, `bottom` |
| `--min-loot N` | 900000 | or **ou** élixir minimum, sinon on passe au village suivant (`0` désactive) |
| `--burst N` | 60 | taps par slot de troupes et par passe |
| `--points N` | 2 | couloirs sondés par côté (plus = mieux réparti, sondage plus long) |
| `--walls N` | 5 | remparts à améliorer après chaque attaque (`0` désactive) |
| `--random F` | 1.0 | dose d'aléatoire : `0` = déroulement identique à chaque attaque, `2` = très dispersé |
| `--probe` | — | déploie puis laisse le combat ouvert, pour observer |

## Comment il « voit » le jeu

Clash of Clans est rendu dans une seule `SurfaceView` : un dump uiautomator ne
renvoie qu'un nœud, sans aucun élément d'interface. Tout passe donc par
l'image.

**Écrans.** Cinq imagettes de boutons (`templates/`) sont comparées à la zone
correspondante de l'écran. La différence moyenne est nulle sur le bon écran et
dépasse 75 sur tous les autres, donc un seuil à 40 sépare sans ambiguïté.

**Slots de troupes.** Le cadre bas des cartes forme des segments clairs très
nets sur le fond sombre de la barre : on balaie quelques lignes vers y≈1060 et
on garde celle qui révèle le plus de segments. Les positions sont donc
retrouvées à chaque combat, quelle que soit la composition de l'armée.

**Type de slot.** Troupes et sorts portent un compteur « xNN » en haut à
droite de la carte ; héros et machines de siège n'en ont pas. En mesurant la
proportion de pixels clairs à cet endroit on obtient 32-39 % pour les
premiers contre 0,3-5,4 % pour les seconds. Comme le deck est toujours ordonné
troupes → sièges → héros → sorts, les sorts sont les cartes à compteur situées
après le dernier slot sans compteur.

Ce classement se fait sur l'image, jamais sur le comportement. Une version
antérieure déduisait « ce slot ne bouge pas, donc c'est un sort » et
abandonnait des piles entières de troupes ; c'est ainsi qu'une attaque s'est
terminée avec 19 titans et 21 boulistes au dépôt. Un slot de troupes n'est
désormais jamais abandonné : s'il ne sort rien, on change de point de largage.

**Boutons superposés.** « Terminer la bataille » (x 138-357, y 772-841) et
« Suivant » sont posés par-dessus la carte : y larguer une troupe revient à
cliquer dessus et interrompt l'attaque. Pire, le sondage les validait à tort,
la boîte de confirmation modifiant l'écran comme l'aurait fait un largage
réussi. Ces zones sont donc exclues des points candidats et du tirage
aléatoire.

**Zone rouge.** La bordure rouge du jeu est trop fine et trop translucide pour
être suivie de façon fiable : sa couleur se confond avec les décorations, et
les filtres de forme la détruisent. On la contourne en la sondant. Un largage
refusé ne coûte rien — le jeu l'ignore, aucune troupe n'est perdue — donc on
tape un point, et si le compteur de la carte ne bouge pas, c'est que le point
est interdit. Chaque couloir est sondé de l'intérieur vers l'extérieur et le
premier point accepté est retenu, ce qui donne le point valide le plus proche
possible de la base.

## Améliorer les remparts

Après chaque attaque, le programme dépense le butin en remparts.

**Trouver un mur.** Le liseré doré des remparts est trop fin et se confond avec
les dorures des bâtiments — un détecteur basé dessus visait systématiquement
les défenses. Le *dessus* des murs, en revanche, est d'un crème clair
(240,224,192) qui forme de larges rubans que rien d'autre ne présente dans le
village. Le mur choisi est confirmé par OCR du titre : « Rempart (Niveau 17) ».

**Payer.** « Améliorer » ouvre une fenêtre de confirmation, reconnue à son
grand panneau de texte blanc (88 % de pixels blancs, contre moins de 5 %
partout ailleurs). Si l'or ne suffit pas, le programme reprend le mur et
retente en élixir, puis retient la ressource qui a marché pour les murs
suivants.

**Attention aux gemmes.** Quand une ressource manque, le jeu propose d'acheter
le complément contre des gemmes, avec un gros bouton vert au centre de l'écran.
Le programme ne clique jamais dans cette fenêtre : il en ressort au bouton
retour d'Android.

Le succès ne se juge donc ni sur la fermeture du menu, ni sur celle de la
fenêtre — après un achat réussi le jeu rouvre aussitôt la confirmation pour le
niveau suivant. Seule preuve fiable : **le compteur de ressources a baissé**
(écart de 0,00 sur 2,5 s sans dépense, 10 à 24 dès qu'une ressource bouge).

## Taper plutôt que maintenir

Contre-intuitif, mais mesuré dans un même combat, à durée égale (3,6 s) :

| Geste | Variation de la carte de troupes |
|---|---|
| appui maintenu 2500 ms | 6,0 |
| 60 taps rapides | **30,7** |

Maintenir le doigt ne fait sortir que deux ou trois unités : le jeu pose une
troupe **par tap**, il n'en enchaîne pas pendant l'appui. Les rafales de taps
sont donc environ cinq fois plus rapides, et c'est ce que fait le programme.

Le vrai multi-touch (tenir quatre points à la fois) serait encore plus rapide,
mais il est inaccessible ici : `/dev/input/event3` renvoie « Permission
denied » malgré l'appartenance au groupe `input`, car SELinux l'interdit au
shell ADB, et le téléphone n'est pas rooté. Deux `input swipe` concurrents ne
donnent pas deux doigts : ils partagent le pointeur 0.

## Ce qui coûte du temps

Mesuré sur Pixel 6 en USB :

| Opération | Coût |
|---|---|
| capture d'écran PNG (`screencap -p`) | 1,72 s |
| capture d'écran brute | **0,79 s** |
| capture brute + `gzip -1` sur le téléphone | 1,06 s (plus lent) |
| un `input tap` | 40 ms |
| aller-retour ADB à vide | 22 ms |

Les captures dominent tout le reste. D'où le format brut plutôt que le PNG,
une seule capture par passe de déploiement plutôt qu'une par slot, et un
sondage volontairement court (une poignée de couloirs) complété par géométrie.

Sur une attaque réelle : ~8 s de sondage, ~23 s de déploiement.

## Mise à jour à chaud

Python ne recharge pas un module déjà en mémoire : corriger le fichier pendant
qu'une série tourne ne change rien au processus en cours. Le programme
surveille donc la date de son propre source et se relance lui-même **entre
deux attaques**, jamais pendant un combat, en reportant le nombre d'attaques
restantes.

Concrètement : on modifie `coc_attack.py`, et la correction s'applique à
l'attaque suivante sans rien arrêter et sans gâcher celle en cours.

## Limites connues

- Les coordonnées sont calibrées pour un écran 2400x1080 en paysage. Une autre
  définition est mise à l'échelle automatiquement, mais des proportions très
  différentes demanderaient de refaire les templates.
- L'OCR du butin se trompe parfois d'un chiffre, voire ne lit pas une ligne.
  Dès qu'une des deux valeurs est illisible le village est attaqué plutôt
  qu'écarté : la valeur manquante pourrait être énorme, et passer un village à
  2 millions coûte bien plus cher qu'une attaque sur un village pauvre.
- Une attaque complète prend environ 2 à 3 minutes, dont ~30 s de sondage et
  de déploiement ; le reste, c'est le combat qui se déroule.
