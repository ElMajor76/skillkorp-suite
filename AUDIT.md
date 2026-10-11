# Audit de SkillKorp Suite 1.0.1 → 1.2.0

Audit complet du dépôt (pilotes, CLI, GUI, tray, profils, installation,
paquets) réalisé le 11/10/2026, suivi des corrections livrées en 1.1.0 et
1.2.0 (validation sur le matériel, récepteur 2.4 GHz du clavier, accès udev).

Les points de protocole ont été vérifiés contre les logiciels officiels fournis
(`ClavierK20Ultimate.exe` et `SourisM20Ultimate.exe`) par analyse statique
uniquement (rien n'a été exécuté) :

| Périphérique | Logiciel analysé | Méthode |
|---|---|---|
| Clavier K20 | « KU_SkillKorp Driver » 2.1.78 (Electron) + `iot_driver.exe` (Rust) | lecture du bundle JS `main_fd30ebc3.js` : le K20 est le modèle `yc3121_cx71d_soc` (classe `Obr` → `vZe` → `zR` → … → `Wj`), désassemblage ponctuel de `iot_driver.exe` |
| Souris M20 | `MU_SkillKorp.exe` (installeur Inno Setup 6.1) | désassemblage x86 des fonctions qui construisent chaque rapport, ressources `ms_1_page.xml` (bornes des curseurs) |

Le matériel réel (récepteurs `3151:4011` et `1d57:fa60`) a servi à valider la
détection et le dimensionnement des rapports (descripteurs HID lus dans sysfs).

---

## 1. Bugs de protocole — clavier K20

| # | Gravité | Problème (1.0.1) | Preuve dans le logiciel officiel | Correction |
|---|---|---|---|---|
| K1 | **Haute** | Le flag « dazzle » (multicolore) de l'éclairage était **inversé** : le pilote envoyait 8 quand une couleur était fournie, 7 sinon. Résultat : une couleur choisie donnait un effet arc-en-ciel, et la GUI (qui ne passait pas de couleur) renvoyait du rouge à chaque mouvement de curseur. | `zR.setLightSetting` : `y = r.dazzle ? e.DAZZLE : e.NORMAL` avec `DAZZLE=8`, `NORMAL=7` (champs de `zR`, qui surchargent ceux de `fR`). | Option explicite `colorful` (CLI `--colorful/--single-color`, interrupteur GUI) ; couleur et réglages non précisés conservés. |
| K2 | **Haute** | Le **remappage visait la mauvaise touche** : l'index envoyé était la position dans `KEY_LAYOUT` (Esc, F1, F2…), alors que le firmware attend l'index dans la matrice d'usine (colonnes de 6 rangées : Esc, ², Tab, Verr Maj, Maj, Ctrl, F1, 1, A…). Remapper F1 modifiait « ² ». | `Obr.setKeyConfigSimple` : `n = findIndexInDefaultMatrix(r.original, r.index)` sur `defaultMatrix` (`mbr`). | Matrices d'usine `DEFAULT_MATRIX`/`DEFAULT_FN_MATRIX` extraites du logiciel, index calculé avec le même algorithme ; 85/85 touches vérifiées. |
| K3 | Moyenne | Impossible d'annuler un remappage ; changer de profil laissait les remappages du profil précédent actifs. | Le logiciel officiel renvoie l'entrée de la matrice d'usine. | Action `default` (« Action d'origine ») ; `apply_profile` restaure les touches absentes du nouveau profil. |
| K4 | Moyenne | `set_keyboard_options` écrasait les octets 3 (matrice Fn) et 4 (économie d'énergie) et les bits non gérés (LED off, verrouillage clavier) en les mettant à 0. | `zR.setKeyboardOption` : `a[2]` bits, `a[3]=keyboardFnKeyMatrix`, `a[4]=powerSaveMode`. | Lecture préalable (`GET_KBOPTION` 0x86) puis lecture-modification-écriture. |
| K5 | Basse | Modes OS `ios`/`android` sur des bits non documentés. | `getKeyboardOption` ne connaît que `win`/`mac` (bit 1). | Limité à `win`/`mac` (anciens profils migrés). |
| K6 | Basse | Veille : bornes 10 s–1 h / 1 min–24 h, alors que le K20 accepte 1–60 min (légère) et 10–60 min (profonde), 0 = jamais. | `otherSetting` `Pi` du K20 : `sleep_24:{min:1,max:60,min_deep:10,max_deep:60}`. | Bornes corrigées, 0 accepté, profil « eco » ajusté. |
| K7 | Basse | L'anti-rebond envoyait l'index de profil en octet 1. | `zR.setDeBounce` : seul `a[2]` est rempli. | Octet 1 à 0. |
| K8 | Basse | Vitesse des bandes latérales envoyée 1–5 alors que l'octet n'est pas inversé (0–4). | `zR.setSLEDParam` : `a[2]=r.speed` (≠ `MAXSPEED-speed` du rétroéclairage). | Vitesse latérale = valeur − 1 ; mode « snake » (5) ajouté. |
| K9 | Basse | Pas de délai entre commandes alors que le firmware écrit sa flash (500 ms, 1 s pour la fréquence). | `COMMONDELAY=500`, `BIGCMDDELAY=1e3`. | Délai appliqué avant la commande suivante (sans ralentir une commande isolée). |
| K10 | Info | Batterie : l'ancien code acceptait n'importe quel octet 1–100 comme pourcentage. | Capture USB : le logiciel officiel lit la batterie dans l'état du récepteur (0xF7, octet 1) toutes les 2 s. | Pourcentage lu dans l'état du récepteur ; en filaire, « en charge ». |
| K12 | **Critique** | **Aucune commande ne passait par le récepteur 2.4 GHz.** Le préfixe `[0xFE, 64]` envoyé avant chaque trame, et les lectures sans récupération de la réponse, laissaient le récepteur bloqué : il ignore toute trame tant qu'une réponse du clavier n'a pas été lue. | Capture USB (usbmon) du logiciel officiel dans une VM Windows, voir ci-dessous. | Protocole du récepteur reproduit à l'identique ; une réponse restée en attente est récupérée avant chaque envoi. |
| K11 | Info | `#FFFFFF` : le logiciel officiel envoie `#FAFFFA`. | `16777215===c&&(c=16449530)`. | Reproduit. |

Points **vérifiés corrects** : codes de commande (0x04 fréquence, 0x06 options,
0x07/0x08 LED, 0x11 anti-rebond, 0x12 veille, 0x13/0x15 remappage, 0x02 reset),
checksums BIT7/BIT8 (`CheckSumType={BIT7:0,BIT8:1,NONE:2}`), codes de modes
d'éclairage, disposition de la trame de veille, codes de fréquence, encodage
des actions (`[0, modificateur, touche, 0]`, souris `[1,0,240..244,0]`,
multimédia `[3,0,usage_lo,usage_hi]`).

### Protocole du récepteur 2.4 GHz (K12)

Capturé avec `tools/usbcapture.py` pendant que le logiciel officiel pilotait
le récepteur `3151:4011` depuis une VM Windows, puis rejoué et validé sous
Linux (accusés `05 0F 00 00` du récepteur et changement visible des couleurs).
Le récepteur traite lui-même les commandes `0xF?`/`0xFC` ; les trames du
clavier (mêmes octets et checksums qu'en filaire) passent telles quelles.

| Étape | Trames (interface 2, feature report de 64 octets) |
|---|---|
| Branchement du récepteur | `F7`, `FE 40`, `F7`, `F6 0A` (une seule fois) |
| Écriture | `F7` (état), puis la trame ; ~1 s plus tard le récepteur émet le rapport d'entrée `05 0F 00 00` (deux fois) |
| Lecture | `F7`, requête (ex. `87`), `F7` jusqu'à ce que l'octet 0 vaille 1, `FC`, puis GET_FEATURE = réponse du clavier |
| Sans activité | `F7` toutes les 2 s |

État `F7` : `[réponse prête, batterie %, 0, clavier hors ligne, 1, 1, 1, …]`
(les octets suivants sont des restes de la trame précédente). Une réponse non
récupérée par `FC` bloque le récepteur : il ignore alors toutes les trames
suivantes, même après débranchement du récepteur ou redémarrage du clavier,
jusqu'à ce que la réponse soit lue. Le pilote la récupère avant chaque envoi.

Le récepteur ne garde **qu'une trame** à destination du clavier : l'octet 5
de l'état `F7` vaut 0 tant qu'elle n'a pas été transmise (60 à 150 ms quand
le clavier est éveillé), puis 1. Une trame envoyée avant remplace la
précédente, qui est perdue sans erreur (mis en évidence par
`tools/hwtest.py`). Le pilote attend donc cette transmission après chaque
trame, puis relit le réglage (la relecture le reflète ~1,2 s après l'envoi)
et le renvoie jusqu'à 3 fois s'il ne correspond pas.
Les remappages (0x13/0x15) ne sont pas encore vérifiés ainsi : la
disposition de leur relecture n'est pas connue.

## 2. Bugs de protocole — souris M20

| # | Gravité | Problème (1.0.1) | Preuve (`MU_SkillKorp.exe`) | Correction |
|---|---|---|---|---|
| M1 | **Haute** | Rapport 0x05 mal construit : l'octet 10 recevait « réveil au mouvement » (0/1) alors que c'est **l'anti-rebond** → l'anti-rebond des boutons était réglé à 0 ou 1 ms à chaque changement d'éclairage. | Fonction à `0x413a43` : `[ebp-0x5e] = cfg+0x958`, et `cfg+0x958` est écrit par le curseur `ms_1_slider_key_debounce` (`0x4112e2`). | Trame refaite à l'identique (voir ci-dessous). |
| M2 | **Haute** | L'anti-rebond n'était **jamais envoyé** (stocké seulement). | Absent du rapport 0x04 (`0x413c80`), présent dans le 0x05. | Envoyé dans le 0x05. |
| M3 | **Haute** | Modes d'éclairage décalés d'un cran (1–12 au lieu de 0–11) : « Fixe » envoyait « Respiration », « Éteint » envoyait « Fixe »… | Mode = index du combo (`ms_1_lightmode1..12`) ; la luminosité n'est envoyée que pour les modes 1 et 9, soit « Fixe » et « Couleur mixte fixe » en numérotation 0. | Codes 0–11. |
| M4 | Moyenne | Vitesse, luminosité et minuterie mal placées : octet 4 = `luminosité<<4 | vitesse`, octet 5 = 0, octet 9 = minutes de veille. | Octet 4 = `(veille & 0xF0) | (9 − vitesse)`, octet 5 = `(veille << 4) | (luminosité si mode fixe, sinon 8)`, octet 9 = **extinction des LED** (`ms_1_slider_light_off`). | Encodage officiel ; nouveau réglage « extinction des LED ». |
| M5 | Moyenne | « Réveil au mouvement » proposé alors que l'option n'existe pas sur la M20. | `ms_1_check_movemode … visible="false"` dans `ms_1_page.xml`. | Option retirée (ignorée avec avertissement). |
| M6 | Basse | Bornes : vitesse 1–8, veille 1–60, pas de borne d'anti-rebond. | `ms_1_page.xml` : luminosité 1–8, vitesse 4–8, veille 1–60, LED 1–60, anti-rebond 2–25. | Bornes officielles. |
| M7 | Moyenne | Trames envoyées plus longues que déclaré (56 octets pour le rapport 0x04 qui en déclare 52, 15 pour le 0x05 qui en déclare 13, 10 pour le 0x0C qui en déclare 6). | Descripteur HID de la souris (lu sur le matériel). La pile HID Windows envoie la taille déclarée. | Taille alignée sur le descripteur (seuls des zéros de bourrage sont retirés). `SKILLKORP_EXACT_REPORT_SIZE=0` rétablit l'ancien comportement. |
| M8 | Moyenne | Batterie : requête 0x0C envoyée sur un descripteur puis lecture sur un **autre** descripteur ouvert après coup → réponse perdue ; 100 % affiché par défaut. | — | Requête et lecture sur le même descripteur ; « inconnu » tant que la souris n'a rien envoyé. |
| M9 | Basse | `apply_profile` envoyait deux fois le rapport 0x05 et renvoyait toujours `True`. | — | Une transaction (0x06, 0x04, 0x05, 0x08), résultat réel. |

Vérifiés corrects : rapport 0x04 (LOD, ripple, masque, angle, motion sync,
DPI/50−1 sur deux tableaux, étape active, couleurs, checksum 16 bits octets
3–49), rapport 0x06, requête 0x0C (`0c 0a 01 fe 01 fe`), longueurs 9/15/56/59.

## 3. Fiabilité des pilotes (transport et état)

* **Mauvais périphérique après renumérotation** : un chemin `/dev/hidrawN`
  mémorisé pouvait désigner un autre appareil après débranchement. Chaque
  ouverture vérifie désormais `st_rdev` contre sysfs et relance la détection.
* **Accès concurrents** : GUI, tray et démon pouvaient entrelacer des
  transactions (préfixe sans-fil + trame). Verrou exclusif `flock` sur le nœud
  hidraw pendant chaque transaction.
* **Tri lexical** des nœuds (`hidraw10` avant `hidraw2`) ; détection par
  « `4011` in uevent » fragile → analyse de `HID_ID` et du descripteur.
* **Erreurs** : ioctl réessayé sur erreurs transitoires (EPIPE, EIO, ETIMEDOUT…),
  exceptions typées (absent / permission / occupé / refusé) au lieu de `False`
  silencieux ou de traces Python ; codes de sortie CLI documentés.
* **Configuration** : écriture non atomique (clavier), fichier `.tmp` commun à
  tous les processus (souris), mises à jour perdues entre processus, fichier
  corrompu silencieusement remplacé par les défauts. Désormais : écriture
  atomique avec `fsync`, lecture-modification-écriture sous verrou, fichier
  corrompu mis de côté (`*.corrupt-<date>`), valeurs invalides réparées.
* La configuration n'est mise à jour qu'après acceptation de la trame.

## 4. Logiciel (CLI, GUI, tray, profils)

* **GUI figée** : toutes les E/S (dont une requête batterie toutes les 2 s)
  tournaient dans la boucle GTK. Désormais un thread par périphérique,
  curseurs anti-rafale (une seule trame après relâchement), messages d'erreur,
  bandeau d'aide si la règle udev manque.
* GUI : les titres contenant `&` (profil « Nuit & Économie d'énergie », touche
  « & 1 ») ne s'affichaient pas (interprétés comme balisage Pango).
* GUI : réglages clavier non enregistrés dans le profil actif (perdus au
  changement de profil) ; pas de réglage d'éclairage pour la souris ; import
  souris par chemin tapé à la main ; compatibilité libadwaita 1.2 (Debian 12)
  à 1.9.
* Tray : mêmes problèmes de blocage, « 0 % » affiché quand la batterie est
  inconnue, plusieurs instances possibles, fichier PID non nettoyé.
* CLI : valeurs de retour ignorées (« ✓ » affiché après un échec), trace
  Python sur une saisie invalide (`--stages 400,abc`), `profile new` sans
  identifiant qui plantait, pas de commande d'éclairage souris. Ajouts :
  `mouse light`, `keyboard info`, `keyboard apply`, `doctor`, `--version`.
* Bascule automatique : la plus longue correspondance gagne, motifs vides
  ignorés, **retour au profil précédent** quand l'application n'est plus au
  premier plan, détection KDE (kdotool) en plus.
* Profils validés à l'import, à l'enregistrement et avant application ;
  anciens profils (1.0.x) migrés ; profil intégré corrompu régénéré.
* Raccourcis « Annuler (Ctrl+Z) » et « Tout sélectionner (Ctrl+A) » envoyaient
  les codes QWERTY, soit **Ctrl+W (fermer l'onglet)** et **Ctrl+Q (quitter)**
  sur un système AZERTY. Corrigés (variantes `_qwerty` disponibles).

## 5. Installation et paquets

* Règle udev nommée `99-…` : le tag `uaccess` doit être posé **avant**
  `73-seat-late.rules`, sinon l'accès sans root n'est pas accordé de façon
  fiable. Renommée `70-skillkorp-suite.rules` (l'ancienne est supprimée).
* **Sécurité (1.2.0)** : la règle donnait l'accès à *toutes* les interfaces
  HID des appareils, dont l'interface clavier (frappes) et la souris :
  n'importe quel programme de la session pouvait lire ce qui était tapé sur
  le K20 (vérifié pendant la capture USB). Le pilote n'utilise que
  l'interface 2 (configuration) ; la règle ne cible plus qu'elle, via le
  `modalias` de l'interface USB parente (`usb:v3151p4011*in02`…). Les ACL
  déjà posées sur les autres interfaces sont retirées par les scripts
  d'installation, et `skillkorpctl doctor` signale une interface de saisie
  encore exposée. Interface 2 vérifiée sur le matériel pour les quatre
  modes : récepteurs 2.4 GHz et câbles USB du clavier comme de la souris.
* Lanceurs `PYTHONPATH="/usr/share/…:$PYTHONPATH"` : avec un `PYTHONPATH` vide,
  le `:` final ajoutait le répertoire courant au chemin d'import. Les lanceurs
  utilisent maintenant `python3 -I` (mode isolé).
* `cp -r` dans un dossier existant créait `skillkorp/skillkorp` à la
  réinstallation ; icône 512×512 déclarée 256×256 ; dépendances manquantes
  (GTK 3 pour le tray) ou inutiles ; aucun RPM openSUSE ; PKGBUILD sans somme
  de contrôle.
* Nouveau : paquets .deb, .rpm (Fedora/RHEL et openSUSE), Arch (.pkg.tar.zst +
  PKGBUILD), métadonnées AppStream, service utilisateur systemd
  `skillkorp-daemon.service`, construction reproductible.

## 6. Limites connues

* Validé sur le matériel : clavier en filaire et en 2.4 GHz (lectures,
  éclairage confirmé visuellement et par relecture/accusés), souris par
  récepteur (batterie, accusés `03 10 50 00 <rapport>` de chaque écriture).
  Les autres réglages utilisent les mêmes trames que le code officiel mais
  n'ont pas tous été essayés un par un. Vérification après installation :
  `skillkorpctl doctor`.
* La M20 Ultimate n'a pas d'éclairage RGB : la GUI ne propose plus ces
  réglages (le rapport 0x05 reste envoyé pour la veille et l'anti-rebond).
* Rapport 0x08 (boutons de la souris) : disposition reprise de la version
  précédente, non revérifiée instruction par instruction.
* Clavier en 2.4 GHz : les commandes échouent (message explicite) tant que
  le clavier dort ; il suffit d'appuyer sur une touche.
