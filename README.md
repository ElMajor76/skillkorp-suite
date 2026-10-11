# SkillKorp Suite

Pilote Linux non officiel, outil en ligne de commande, application graphique
(GTK 4 / Libadwaita) et indicateur de barre système pour le clavier
**SkillKorp K20 Ultimate** et la souris **SkillKorp M20 Ultimate**.

Les pilotes fonctionnent entièrement en espace utilisateur via `/dev/hidraw*` :
aucun module noyau n'est nécessaire, une règle udev donne l'accès à
l'utilisateur de la session.

> Version 1.2.0 : clavier pleinement fonctionnel via son récepteur 2.4 GHz,
> accès limité à l'interface de configuration (les frappes ne sont plus
> lisibles par la session), indicateur intégré à l'application. Protocoles
> vérifiés contre les logiciels officiels et sur le matériel — voir
> [AUDIT.md](AUDIT.md) et [CHANGELOG.md](CHANGELOG.md).

---

## 🔍 Matériel pris en charge

| Périphérique | Puce | USB IDs | Protocole |
|---|---|---|---|
| Clavier K20 Ultimate | Yichip YC3121 (carte CX71D) | `3151:4011` (récepteur 2.4 GHz) / `3151:4015` (câble) | Feature report 64 octets, Usage Page `0xFFFF`, octet de commande + checksum complément à un |
| Souris M20 Ultimate | Beken BK3633 + PixArt PAW3395 | `1d57:fa60` (récepteur 2.4 GHz) / `1d57:fa61` (câble USB-C) | Feature reports, Usage Page `0x000B` : `0x04` DPI/capteur, `0x05` éclairage/veille/anti-rebond, `0x06` fréquence, `0x08` boutons, `0x0C`→`0x03` état |

## 🚀 Installation

### Paquets prêts à l'emploi

| Distribution | Paquet | Commande |
|---|---|---|
| Debian 12+, Ubuntu 22.04+, Linux Mint, Pop!_OS | `skillkorp-suite_1.2.0-1_all.deb` | `sudo apt install ./skillkorp-suite_1.2.0-1_all.deb` |
| Fedora 39+, RHEL/Alma/Rocky 9+ | `skillkorp-suite-1.2.0-1.fc.noarch.rpm` | `sudo dnf install ./skillkorp-suite-1.2.0-1.fc.noarch.rpm` |
| openSUSE Tumbleweed / Leap 16 | `skillkorp-suite-1.2.0-1.suse.noarch.rpm` | `sudo zypper install --allow-unsigned-rpm ./skillkorp-suite-1.2.0-1.suse.noarch.rpm` |
| Arch, Manjaro, EndeavourOS | `skillkorp-suite-1.2.0-1-any.pkg.tar.zst` | `sudo pacman -U ./skillkorp-suite-1.2.0-1-any.pkg.tar.zst` |
| Arch (depuis les sources) | `PKGBUILD` + archive source | `makepkg -si` dans le dossier contenant les deux |

Après l'installation, **débranchez puis rebranchez** le récepteur (ou le
câble), puis vérifiez :

```bash
skillkorpctl doctor
```

### Script d'installation

```bash
./install.sh              # paquet natif de votre distribution, sinon installation dans /usr
./install.sh --system     # installation directe dans /usr
./install.sh --user       # installation dans ~/.local (la règle udev demande sudo)
./install.sh --uninstall  # désinstallation (vos profils sont conservés)
```

### Construire les paquets

```bash
python3 packaging/build_packages.py                     # tout ce que la machine sait construire
python3 packaging/build_packages.py --formats deb,rpm   # sélection
```

Les fichiers arrivent dans `dist/` avec un `SHA256SUMS`. Il faut `dpkg-deb`
pour le .deb, `rpmbuild` pour les .rpm et `bsdtar` + `zstd` pour le paquet
Arch (sinon seul le `PKGBUILD` est produit).

### Dépendances

Python ≥ 3.10, PyGObject, GTK 4 et libadwaita ≥ 1.2 pour l'interface. Le tray
utilise GTK 3 + (Ayatana)AppIndicator et libnotify (facultatifs). La bascule
automatique de profils détecte la fenêtre active avec `xdotool`/`xprop` (X11),
`hyprctl` (Hyprland), `swaymsg` (Sway) ou `kdotool` (KDE Wayland).

---

## 🛠️ Utilisation

### Ligne de commande : `skillkorpctl`

