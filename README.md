# SkillKorp Suite

Application Linux unifiée pour le clavier gaming **SkillKorp K20 Ultimate** et
la souris gaming **SkillKorp M20 Ultimate**. Ce dépôt remplace les deux
projets historiques distincts [skillkorp-k20](https://github.com/ElMajor76/skillkorp-k20)
et [skillkorp-m20](https://github.com/ElMajor76/skillkorp-m20), qui maintenaient
en parallèle deux codebases quasi-identiques en structure (pilote + CLI + GUI
GTK4/Libadwaita + tray + packaging) mais divergentes en détail.

Les deux périphériques utilisent des puces, Usage Pages HID et protocoles de
trame totalement différents — cette fusion unifie uniquement la couche
applicative (CLI, GUI, profils, tray, packaging), jamais le protocole matériel
bas niveau de chaque pilote.

---

## 🔍 Matériel pris en charge

| Périphérique | Puce | USB IDs | Protocole |
|---|---|---|---|
| Clavier K20 Ultimate | Yichip YC3121 SoC (carte CX71D) | `3151:4011` (sans-fil) / `3151:4015` (filaire) | HID Feature Reports 64 octets, Usage Page `0xFFFF`, un octet de commande |
| Souris M20 Ultimate | XinMaiGao/Beken BK3633 + PixArt PAW3395 | `1d57:fa60` (sans-fil) / `1d57:fa61` (filaire) | HID Feature Reports, Usage Page `0x000B`, plusieurs Report IDs dédiés (`0x04` DPI, `0x05` veille, `0x06` polling, `0x08` boutons, `0x03`/`0x0C` batterie) |

---

## 🛠️ Outils disponibles

### 1. CLI unifiée : `skillkorpctl`

```bash
# Vue d'ensemble combinée (clavier + souris)
skillkorpctl status

# Clavier (toutes les anciennes commandes k20ctl, préfixées)
skillkorpctl keyboard status
skillkorpctl keyboard rgb --mode wave --speed 4
skillkorpctl keyboard profile list

# Souris (toutes les anciennes commandes m20ctl, préfixées)
skillkorpctl mouse status
skillkorpctl mouse dpi --stages 400,800,1600 --active 2
skillkorpctl mouse profile switch gaming_fps

# Bascule automatique de profil (clavier + souris) selon l'application active
skillkorpctl daemon

# Indicateur système unique
skillkorpctl tray start
skillkorpctl autostart enable
```

### 2. Application graphique : `skillkorp-gui`

Une seule fenêtre Adw.ApplicationWindow avec un sélecteur de section :
- **Vue d'ensemble** : état et batterie du clavier et de la souris côte à côte
- **Clavier** : Éclairage RGB, Performance, Options & remappage, Profils
- **Souris** : Profils, DPI et Capteur, Boutons (schéma interactif), Alimentation

### 3. Indicateur systray unique : `skillkorp-tray`

Un seul indicateur AppIndicator, avec des sous-menus « Clavier » et « Souris »,
et un label combiné du type `⌨82% 🖱91%`.

---

## 📦 Migration depuis skillkorp-k20 / skillkorp-m20

Au premier lancement (CLI, GUI ou tray), si `~/.config/skillkorp-k20/` et/ou
`~/.config/skillkorp-m20/` existent, leurs profils et configuration sont
importés automatiquement vers le nouvel emplacement unifié :

```
~/.config/skillkorp/keyboard/
~/.config/skillkorp/mouse/
```

Les anciens dossiers ne sont **pas** supprimés automatiquement — vous pouvez
les effacer vous-même une fois satisfait du fonctionnement de la suite.

---

## 🚀 Installation

```bash
./install.sh              # détection automatique de la distribution
./install.sh --system     # installation directe dans /usr
./install.sh --user       # installation dans ~/.local
./install.sh --uninstall  # désinstallation complète
```

Des paquets natifs (`.rpm`, `.deb`, `PKGBUILD` Arch) sont générés via
`packaging/build_packages.py`.

---

## 🧪 Tests

```bash
python3 -m pytest tests/ -v
```

## 📄 Licence

MIT — voir [LICENSE](LICENSE).
