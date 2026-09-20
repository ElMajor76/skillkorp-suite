# Changelog — SkillKorp Suite

Toutes les modifications notables sont documentées dans ce fichier.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/).

## [1.0.1] — 2026-09-20

### Correction du protocole clavier K20 (RGB, verrouillage, remapping...)

- `skillkorp/devices/keyboard.py` : le protocole HID du K20 (SoC Yichip
  YC3121) tel que porté depuis `k20_driver.py` n'avait en réalité **jamais
  été validé sur du vrai matériel** — les codes de commande et les offsets de
  payload étaient faux, et il manquait un checksum complément à un obligatoire
  (le firmware ignore silencieusement toute trame sans checksum valide).
  Corrigé après reverse-engineering du logiciel officiel :
  - Checksum BIT7 (`payload[7]`) pour les commandes standard (debounce,
    polling rate, options, veille, remap de touches, requêtes) et checksum
    BIT8 (`payload[8]`) pour les commandes d'éclairage (RGB principal et
    bandes latérales).
  - Codes de commande corrigés : `CMD_SET_LEDPARAM` (0x04→0x07),
    `CMD_SET_REPORT` (0x01→0x04), `CMD_SET_PROFILE` (0x02→0x05),
    `CMD_SET_RESERT` (0x0A→0x02), et leurs équivalents `GET`.
  - Index des modes d'éclairage principal/latéral corrigés.
  - Décalage d'un octet des trames `set_sleep_time`/`remap_key`/
    `remap_fn_key` pour laisser la place au checksum.
  - `tests/test_keyboard_driver.py` mis à jour (81 tests, tous verts).
- Le pilote souris (`skillkorp/devices/mouse.py`) n'est pas concerné par ce
  correctif.

## [1.0.0] — 2026-09-20

### Fusion initiale de skillkorp-k20 et skillkorp-m20

- **Pilotes matériels portés sans régression** : `skillkorp/devices/keyboard.py`
  (port de `k20_driver.py`, audité précédemment — offsets corrigés pour
  `set_sleep_time`/`remap_key`/`remap_fn_key`, détection d'interface HID à 5
  octets, lecture de batterie honnête avec fallback `None`) et
  `skillkorp/devices/mouse.py` (port de `m20_driver.py`, revérifié pendant ce
  portage contre `skillkorp-m20/README.md` : le checksum du Report 0x05 et le
  parsing de la trame batterie `0x03/0x10/0x40` correspondent au protocole
  documenté, aucune divergence trouvée).
- **Interface commune `SkillkorpDeviceDriver`** (`skillkorp/devices/base.py`) :
  `is_connected`, `is_wireless`, `get_battery`, `apply_profile`,
  `reload_config_if_changed`, `save_config`. Deux méthodes additives ont été
  introduites sur le pilote souris pour satisfaire cette interface commune —
  `is_wireless()` et `apply_profile()` — sans changer le comportement matériel
  existant.
- **ProfileManager unifié** (`skillkorp/profiles/manager.py`) paramétré par
  `device_type` ("keyboard"|"mouse"), stockage sous
  `~/.config/skillkorp/{keyboard,mouse}/`.
- **Migration automatique** (`skillkorp/profiles/migration.py`) des anciens
  dossiers `~/.config/skillkorp-k20` et `~/.config/skillkorp-m20` vers le
  nouvel emplacement unifié, au premier lancement, sans suppression des
  anciens dossiers.
- **CLI unifiée `skillkorpctl`** : sous-commandes préfixées par appareil
  (`skillkorpctl keyboard ...`, `skillkorpctl mouse ...`), plus
  `skillkorpctl status` (vue combinée), `skillkorpctl daemon` (bascule
  automatique des profils clavier ET souris selon l'application active) et
  `skillkorpctl tray`/`autostart` unifiés.
- **GUI unique `skillkorp-gui`** (GTK4/Libadwaita) : section "Vue d'ensemble"
  + section "Clavier" (4 onglets K20) + section "Souris" (4 onglets M20).
- **Tray unique `skillkorp-tray`** : un seul indicateur AppIndicator pour les
  deux périphériques, label combiné `⌨XX% 🖱YY%`.
- **Packaging unique** : `.rpm`, `.deb`, `PKGBUILD` Arch et `install.sh`
  installent l'ensemble (remplace les paquets séparés `skillkorp-k20` et
  `skillkorp-m20`).
- **CI GitHub Actions unique** testant les deux pilotes (lint flake8 +
  pytest + smoke tests d'import).