```bash
skillkorpctl status                     # vue d'ensemble clavier + souris
skillkorpctl doctor                     # diagnostic (règle udev, accès, outils)

# Clavier
skillkorpctl keyboard status
skillkorpctl keyboard battery                            # clavier réveillé
skillkorpctl keyboard rgb --mode wave --speed 4 --colorful
skillkorpctl keyboard rgb --mode static --color '#00AAFF' --single-color
skillkorpctl keyboard side-rgb --mode breathing --color '#FF0055'
skillkorpctl keyboard polling-rate 1000
skillkorpctl keyboard debounce 4
skillkorpctl keyboard sleep --light 300 --deep 1800      # secondes, 0 = jamais
skillkorpctl keyboard win-lock toggle
skillkorpctl keyboard remap --list
skillkorpctl keyboard remap F12 media_vol_up
skillkorpctl keyboard remap F12 default                  # action d'origine
skillkorpctl keyboard remap --fn F1 calculator

# Souris
skillkorpctl mouse status
skillkorpctl mouse battery
skillkorpctl mouse dpi --stages 400,800,1600,3200 --active 2
skillkorpctl mouse sensor --lod 1 --debounce 4 --motion-sync on
skillkorpctl mouse light --mode breathing --speed 6 --color '#FF4000'
skillkorpctl mouse power --led-off 5 --sleep-timer 10
skillkorpctl mouse button --btn 4 --action copy
skillkorpctl mouse monitor                                # bouton DPI et batterie en direct

# Profils (mêmes commandes pour keyboard et mouse)
skillkorpctl mouse profile list
skillkorpctl mouse profile set gaming_fps
skillkorpctl keyboard profile new streaming --copy-from gaming
skillkorpctl keyboard profile apps streaming --apps obs,discord
skillkorpctl keyboard profile export streaming --file ~/streaming.json
```

Codes de sortie : `0` succès, `1` valeur invalide, `2` périphérique absent ou
inaccessible, `3` commande refusée par le périphérique. Ajoutez `--json` pour
une sortie exploitable par des scripts.

### Application graphique : `skillkorp-gui`

- **Vue d'ensemble** : connexion, batterie, profil actif, DPI.
- **Clavier** : éclairage (multicolore ou couleur), bandes latérales,
  fréquence, anti-rebond, veille, options, remappage (couches normale et Fn).
- **Souris** : profils, DPI et capteur, boutons (schéma cliquable), fréquence
  et veille (la M20 Ultimate n'a pas d'éclairage RGB).

Les réglages modifiés dans la GUI sont enregistrés dans le profil actif.

### Indicateur : `skillkorp-tray`

Seul le logo SkillKorp apparaît dans la barre. Son menu affiche la batterie
du clavier et de la souris et permet de changer rapidement de profil,
d'éclairage, de DPI et de fréquence ; alertes batterie faible et bascule
automatique de profils selon l'application active.

Il démarre avec la session (entrée installée dans `/etc/xdg/autostart`) et au
lancement de `skillkorp-gui`. Son bouton **Quitter** ferme aussi la fenêtre de
l'application. Démarrage avec la session : case « Lancer avec la session » du
menu, ou `skillkorpctl autostart enable|disable`. `SKILLKORP_NO_TRAY=1`
empêche l'application de le lancer.

### Bascule automatique sans indicateur

```bash
systemctl --user enable --now skillkorp-daemon.service   # ou : skillkorpctl daemon
```

Chaque profil liste ses applications (`auto_switch_apps`). Quand l'une d'elles
passe au premier plan, son profil est appliqué ; le profil précédent revient
ensuite. N'activez pas le service et la bascule du tray en même temps.

Détection de l'application au premier plan : `xdotool` (X11), Hyprland,
Sway, `kdotool` (KDE) et, **sous GNOME Wayland**, l'extension GNOME Shell
`skillkorp-focus@skillkorp.github.io` installée avec le paquet. Elle ne
transmet que l'identifiant de l'application (jamais le titre ni le contenu
des fenêtres) et est activée automatiquement avec la bascule ; si le paquet
a été installé pendant la session, elle est chargée à la connexion
suivante. `skillkorpctl doctor` indique son état.

---

## 📁 Configuration

```
~/.config/skillkorp/keyboard/{default_profile.json, profile_state.json, profiles/*.json}
~/.config/skillkorp/mouse/{default_profile.json, profile_state.json, profiles/*.json}
```

Les anciens dossiers `~/.config/skillkorp-k20` et `~/.config/skillkorp-m20`
sont importés automatiquement au premier lancement (sans être supprimés).
Un fichier illisible est mis de côté sous `*.corrupt-<date>` puis remplacé par
les valeurs par défaut.

Variables de dépannage :

| Variable | Effet |
|---|---|
| `SKILLKORP_EXACT_REPORT_SIZE=0` | envoie les rapports de la souris avec les longueurs du logiciel Windows au lieu de celles du descripteur HID |

## 📡 Clavier en 2.4 GHz

Validé sur le matériel (capture USB du logiciel officiel, puis tests sous
Linux) : réglages et lectures passent par le récepteur comme par le câble.

- Le clavier doit être **réveillé** : s'il dort, la commande échoue avec
  « Le clavier est éteint ou en veille : appuyez sur une touche puis réessayez ».
- La batterie est lue sur le récepteur (`skillkorpctl keyboard battery`) ;
  en filaire le clavier est en charge et n'indique pas de pourcentage.
- Pour analyser le trafic USB d'un logiciel (par exemple le logiciel Windows
  dans une machine virtuelle), `tools/usbcapture.py` enregistre les échanges
  de configuration via `usbmon` sans jamais enregistrer les frappes.

## 🧪 Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

Les tests utilisent un faux périphérique HID (`tests/fakes.py`) qui enregistre
les trames : aucun matériel n'est nécessaire et aucun périphérique réel n'est
touché.

## 📄 Licence

MIT — voir [LICENSE](LICENSE). Projet non affilié à SkillKorp.
