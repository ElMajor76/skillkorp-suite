# Changelog — SkillKorp Suite

Toutes les modifications notables sont documentées dans ce fichier.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/).

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
