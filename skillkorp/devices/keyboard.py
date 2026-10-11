#!/usr/bin/env python3
"""
SkillKorp K20 Ultimate - Linux Hardware Driver & Controller

Protocol reference: the official "KU_SkillKorp Driver" 2.1.78 (Electron UI +
iot_driver.exe). The K20 is device "yc3121_cx71d_soc" (VID 0x3151, PID 0x4011
for the 2.4GHz receiver, 0x4015 wired), handled by the vendor's YC3121 class
hierarchy. Every frame below was checked against that code; see AUDIT.md.

Transport: interface 2 (Usage Page 0xFFFF, Usage 0x0002), a single 64-byte
feature report without report ID. byte[0] is the command, then:
  - "BIT7" checksum: payload[7] = ~sum(payload[0:7]) (most commands),
  - "BIT8" checksum: payload[8] = ~sum(payload[0:8]) (LED commands).
On the 2.4GHz receiver frames are sent unchanged; the receiver has its own
commands (0xF7 status, 0xFC fetch answer, 0xFE/0xF6 init) used around them,
as captured from the vendor software (see SkillkorpK20Driver._rf_prepare).

Features: RGB backlight & side LEDs (incl. multicolour "dazzle" flag), polling
rate, debounce, light/deep sleep, Win-lock / WASD swap / gaming mode / OS mode
(read-modify-write, unknown option bits preserved), hardware profile, single
key remapping on the base and Fn layers (correct matrix indices, restorable
to factory), factory reset, firmware version and battery query.
"""

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import SkillkorpDeviceDriver
from . import hidraw
from .hidraw import DeviceBusyError, DeviceError, DeviceNotFoundError, DevicePermissionError, HidrawNode



class KeyboardOfflineError(DeviceError):
    """The 2.4GHz receiver reports the keyboard off or asleep."""


CONFIG_DIR = os.path.expanduser("~/.config/skillkorp/keyboard")
DEFAULT_PROFILE_FILE = os.path.join(CONFIG_DIR, "default_profile.json")

# USB IDs
VENDOR_ID = 0x3151
PID_WIRELESS = 0x4011  # "KU SKK 2,4GHz" receiver
PID_WIRED = 0x4015     # "KU SkillKorp" wired
PAYLOAD_LEN = 64

# Polling Rate mappings: rate in Hz -> hardware code
POLLING_RATE_MAP = {
    1000: 1,
    500: 2,
    250: 4,
    125: 8,
}
CODE_TO_POLLING_RATE = {1: 1000, 2: 500, 4: 250, 8: 125}

# RGB lighting effects (FEA_CMD_SET_LEDPARAM = 0x07). Codes from the vendor's
# setLightSetting(): LightOff=0, LightAlwaysOn=1, LightBreath=2, LightWave=4,
# LightRipple=5, LightRaindrop=6, LightSnake=7, LightPressAction=8,
# LightConverage=9, LightUserPicture=13.
LIGHT_MODES = [
    ("off", "Éteint (Off)", 0),
    ("static", "Fixe (Always On)", 1),
    ("breathing", "Respiration (Breathing)", 2),
    ("wave", "Onde (Wave)", 4),
    ("ripple", "Ondulation (Ripple)", 5),
    ("raindrop", "Gouttes de pluie (Raindrop)", 6),
    ("snake", "Serpent (Snake)", 7),
    ("press_action", "Touche active (Press Action)", 8),
    ("convergence", "Convergence", 9),
    ("custom", "Personnalisé (Custom)", 13),
]
LIGHT_MODE_BY_CODE = {code: key for key, _, code in LIGHT_MODES}
LIGHT_CODE_BY_KEY = {key: code for key, _, code in LIGHT_MODES}

# Side LED effects (FEA_CMD_SET_SLEDPARAM = 0x08): LightOff=0, AlwaysOn=1,
# Breath=2, Neon=3 (colour cycling, shown as "rainbow"), Wave=4, Snake=5.
SIDE_LIGHT_MODES = [
    ("off", "Éteint (Off)", 0),
    ("static", "Fixe (Static)", 1),
    ("breathing", "Respiration (Breathing)", 2),
    ("wave", "Onde (Wave)", 4),
    ("rainbow", "Arc-en-ciel (Rainbow)", 3),
    ("snake", "Serpent (Snake)", 5),
]
SIDE_LIGHT_MODE_BY_CODE = {code: key for key, _, code in SIDE_LIGHT_MODES}
SIDE_LIGHT_CODE_BY_KEY = {key: code for key, _, code in SIDE_LIGHT_MODES}

# Wave direction option (vendor WAVEOP): stored in the high nibble of byte 4.
WAVE_DIRECTIONS = {"right": 0, "left": 1, "down": 2, "up": 3}

# Low nibble of the LED option byte: DAZZLE = multicolour, NORMAL = the RGB
# colour given in bytes 5..7 (vendor class constants DAZZLE=8, NORMAL=7).
LED_DAZZLE = 8
LED_NORMAL = 7
LED_MAX_SPEED = 4      # hardware speed byte = LED_MAX_SPEED - (speed - 1)
WHITE_SUBSTITUTE = (0xFA, 0xFF, 0xFA)  # the vendor never sends pure #FFFFFF

# Protocol command codes (YC3121 "rongyuan" keyboard class)
CMD_SET_REV = 0x00
CMD_GET_REV = 0x80         # firmware version
CMD_SET_RESERT = 0x02      # factory reset
CMD_SET_REPORT = 0x04      # polling rate
CMD_GET_REPORT = 0x84
CMD_SET_PROFILE = 0x05     # profile index
CMD_GET_PROFILE = 0x85
CMD_SET_KBOPTION = 0x06    # Win lock, OS mode, WASD swap, LED off, gaming...
CMD_GET_KBOPTION = 0x86
CMD_SET_LEDPARAM = 0x07    # backlight
CMD_GET_LEDPARAM = 0x87
CMD_SET_SLEDPARAM = 0x08   # side LEDs
CMD_GET_SLEDPARAM = 0x88
CMD_SET_KEYMATRIX = 0x09
CMD_GET_KEYMATRIX = 0x89
CMD_SET_DEBOUNCE = 0x11
CMD_GET_DEBOUNCE = 0x91
CMD_SET_SLEEPTIME = 0x12
CMD_GET_SLEEPTIME = 0x92
CMD_SET_KEYMATRIX_SIMPLE = 0x13  # single key remapping
CMD_GET_KEYMATRIX_SIMPLE = 0x93
CMD_SET_FN_SIMPLE = 0x15         # single Fn-layer remapping
CMD_GET_FN_SIMPLE = 0x95
CMD_GET_BATTERY = 0x83     # 2.4GHz battery query (iot_driver CMD list)
# 2.4GHz receiver commands (handled by the receiver itself, see _rf_status()).
RF_SET_NEXT_PACK_LENGTH = 0xFE  # [0xFE, 64]: sent once when the receiver appears
RF_SET_PARAM = 0xF6             # [0xF6, 0x0A]: sent once, right after
RF_PARAM_VALUE = 0x0A
RF_GET_STATUS = 0xF7            # status, polled before every frame
RF_FETCH_REPLY = 0xFC           # move the keyboard's answer into the GET buffer
RF_STATUS_REPLY_READY = 0       # 1 = an answer from the keyboard is waiting
RF_STATUS_BATTERY = 1           # battery percentage
RF_STATUS_OFFLINE = 3           # non-zero while the keyboard is off or asleep
RF_REPLY_TIMEOUT = 1.5          # max wait for the keyboard's answer over the air

# Delays observed in the vendor code after each command (COMMONDELAY /
# BIGCMDDELAY): the firmware writes its flash and drops frames meanwhile.
DELAY_COMMON = 0.5
DELAY_BIG = 1.0
DELAY_KEY = 0.05

# Limits declared for this model (vendor otherSetting "Pi"): light sleep
# 1..60 min, deep sleep 10..60 min, 0 = never.
LIGHT_SLEEP_RANGE = (60, 3600)
DEEP_SLEEP_RANGE = (600, 3600)
DEBOUNCE_RANGE = (2, 20)
PROFILE_COUNT = 3

# Default 75% AZERTY keyboard layout (85 entries: 84 keys + the rotary knob).
# Matches OE.layout from the official software. "code" is the key's factory
# action as found in DEFAULT_MATRIX; x/y/w/h are only used for drawing.
KEY_LAYOUT = [
    # Row 1
    {"name": "Esc", "code": 41, "x": 99, "y": -16, "w": 29, "h": 34},
    {"name": "F1", "code": 58, "x": 142, "y": -16, "w": 29, "h": 34},
    {"name": "F2", "code": 59, "x": 185, "y": -16, "w": 29, "h": 34},
    {"name": "F3", "code": 60, "x": 228, "y": -16, "w": 29, "h": 34},
    {"name": "F4", "code": 61, "x": 271, "y": -16, "w": 29, "h": 34},
    {"name": "F5", "code": 62, "x": 314, "y": -16, "w": 29, "h": 34},
    {"name": "F6", "code": 63, "x": 357, "y": -16, "w": 29, "h": 34},
    {"name": "F7", "code": 64, "x": 399, "y": -16, "w": 29, "h": 34},
    {"name": "F8", "code": 65, "x": 442, "y": -16, "w": 29, "h": 34},
    {"name": "F9", "code": 66, "x": 485, "y": -16, "w": 29, "h": 34},
    {"name": "F10", "code": 67, "x": 528, "y": -16, "w": 29, "h": 34},
    {"name": "F11", "code": 68, "x": 571, "y": -16, "w": 29, "h": 34},
    {"name": "F12", "code": 69, "x": 614, "y": -16, "w": 29, "h": 34},
    {"name": "Recherche", "code": 50340098, "x": 657, "y": -16, "w": 29, "h": 34},
    {"name": "Molette", "code": 4294967295, "x": 700, "y": -16, "w": 29, "h": 34},
    {"name": "Suppr", "code": 76, "x": 743, "y": -16, "w": 29, "h": 34},

    # Row 2
    {"name": "²", "code": 53, "x": 99, "y": -59, "w": 29, "h": 34},
    {"name": "& 1", "code": 30, "x": 142, "y": -59, "w": 29, "h": 34},
    {"name": "é 2", "code": 31, "x": 185, "y": -59, "w": 29, "h": 34},
    {"name": "\" 3", "code": 32, "x": 228, "y": -59, "w": 29, "h": 34},
    {"name": "' 4", "code": 33, "x": 271, "y": -59, "w": 29, "h": 34},
    {"name": "( 5", "code": 34, "x": 314, "y": -59, "w": 29, "h": 34},
    {"name": "- 6", "code": 35, "x": 357, "y": -59, "w": 29, "h": 34},
    {"name": "è 7", "code": 36, "x": 399, "y": -59, "w": 29, "h": 34},
    {"name": "_ 8", "code": 37, "x": 442, "y": -59, "w": 29, "h": 34},
    {"name": "ç 9", "code": 38, "x": 485, "y": -59, "w": 29, "h": 34},
    {"name": "à 0", "code": 39, "x": 528, "y": -59, "w": 29, "h": 34},
    {"name": ") °", "code": 45, "x": 571, "y": -59, "w": 29, "h": 34},
    {"name": "= +", "code": 46, "x": 614, "y": -59, "w": 29, "h": 34},
    {"name": "Retour", "code": 42, "x": 657, "y": -59, "w": 72, "h": 34},
    {"name": "Origine", "code": 74, "x": 743, "y": -59, "w": 29, "h": 34},

    # Row 3
    {"name": "Tab", "code": 43, "x": 99, "y": -102, "w": 50, "h": 34},
    {"name": "A", "code": 20, "x": 163, "y": -102, "w": 29, "h": 34},
    {"name": "Z", "code": 26, "x": 206, "y": -102, "w": 29, "h": 34},
    {"name": "E", "code": 8, "x": 249, "y": -102, "w": 29, "h": 34},
    {"name": "R", "code": 21, "x": 292, "y": -102, "w": 29, "h": 34},
    {"name": "T", "code": 23, "x": 335, "y": -102, "w": 29, "h": 34},
    {"name": "Y", "code": 28, "x": 378, "y": -102, "w": 29, "h": 34},
    {"name": "U", "code": 24, "x": 420, "y": -102, "w": 29, "h": 34},
    {"name": "I", "code": 12, "x": 463, "y": -102, "w": 29, "h": 34},
    {"name": "O", "code": 18, "x": 506, "y": -102, "w": 29, "h": 34},
    {"name": "P", "code": 19, "x": 549, "y": -102, "w": 29, "h": 34},
    {"name": "^ ¨", "code": 47, "x": 592, "y": -102, "w": 29, "h": 34},
    {"name": "$ £", "code": 48, "x": 635, "y": -102, "w": 29, "h": 34},
    {"name": "Entrée", "code": 40, "x": 688, "y": -102, "w": 40, "h": 77},
    {"name": "Fin", "code": 77, "x": 743, "y": -102, "w": 29, "h": 34},

    # Row 4
    {"name": "Verr Maj", "code": 57, "x": 99, "y": -145, "w": 61, "h": 34},
    {"name": "Q", "code": 4, "x": 174, "y": -145, "w": 29, "h": 34},
    {"name": "S", "code": 22, "x": 217, "y": -145, "w": 29, "h": 34},
    {"name": "D", "code": 7, "x": 260, "y": -145, "w": 29, "h": 34},
    {"name": "F", "code": 9, "x": 303, "y": -145, "w": 29, "h": 34},
    {"name": "G", "code": 10, "x": 346, "y": -145, "w": 29, "h": 34},
    {"name": "H", "code": 11, "x": 389, "y": -145, "w": 29, "h": 34},
    {"name": "J", "code": 13, "x": 431, "y": -145, "w": 29, "h": 34},
    {"name": "K", "code": 14, "x": 474, "y": -145, "w": 29, "h": 34},
    {"name": "L", "code": 15, "x": 517, "y": -145, "w": 29, "h": 34},
    {"name": "M", "code": 51, "x": 560, "y": -145, "w": 29, "h": 34},
    {"name": "% ù", "code": 52, "x": 603, "y": -145, "w": 29, "h": 34},
    {"name": "* µ", "code": 49, "x": 646, "y": -145, "w": 29, "h": 34},
    {"name": "Page H.", "code": 75, "x": 743, "y": -145, "w": 29, "h": 34},

    # Row 5
    {"name": "Maj G.", "code": 225, "x": 99, "y": -188, "w": 41, "h": 34},
    {"name": "< >", "code": 100, "x": 153, "y": -188, "w": 29, "h": 34},
    {"name": "W", "code": 29, "x": 196, "y": -188, "w": 29, "h": 34},
    {"name": "X", "code": 27, "x": 239, "y": -188, "w": 29, "h": 34},
    {"name": "C", "code": 6, "x": 282, "y": -188, "w": 29, "h": 34},
    {"name": "V", "code": 25, "x": 325, "y": -188, "w": 29, "h": 34},
    {"name": "B", "code": 5, "x": 368, "y": -188, "w": 29, "h": 34},
    {"name": "N", "code": 17, "x": 410, "y": -188, "w": 29, "h": 34},
    {"name": "? ,", "code": 16, "x": 453, "y": -188, "w": 29, "h": 34},
    {"name": ". ;", "code": 54, "x": 496, "y": -188, "w": 29, "h": 34},
    {"name": "/ :", "code": 55, "x": 539, "y": -188, "w": 29, "h": 34},
    {"name": "§ !", "code": 56, "x": 582, "y": -188, "w": 29, "h": 34},
    {"name": "Maj D.", "code": 229, "x": 625, "y": -188, "w": 61, "h": 34},
    {"name": "▲", "code": 82, "x": 699, "y": -188, "w": 29, "h": 34},
    {"name": "Page B.", "code": 78, "x": 743, "y": -188, "w": 29, "h": 34},

    # Row 6
    {"name": "Ctrl G.", "code": 224, "x": 99, "y": -231, "w": 47, "h": 34},
    {"name": "Fn", "code": 167837696, "x": 160, "y": -231, "w": 29, "h": 34},
    {"name": "Win", "code": 227, "x": 203, "y": -231, "w": 29, "h": 34},
    {"name": "Alt", "code": 226, "x": 246, "y": -231, "w": 29, "h": 34},
    {"name": "Espace", "code": 44, "x": 289, "y": -231, "w": 250, "h": 34},
    {"name": "Alt Gr", "code": 230, "x": 554, "y": -231, "w": 29, "h": 34},
    {"name": "Menu / Win", "code": 231, "x": 597, "y": -231, "w": 46, "h": 34},
    {"name": "◀", "code": 80, "x": 657, "y": -231, "w": 29, "h": 34},
    {"name": "▼", "code": 81, "x": 699, "y": -231, "w": 29, "h": 34},
    {"name": "▶", "code": 79, "x": 743, "y": -231, "w": 29, "h": 34},
]

# Action mapping dictionaries for key remapping
REMAP_ACTIONS: Dict[str, Tuple[str, List[int]]] = {
    # Multimedia
    "media_play_pause": ("Lecture / Pause", [3, 0, 205, 0]),
    "media_prev": ("Piste Précédente", [3, 0, 182, 0]),
    "media_next": ("Piste Suivante", [3, 0, 181, 0]),
    "media_stop": ("Arrêt Média", [3, 0, 183, 0]),
    "media_mute": ("Muet", [3, 0, 226, 0]),
    "media_vol_up": ("Volume +", [3, 0, 233, 0]),
    "media_vol_down": ("Volume -", [3, 0, 234, 0]),
    "calculator": ("Calculatrice", [3, 0, 146, 1]),
    "email": ("Email / Messagerie", [3, 0, 138, 1]),
    "browser_home": ("Page d'accueil Web", [3, 0, 35, 2]),
    "browser_search": ("Recherche Web", [3, 0, 33, 2]),
    "my_computer": ("Poste de Travail", [3, 0, 148, 1]),

    # Mouse actions
    "mouse_left": ("Clic Gauche Souris", [1, 0, 240, 0]),
    "mouse_right": ("Clic Droit Souris", [1, 0, 241, 0]),
    "mouse_middle": ("Clic Molette Souris", [1, 0, 242, 0]),
    "mouse_forward": ("Bouton Suivant Souris", [1, 0, 244, 0]),
    "mouse_back": ("Bouton Précédent Souris", [1, 0, 243, 0]),

    # System actions
    "system_power": ("Arrêt Système", [2, 129, 0, 0]),
    "system_sleep": ("Mise en Veille", [2, 130, 0, 0]),
    "system_wake": ("Réveil Système", [2, 131, 0, 0]),

    # Shortcuts
    "copy": ("Copier (Ctrl+C)", [0, 224, 6, 0]),
    "paste": ("Coller (Ctrl+V)", [0, 224, 25, 0]),
    "cut": ("Couper (Ctrl+X)", [0, 224, 27, 0]),
    # The K20 is an AZERTY keyboard: shortcuts are sent as HID usages, which
    # the OS translates with its own layout. Usage 0x1A is the key labelled
    # "Z" on AZERTY (0x1D would give Ctrl+W = close tab), 0x14 is "A"
    # (0x04 would give Ctrl+Q = quit). QWERTY variants are kept for users
    # whose OS layout is QWERTY.
    "undo": ("Annuler (Ctrl+Z)", [0, 224, 26, 0]),
    "save": ("Enregistrer (Ctrl+S)", [0, 224, 22, 0]),
    "select_all": ("Tout sélectionner (Ctrl+A)", [0, 224, 20, 0]),
    "undo_qwerty": ("Annuler (Ctrl+Z, OS en QWERTY)", [0, 224, 29, 0]),
    "select_all_qwerty": ("Tout sélectionner (Ctrl+A, OS en QWERTY)", [0, 224, 4, 0]),
    "lock_screen": ("Verrouiller écran (Win+L)", [0, 227, 15, 0]),
    "show_desktop": ("Afficher Bureau (Win+D)", [0, 227, 7, 0]),

    # Special
    "disabled": ("Désactivé", [0, 0, 0, 0]),
    # "default" restores the key's factory action (taken from DEFAULT_MATRIX /
    # DEFAULT_FN_MATRIX); its bytes are resolved per key at send time.
    "default": ("Action d'origine", []),
}



# Factory key matrix of the K20 (vendor "defaultMatrix" of yc3121_cx71d_soc):
# 4 bytes per key [type, modifier, code, extra], 6 rows per column. The index
# sent with CMD_SET_KEYMATRIX_SIMPLE is the entry index in this matrix, NOT
# the position in KEY_LAYOUT.
DEFAULT_MATRIX: List[int] = [
    0, 0, 41, 0, 0, 0, 53, 0, 0, 0, 43, 0, 0, 0, 57, 0, 0, 0, 225, 0, 0, 0, 224, 0,
    0, 0, 58, 0, 0, 0, 30, 0, 0, 0, 20, 0, 0, 0, 4, 0, 0, 0, 100, 0, 10, 1, 0, 0,
    0, 0, 59, 0, 0, 0, 31, 0, 0, 0, 26, 0, 0, 0, 22, 0, 0, 0, 29, 0, 0, 0, 227, 0,
    0, 0, 60, 0, 0, 0, 32, 0, 0, 0, 8, 0, 0, 0, 7, 0, 0, 0, 27, 0, 0, 0, 226, 0,
    0, 0, 61, 0, 0, 0, 33, 0, 0, 0, 21, 0, 0, 0, 9, 0, 0, 0, 6, 0, 0, 0, 58, 0,
    0, 0, 62, 0, 0, 0, 34, 0, 0, 0, 23, 0, 0, 0, 10, 0, 0, 0, 25, 0, 0, 0, 59, 0,
    0, 0, 63, 0, 0, 0, 35, 0, 0, 0, 28, 0, 0, 0, 11, 0, 0, 0, 5, 0, 0, 0, 44, 0,
    0, 0, 64, 0, 0, 0, 36, 0, 0, 0, 24, 0, 0, 0, 13, 0, 0, 0, 17, 0, 0, 0, 60, 0,
    0, 0, 65, 0, 0, 0, 37, 0, 0, 0, 12, 0, 0, 0, 14, 0, 0, 0, 16, 0, 0, 0, 61, 0,
    0, 0, 66, 0, 0, 0, 38, 0, 0, 0, 18, 0, 0, 0, 15, 0, 0, 0, 54, 0, 0, 0, 62, 0,
    0, 0, 67, 0, 0, 0, 39, 0, 0, 0, 19, 0, 0, 0, 51, 0, 0, 0, 55, 0, 0, 0, 230, 0,
    0, 0, 68, 0, 0, 0, 45, 0, 0, 0, 47, 0, 0, 0, 52, 0, 0, 0, 56, 0, 0, 0, 231, 0,
    0, 0, 69, 0, 0, 0, 46, 0, 0, 0, 48, 0, 0, 0, 0, 0, 0, 0, 229, 0, 0, 0, 80, 0,
    3, 0, 33, 2, 0, 0, 42, 0, 0, 0, 49, 0, 0, 0, 40, 0, 0, 0, 82, 0, 0, 0, 81, 0,
    0, 0, 76, 0, 0, 0, 74, 0, 0, 0, 77, 0, 0, 0, 75, 0, 0, 0, 78, 0, 0, 0, 79, 0,
    3, 0, 233, 0, 3, 0, 234, 0,
]
# Factory Fn layer (vendor "defaultFnMatrix"), same indexing.
DEFAULT_FN_MATRIX: List[int] = [
    10, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 13, 1, 0, 0, 0, 0, 0, 0,
    14, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    14, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 10, 3, 0, 0,
    14, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 61, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    3, 0, 226, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    3, 0, 234, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    3, 0, 233, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    3, 0, 182, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    3, 0, 205, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    3, 0, 181, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 227, 43, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 10, 3, 0, 0,
    0, 0, 226, 43, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 227, 15, 13, 5, 1, 0, 13, 3, 1, 0,
    0, 0, 0, 0, 10, 8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 13, 2, 1, 0, 13, 2, 2, 0,
    10, 2, 0, 0, 0, 0, 73, 0, 0, 0, 70, 0, 0, 0, 71, 0, 0, 0, 72, 0, 13, 3, 2, 0,
]
EMPTY_KEY_VALUE = 0xFFFFFFFF   # "Molette" (rotary knob press) placeholder
EMPTY_KEY_INDEX = 75           # vendor emptyKeyIndex for this model


def _matrix_entry(matrix: List[int], index: int) -> List[int]:
    entry = matrix[4 * index:4 * index + 4]
    return entry + [0] * (4 - len(entry))


def find_matrix_index(code: int, occurrence: int = 0) -> Optional[int]:
    """Vendor findIndexInDefaultMatrix(): locate a factory key code in DEFAULT_MATRIX."""
    if code == EMPTY_KEY_VALUE:
        return EMPTY_KEY_INDEX
    if code <= 0xFF:
        target = [0, 0, code, 0]
    else:
        target = list(code.to_bytes(4, "big"))
    seen = 0
    for idx in range(len(DEFAULT_MATRIX) // 4):
        if _matrix_entry(DEFAULT_MATRIX, idx) == target:
            if seen == occurrence:
                return idx
            seen += 1
    return None


KEY_MATRIX_INDEX: Dict[str, int] = {}
for _k in KEY_LAYOUT:
    _idx = find_matrix_index(_k["code"])
    if _idx is not None:
        KEY_MATRIX_INDEX[_k["name"]] = _idx
del _k, _idx


def _key_by_name(key_name: str) -> Dict[str, Any]:
    for k in KEY_LAYOUT:
        if k["name"].lower() == str(key_name).lower():
            return k
    raise ValueError(f"Touche inconnue: {key_name}")


def apply_checksum(payload: bytearray, checksum_type: Optional[str] = "bit7") -> bytearray:
    """One's-complement checksum used by the YC3121 firmware (see module doc)."""
    if checksum_type == "bit7":
        payload[7] = (~sum(payload[0:7])) & 0xFF
    elif checksum_type == "bit8":
        payload[8] = (~sum(payload[0:8])) & 0xFF
    return payload


def parse_hex_color(color: Optional[str]) -> Optional[Tuple[int, int, int]]:
    if color is None:
        return None
    if isinstance(color, (list, tuple)) and len(color) == 3:
        return tuple(max(0, min(255, int(c))) for c in color)
    text = str(color).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"Couleur invalide: {color!r} (format attendu #RRGGBB)")
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        raise ValueError(f"Couleur invalide: {color!r} (format attendu #RRGGBB)") from None


def format_hex_color(rgb: Tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _clamp(value, lo, hi) -> int:
    return max(lo, min(hi, int(value)))


# =============================================================================
# Pure frame builders (unit-tested byte for byte)
# =============================================================================

def build_led_frame(mode: str, speed: int, brightness: int, direction: int,
                    rgb: Tuple[int, int, int], colorful: bool) -> bytearray:
    code = LIGHT_CODE_BY_KEY[mode]
    p = bytearray(PAYLOAD_LEN)
    p[0] = CMD_SET_LEDPARAM
    p[1] = code
    flag = LED_DAZZLE if colorful else LED_NORMAL
    if code == 0:  # off: no speed/colour
        p[4] = LED_NORMAL
    elif code == 13:  # user picture: option index, fixed colour
        p[3] = _clamp(brightness, 0, 4)
        p[4] = 0
        p[5], p[6], p[7] = 0, 200, 200
    else:
        p[2] = LED_MAX_SPEED - (_clamp(speed, 1, 5) - 1)
        p[3] = _clamp(brightness, 0, 4)
        p[4] = ((_clamp(direction, 0, 3) << 4) | flag) if code == 4 else flag
        r, g, b = WHITE_SUBSTITUTE if tuple(rgb) == (255, 255, 255) else rgb
        p[5], p[6], p[7] = r, g, b
    return apply_checksum(p, "bit8")


def build_side_led_frame(mode: str, speed: int, brightness: int,
                         rgb: Tuple[int, int, int], colorful: bool) -> bytearray:
    code = SIDE_LIGHT_CODE_BY_KEY[mode]
    p = bytearray(PAYLOAD_LEN)
    p[0] = CMD_SET_SLEDPARAM
    p[1] = code
    flag = LED_DAZZLE if colorful else LED_NORMAL
    if code == 0:
        p[4] = LED_NORMAL
    else:
        # Unlike the main backlight, the side LED speed byte is not inverted.
        p[2] = _clamp(speed, 1, 5) - 1
        p[3] = _clamp(brightness, 0, 4)
        if code == 3:  # neon/rainbow: no colour, default option
            p[4] = LED_NORMAL
        else:
            p[4] = flag
            r, g, b = WHITE_SUBSTITUTE if tuple(rgb) == (255, 255, 255) else rgb
            p[5], p[6], p[7] = r, g, b
    return apply_checksum(p, "bit8")


def build_polling_rate_frame(rate_hz: int, profile_idx: int) -> bytearray:
    p = bytearray(PAYLOAD_LEN)
    p[0] = CMD_SET_REPORT
    p[1] = profile_idx
    p[2] = POLLING_RATE_MAP[rate_hz]
    return apply_checksum(p, "bit7")


def build_debounce_frame(debounce_ms: int) -> bytearray:
    p = bytearray(PAYLOAD_LEN)
    p[0] = CMD_SET_DEBOUNCE
    p[2] = debounce_ms
    return apply_checksum(p, "bit7")


def build_sleep_frame(light_sleep_sec: int, deep_sleep_sec: int) -> bytearray:
    p = bytearray(PAYLOAD_LEN)
    p[0] = CMD_SET_SLEEPTIME
    # [8..9] BT light, [10..11] 2.4G light, [12..13] BT deep, [14..15] 2.4G deep
    for offset, value in ((8, light_sleep_sec), (10, light_sleep_sec),
                          (12, deep_sleep_sec), (14, deep_sleep_sec)):
        p[offset] = value & 0xFF
        p[offset + 1] = (value >> 8) & 0xFF
    return apply_checksum(p, "bit7")


def build_options_frame(options_byte: int, fn_byte: int, power_byte: int, profile_idx: int) -> bytearray:
    p = bytearray(PAYLOAD_LEN)
    p[0] = CMD_SET_KBOPTION
    p[1] = profile_idx
    p[2] = options_byte & 0xFF
    p[3] = fn_byte & 0xFF
    p[4] = power_byte & 0xFF
    return apply_checksum(p, "bit7")


def build_key_frame(cmd: int, profile_idx: int, matrix_idx: int, action: List[int]) -> bytearray:
    p = bytearray(PAYLOAD_LEN)
    p[0] = cmd
    p[1] = profile_idx
    p[2] = matrix_idx
    apply_checksum(p, "bit7")  # covers bytes 0..6 only
    p[8:12] = bytes(action[:4])
    return p


def build_simple_frame(cmd: int, arg1: int = 0) -> bytearray:
    p = bytearray(PAYLOAD_LEN)
    p[0] = cmd
    p[1] = arg1
    return apply_checksum(p, "bit7")


# Keyboard option bits (vendor setKeyboardOption / getKeyboardOption)
OPT_WIN_LOCK = 1 << 0
OPT_OS_MAC = 1 << 1
OPT_WASD_SWAP = 1 << 3
OPT_LED_OFF = 1 << 4
OPT_SIDE_LED_OFF = 1 << 5
OPT_GAMING = 1 << 6
OPT_KEYBOARD_LOCK = 1 << 7
OS_MODES = ("win", "mac")


def encode_options(base: int, win_lock: bool, wasd_swap: bool, os_mode: str, gaming_mode: bool) -> int:
    value = base & ~(OPT_WIN_LOCK | OPT_OS_MAC | OPT_WASD_SWAP | OPT_GAMING) & 0xFF
    if win_lock:
        value |= OPT_WIN_LOCK
    if os_mode == "mac":
        value |= OPT_OS_MAC
    if wasd_swap:
        value |= OPT_WASD_SWAP
    if gaming_mode:
        value |= OPT_GAMING
    return value


# Backwards-compatible names (CI smoke test & external scripts).
_HIDIOCSFEATURE = hidraw.HIDIOCSFEATURE
_HIDIOCGFEATURE = hidraw.HIDIOCGFEATURE


class SkillkorpK20Driver(SkillkorpDeviceDriver):
    """High-level driver for the SkillKorp K20 Ultimate keyboard."""

    device_type = "keyboard"
    display_name = "Clavier SkillKorp K20"

    def __init__(self, dev_path: Optional[str] = None):
        self.last_error = ""
        self._pending_gap = 0.0
        self._last_command_end = 0.0
        self._options_cache: Optional[Tuple[int, int, int]] = None
        self._rf_initialised: Optional[str] = None
        # Settings sent while the keyboard sleeps are saved and marked pending
        # ("pending_sync" in the config), then re-sent once it answers again.
        self.defer_when_offline = True
        self.last_deferred = False
        self._syncing = False
        super().__init__(dev_path)

    # ------------------------------------------------------------------
    # Discovery / config plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _is_vendor_interface(node: HidrawNode) -> bool:
        rep = node.reports
        return 0xFFFF in rep.usage_pages and rep.feature.get(0) == PAYLOAD_LEN

    @classmethod
    def find_nodes(cls) -> List[HidrawNode]:
        nodes = hidraw.find_nodes(VENDOR_ID, (PID_WIRED, PID_WIRELESS), cls._is_vendor_interface)
        # Prefer the cable when both the cable and the receiver are plugged in.
        nodes.sort(key=lambda n: 0 if n.product_id == PID_WIRED else 1)
        return nodes

    def wireless_product_ids(self) -> tuple:
        return (PID_WIRELESS,)

    def config_file(self) -> str:
        return DEFAULT_PROFILE_FILE

    def default_config(self) -> Dict[str, Any]:
        return {
            "profile_name": "Par défaut",
            "profile_index": 0,
            "polling_rate": 1000,
            "debounce_ms": 8,
            "sleep": {
                "light_sleep_sec": 120,
                "deep_sleep_sec": 1680,
            },
            "options": {
                "win_lock": False,
                "wasd_swap": False,
                "os_mode": "win",
                "gaming_mode": False,
            },
            "rgb": {
                "mode": "wave",
                "speed": 3,
                "brightness": 4,
                "direction": 0,
                "color": "#FF0000",
                "colorful": True,
            },
            "side_rgb": {
                "mode": "rainbow",
                "speed": 3,
                "brightness": 4,
                "color": "#00FFCC",
                "colorful": False,
            },
            "key_remaps": {},
            "fn_remaps": {},
        }

    def sanitize_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        d = self.default_config()
        try:
            if cfg.get("polling_rate") not in POLLING_RATE_MAP:
                cfg["polling_rate"] = d["polling_rate"]
            cfg["profile_index"] = _clamp(cfg.get("profile_index", 0), 0, PROFILE_COUNT - 1)
            cfg["debounce_ms"] = _clamp(cfg.get("debounce_ms", 8), *DEBOUNCE_RANGE)
            rgb = cfg.setdefault("rgb", {})
            if rgb.get("mode") not in LIGHT_CODE_BY_KEY:
                rgb["mode"] = d["rgb"]["mode"]
            srgb = cfg.setdefault("side_rgb", {})
            if srgb.get("mode") not in SIDE_LIGHT_CODE_BY_KEY:
                srgb["mode"] = d["side_rgb"]["mode"]
            for section in (rgb, srgb):
                try:
                    parse_hex_color(section.get("color"))
                except ValueError:
                    section["color"] = "#FF0000"
            opts = cfg.setdefault("options", {})
            if opts.get("os_mode") not in OS_MODES:
                opts["os_mode"] = "win"
            for key in ("key_remaps", "fn_remaps"):
                if not isinstance(cfg.get(key), dict):
                    cfg[key] = {}
        except (TypeError, ValueError):
            return d
        return cfg

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _wait_command_gap(self, gap: float):
        remaining = self._last_command_end + gap - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    # Over the 2.4GHz receiver (sequence captured from the vendor software):
    #   init (once per plug-in): F7, [FE 40], F7, [F6 0A]
    #   write: F7 poll, then the frame unchanged (the keyboard applies it ~1 s
    #          later and the receiver emits input report 05 0F 00 00)
    #   read:  F7 poll, query frame, poll F7 until byte 0 = 1 (answer ready),
    #          [FC], then GET_FEATURE returns the keyboard's answer.
    # An answer that is never fetched with FC blocks the receiver: it drops
    # every following frame. _rf_prepare() drains such a leftover answer.
    @staticmethod
    def _rf_command(dev, *data: int) -> None:
        frame = bytearray(PAYLOAD_LEN)
        frame[:len(data)] = bytes(data)
        dev.set_feature(b"\x00" + bytes(frame))

    @staticmethod
    def _get_payload(dev) -> bytes:
        data = dev.get_feature(0, PAYLOAD_LEN + 1)
        return bytes(data[1:]) if len(data) > PAYLOAD_LEN else bytes(data)

    def _rf_status(self, dev) -> bytes:
        """Receiver status: [answer ready, battery %, 0, offline, 1, 1, 1, ...]
        (bytes past 6 are leftovers of the previous frame)."""
        self._rf_command(dev, RF_GET_STATUS)
        time.sleep(0.015)
        return self._get_payload(dev)

    def _rf_fetch(self, dev) -> bytes:
        self._rf_command(dev, RF_FETCH_REPLY)
        time.sleep(0.03)
        return self._get_payload(dev)

    def _rf_prepare(self, dev) -> bytes:
        """Initialise the receiver once per plug-in, refuse to send while the
        keyboard is offline and drain any answer left unread."""
        status = self._rf_status(dev)
        key = hidraw.usb_device_path(dev.node) or dev.node.devnode
        if self._rf_initialised != key:
            self._rf_command(dev, RF_SET_NEXT_PACK_LENGTH, PAYLOAD_LEN)
            time.sleep(0.1)
            status = self._rf_status(dev)
            self._rf_command(dev, RF_SET_PARAM, RF_PARAM_VALUE)
            time.sleep(0.015)
            self._rf_initialised = key
        if len(status) > RF_STATUS_OFFLINE and status[RF_STATUS_OFFLINE]:
            raise KeyboardOfflineError("Le clavier est éteint ou en veille : appuyez sur une touche puis réessayez.")
        if status and status[RF_STATUS_REPLY_READY]:
            self._rf_fetch(dev)
            status = self._rf_status(dev)
            if status and status[RF_STATUS_REPLY_READY]:
                raise DeviceError("Le récepteur 2.4 GHz ne répond plus correctement : "
                                  "débranchez-le puis rebranchez-le.")
        return status

    def _write_frame(self, dev, payload: bytes):
        if dev.node.product_id == PID_WIRELESS:
            self._rf_prepare(dev)
        dev.set_feature(b"\x00" + bytes(payload[:PAYLOAD_LEN]))

    def _send(self, payloads, settle: float = DELAY_COMMON) -> None:
        """Send one or more 64-byte payloads in a single locked transaction.

        Raises DeviceError on failure. *settle* is the time the firmware needs
        before accepting the next command (enforced lazily before the next send).
        """
        if isinstance(payloads, (bytes, bytearray)):
            payloads = [payloads]
        self._wait_command_gap(getattr(self, "_pending_gap", 0.0))
        with self.open_device() as dev:
            for i, payload in enumerate(payloads):
                if i:
                    time.sleep(DELAY_KEY)
                self._write_frame(dev, payload)
        self._last_command_end = time.monotonic()
        self._pending_gap = settle

    def _send_feature_report(self, payload: bytes, checksum_type: Optional[str] = "bit7") -> bool:
        """Compatibility wrapper: checksum + send, returns True/False."""
        p = bytearray(PAYLOAD_LEN)
        p[:min(len(payload), PAYLOAD_LEN)] = bytes(payload[:PAYLOAD_LEN])
        if checksum_type:
            apply_checksum(p, checksum_type)
        try:
            self._send(p)
            return True
        except DeviceError as e:
            self.last_error = str(e)
            return False

    def _query(self, cmd: int, arg1: int = 0, timeout: float = 0.02) -> bytes:
        """Write a GET command and read the 64-byte answer (report ID stripped)."""
        self._wait_command_gap(getattr(self, "_pending_gap", 0.0))
        with self.open_device() as dev:
            if dev.node.product_id == PID_WIRELESS:
                data = self._rf_query(dev, cmd, arg1)
            else:
                self._write_frame(dev, build_simple_frame(cmd, arg1))
                time.sleep(timeout)
                data = self._get_payload(dev)
        self._last_command_end = time.monotonic()
        self._pending_gap = 0.0
        return data

    def _rf_query(self, dev, cmd: int, arg1: int) -> bytes:
        self._write_frame(dev, build_simple_frame(cmd, arg1))
        deadline = time.monotonic() + RF_REPLY_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.1)
            status = self._rf_status(dev)
            if status and status[RF_STATUS_REPLY_READY]:
                return self._rf_fetch(dev)
        raise DeviceError("Le clavier n'a pas répondu via le récepteur 2.4 GHz.")

    def _read_feature_report(self, cmd: int, length: int = 65, checksum_type: str = "bit7") -> Optional[bytearray]:
        """Compatibility wrapper returning [report_id] + payload, or None."""
        try:
            return bytearray(b"\x00" + self._query(cmd))
        except DeviceError as e:
            self.last_error = str(e)
            return None

    @staticmethod
    def _apply_checksum(payload: bytearray, checksum_type: str = "bit7") -> bytearray:
        return apply_checksum(payload, checksum_type)

    def _guard(self, func, *args, pending: Optional[str] = None, **kwargs) -> bool:
        """Run a transport call: True on success, False if the keyboard refused
        the frame. Missing device / permission / busy errors are raised.

        *pending* names the setting being written: if the keyboard is asleep
        it is recorded for sync_pending() and the call counts as a success
        (the caller then saves the new value); last_deferred tells which."""
        self.last_deferred = False
        try:
            func(*args, **kwargs)
            self.last_error = ""
        except (DevicePermissionError, DeviceNotFoundError, DeviceBusyError):
            raise
        except KeyboardOfflineError as e:
            if not (pending and self.defer_when_offline and not self._syncing):
                self.last_error = str(e)
                return False
            self._mark_pending(pending)
            self.last_deferred = True
            self.last_error = ""
            return True
        except DeviceError as e:
            self.last_error = str(e)
            return False
        if pending and not self._syncing:
            self._unmark_pending(pending)
            self.sync_pending()
        return True

    # ------------------------------------------------------------------
    # Settings deferred while the keyboard sleeps
    # ------------------------------------------------------------------
    def _mark_pending(self, key: str) -> None:
        def mutate(cfg):
            keys = set(cfg.get("pending_sync") or [])
            keys.add(key)
            cfg["pending_sync"] = sorted(keys)
        self.update_config(mutate)

    def _unmark_pending(self, key: str) -> None:
        if key in (self.config.get("pending_sync") or []):
            self.update_config(lambda cfg: cfg.__setitem__(
                "pending_sync", [k for k in cfg.get("pending_sync") or [] if k != key]))

    def pending_settings(self) -> List[str]:
        self.reload_config_if_changed()
        pending = self.config.get("pending_sync")
        return list(pending) if isinstance(pending, list) else []

    def sync_pending(self) -> bool:
        """Re-send the settings saved while the keyboard was asleep.

        Returns True when nothing is left pending. Cheap while the keyboard
        still sleeps (one receiver status poll), so it can be called from a
        periodic status check."""
        pending = self.pending_settings()
        if not pending or self._syncing:
            return not pending
        cfg = self.config
        # The hardware profile first: the other settings are stored per profile.
        order = sorted(pending, key=lambda k: (k != "profile_index", k))
        self._syncing = True
        try:
            for key in order:
                section, _, name = key.partition(":")
                if section == "profile_index":
                    ok = self.set_profile(cfg.get("profile_index", 0))
                elif section == "rgb":
                    ok = self.set_rgb()
                elif section == "side_rgb":
                    ok = self.set_side_rgb()
                elif section == "polling_rate":
                    ok = self.set_polling_rate(cfg.get("polling_rate", 1000))
                elif section == "debounce_ms":
                    ok = self.set_debounce(cfg.get("debounce_ms", 8))
                elif section == "sleep":
                    sl = cfg.get("sleep", {})
                    ok = self.set_sleep_time(sl.get("light_sleep_sec", 120), sl.get("deep_sleep_sec", 1680))
                elif section == "options":
                    ok = self.set_keyboard_options()
                elif section in ("key_remaps", "fn_remaps") and name:
                    action = cfg.get(section, {}).get(name, "default")
                    ok = self._remap(name, action, None, section == "fn_remaps")
                else:
                    ok = True  # unknown entry (older/newer version): drop it
                if not ok:
                    return False  # still asleep or refused: keep the rest pending
                self._unmark_pending(key)
                cfg = self.config
        except ValueError:
            return False
        finally:
            self._syncing = False
        return not self.pending_settings()

    # ------------------------------------------------------------------
    # Hardware controls (return True on success, False on device error,
    # raise ValueError on invalid arguments)
    # ------------------------------------------------------------------
    def _resolve_led(self, section: str, mode, speed, brightness, color, colorful):
        cur = self.config.get(section, {})
        mode = (mode or cur.get("mode") or "static").lower()
        table = LIGHT_CODE_BY_KEY if section == "rgb" else SIDE_LIGHT_CODE_BY_KEY
        if mode not in table:
            raise ValueError(f"Mode d'éclairage inconnu: {mode}. Choix: {', '.join(table)}")
        speed = _clamp(cur.get("speed", 3) if speed is None else speed, 1, 5)
        brightness = _clamp(cur.get("brightness", 4) if brightness is None else brightness, 0, 4)
        rgb = parse_hex_color(color) if color is not None else parse_hex_color(cur.get("color", "#FF0000"))
        colorful = bool(cur.get("colorful", False) if colorful is None else colorful)
        return mode, speed, brightness, rgb, colorful

    def set_rgb(self, mode: Optional[str] = None, speed: Optional[int] = None,
                brightness: Optional[int] = None, direction: Optional[int] = None,
                color: Optional[str] = None, colorful: Optional[bool] = None) -> bool:
        """Configure the key backlight. Unspecified values keep their current setting.

        - mode: one of LIGHT_MODES keys
        - speed: 1 (slow) .. 5 (fast); brightness: 0 .. 4
        - direction (wave): 0 right, 1 left, 2 down, 3 up
        - color: '#RRGGBB'; colorful: True = multicolour effect, False = color
        """
        self.reload_config_if_changed()
        mode, speed, brightness, rgb, colorful = self._resolve_led("rgb", mode, speed, brightness, color, colorful)
        direction = _clamp(self.config.get("rgb", {}).get("direction", 0) if direction is None else direction, 0, 3)
        frame = build_led_frame(mode, speed, brightness, direction, rgb, colorful)
        if not self._guard(self._send, frame, pending="rgb"):
            return False
        self.commit({"rgb": {"mode": mode, "speed": speed, "brightness": brightness,
                             "direction": direction, "color": format_hex_color(rgb),
                             "colorful": colorful}})
        return True

    def set_side_rgb(self, mode: Optional[str] = None, speed: Optional[int] = None,
                     brightness: Optional[int] = None, color: Optional[str] = None,
                     colorful: Optional[bool] = None) -> bool:
        """Configure the side LED strips (same conventions as set_rgb)."""
        self.reload_config_if_changed()
        mode, speed, brightness, rgb, colorful = self._resolve_led("side_rgb", mode, speed, brightness, color, colorful)
        frame = build_side_led_frame(mode, speed, brightness, rgb, colorful)
        if not self._guard(self._send, frame, pending="side_rgb"):
            return False
        self.commit({"side_rgb": {"mode": mode, "speed": speed, "brightness": brightness,
                                  "color": format_hex_color(rgb), "colorful": colorful}})
        return True

    def set_polling_rate(self, rate_hz: int) -> bool:
        """Set keyboard polling rate: 125, 250, 500, or 1000 Hz."""
        if rate_hz not in POLLING_RATE_MAP:
            raise ValueError(f"Taux de rafraîchissement invalide: {rate_hz} Hz. Choix: 125, 250, 500, 1000.")
        self.reload_config_if_changed()
        frame = build_polling_rate_frame(rate_hz, self.config.get("profile_index", 0))
        if not self._guard(self._send, frame, DELAY_BIG, pending="polling_rate"):
            return False
        self.commit({"polling_rate": rate_hz})
        return True

    def set_sleep_time(self, light_sleep_sec: int, deep_sleep_sec: int) -> bool:
        """Inactivity timeouts in seconds (0 disables). Light: 60..3600, deep: 600..3600."""
        light = int(light_sleep_sec)
        deep = int(deep_sleep_sec)
        light = 0 if light <= 0 else _clamp(light, *LIGHT_SLEEP_RANGE)
        deep = 0 if deep <= 0 else _clamp(deep, *DEEP_SLEEP_RANGE)
        if light and deep and deep < light:
            deep = max(light, DEEP_SLEEP_RANGE[0])
        if not self._guard(self._send, build_sleep_frame(light, deep), pending="sleep"):
            return False
        self.commit({"sleep": {"light_sleep_sec": light, "deep_sleep_sec": deep}})
        return True

    def set_debounce(self, debounce_ms: int) -> bool:
        """Switch debounce filter in ms (2..20)."""
        debounce_ms = _clamp(debounce_ms, *DEBOUNCE_RANGE)
        if not self._guard(self._send, build_debounce_frame(debounce_ms), pending="debounce_ms"):
            return False
        self.commit({"debounce_ms": debounce_ms})
        return True

    def read_options(self) -> Optional[Tuple[int, int, int]]:
        """Read (options, fn, power) bytes from the keyboard, or None if unavailable."""
        try:
            data = self._query(CMD_GET_KBOPTION, self.config.get("profile_index", 0))
        except DeviceError:
            return None
        if len(data) < 5 or data[0] != CMD_GET_KBOPTION:
            return None
        current = (data[2], data[3], data[4])
        if self.config.get("options_raw") != list(current):
            self.commit({"options_raw": list(current)})
        return current

    def _known_options(self) -> Tuple[int, int, int]:
        """Last option bytes known: read live (cable), else the ones saved
        from the last cable connection, so wireless writes keep them."""
        live = self.read_options()
        if live:
            return live
        if self._options_cache:
            return self._options_cache
        raw = self.config.get("options_raw")
        if isinstance(raw, list) and len(raw) == 3 and all(isinstance(b, int) and 0 <= b <= 255 for b in raw):
            return tuple(raw)
        return (0, 0, 0)

    def set_keyboard_options(self, win_lock: Optional[bool] = None, wasd_swap: Optional[bool] = None,
                             os_mode: Optional[str] = None, gaming_mode: Optional[bool] = None) -> bool:
        """Win lock, WASD/arrows swap, OS mode ('win'/'mac') and gaming mode.

        Option bits we do not manage (LED off, keyboard lock, Fn matrix, power
        saving) are read back from the keyboard first and preserved.
        """
        self.reload_config_if_changed()
        opts = self.config.get("options", {})
        win_lock = opts.get("win_lock", False) if win_lock is None else bool(win_lock)
        wasd_swap = opts.get("wasd_swap", False) if wasd_swap is None else bool(wasd_swap)
        gaming_mode = opts.get("gaming_mode", False) if gaming_mode is None else bool(gaming_mode)
        os_mode = (opts.get("os_mode", "win") if os_mode is None else str(os_mode)).lower()
        if os_mode not in OS_MODES:
            raise ValueError(f"Mode système non pris en charge par le K20: {os_mode} (choix: win, mac)")

        current = self._known_options()
        options_byte = encode_options(current[0], win_lock, wasd_swap, os_mode, gaming_mode)
        frame = build_options_frame(options_byte, current[1], current[2], self.config.get("profile_index", 0))
        if not self._guard(self._send, frame, pending="options"):
            return False
        self._options_cache = (options_byte, current[1], current[2])
        self.commit({"options": {"win_lock": win_lock, "wasd_swap": wasd_swap,
                                 "os_mode": os_mode, "gaming_mode": gaming_mode},
                     "options_raw": list(self._options_cache)})
        return True

    def set_profile(self, profile_idx: int) -> bool:
        """Switch the on-board hardware profile (0, 1 or 2)."""
        profile_idx = int(profile_idx)
        if not 0 <= profile_idx < PROFILE_COUNT:
            raise ValueError(f"Profil matériel invalide: {profile_idx} (0 à {PROFILE_COUNT - 1}).")
        if not self._guard(self._send, build_simple_frame(CMD_SET_PROFILE, profile_idx), DELAY_BIG,
                           pending="profile_index"):
            return False
        self.commit({"profile_index": profile_idx})
        return True

    def _action_bytes(self, key: Dict[str, Any], action_key: str, fn_layer: bool) -> List[int]:
        if action_key not in REMAP_ACTIONS:
            raise ValueError(f"Action inconnue: {action_key}. Choix: {', '.join(REMAP_ACTIONS)}")
        if action_key == "default":
            idx = KEY_MATRIX_INDEX[key["name"]]
            return _matrix_entry(DEFAULT_FN_MATRIX if fn_layer else DEFAULT_MATRIX, idx)
        return list(REMAP_ACTIONS[action_key][1])

    def _remap(self, key_name: str, action_key: str, profile_idx: Optional[int], fn_layer: bool) -> bool:
        key = _key_by_name(key_name)
        if key["name"] not in KEY_MATRIX_INDEX:
            raise ValueError(f"La touche {key['name']} ne peut pas être remappée.")
        action = self._action_bytes(key, action_key, fn_layer)
        self.reload_config_if_changed()
        if profile_idx is None:
            profile_idx = self.config.get("profile_index", 0)
        cmd = CMD_SET_FN_SIMPLE if fn_layer else CMD_SET_KEYMATRIX_SIMPLE
        frame = build_key_frame(cmd, int(profile_idx), KEY_MATRIX_INDEX[key["name"]], action)
        section = "fn_remaps" if fn_layer else "key_remaps"
        if not self._guard(self._send, frame, DELAY_KEY, pending=f"{section}:{key['name']}"):
            return False

        def mutate(cfg):
            remaps = cfg.setdefault(section, {})
            if action_key == "default":
                remaps.pop(key["name"], None)
            else:
                remaps[key["name"]] = action_key
        self.update_config(mutate)
        return True

    def remap_key(self, key_name: str, action_key: str, profile_idx: Optional[int] = None) -> bool:
        """Remap a key of the base layer ('default' restores its factory action)."""
        return self._remap(key_name, action_key, profile_idx, fn_layer=False)

    def remap_fn_key(self, key_name: str, action_key: str, profile_idx: Optional[int] = None) -> bool:
        """Remap Fn + key ('default' restores its factory action)."""
        return self._remap(key_name, action_key, profile_idx, fn_layer=True)

    def factory_reset(self) -> bool:
        """Restore the keyboard to factory settings."""
        if not self._guard(self._send, build_simple_frame(CMD_SET_RESERT), DELAY_BIG * 2):
            return False
        self._options_cache = None
        self.update_config(lambda cfg: (cfg.clear(), cfg.update(self.default_config())))  # also drops pending_sync
        return True

    def get_firmware_version(self) -> Optional[int]:
        try:
            data = self._query(CMD_GET_REV)
        except DeviceError:
            return None
        if len(data) < 3 or data[0] != CMD_GET_REV:
            return None
        return data[1] | (data[2] << 8)

    def get_battery(self) -> Dict[str, Any]:
        """Battery status: level from the 2.4GHz receiver (byte 1 of its 0xF7
        status); over the cable the keyboard is charging."""
        if not self.is_connected():
            return {"percentage": None, "charging": False, "connected": False,
                    "wireless": False, "status_str": "Non connecté"}
        wireless = self.is_wireless()
        if not wireless:
            # Over the cable the keyboard charges. It gives no level there
            # (0x83 answers 83 00 01) and the receiver keeps the last one.
            return {"percentage": None, "charging": True, "connected": True,
                    "wireless": False, "status_str": "En charge (câble USB)"}
        try:
            self._wait_command_gap(getattr(self, "_pending_gap", 0.0))
            with self.open_device() as dev:
                status = self._rf_status(dev)
        except DeviceError as e:
            return {"percentage": None, "charging": False, "connected": True,
                    "wireless": True, "status_str": f"Erreur de communication ({e})"}
        # Receiver status [busy, battery %, ...]: the same value the vendor
        # software polls every 2 s.
        if len(status) > RF_STATUS_BATTERY and 1 <= status[RF_STATUS_BATTERY] <= 100:
            pct = status[RF_STATUS_BATTERY]
            return {"percentage": pct, "charging": False, "connected": True,
                    "wireless": True, "status_str": f"{pct}%"}
        return {"percentage": None, "charging": False, "connected": True, "wireless": True,
                "status_str": "Niveau de batterie non communiqué par le clavier"}

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------
    def apply_profile(self, profile: Dict[str, Any]) -> bool:
        """Apply a complete profile; returns True only if every step succeeded.

        Remaps present in the previous state but absent from the profile are
        restored to their factory action so profiles never leak into each other.
        """
        self.reload_config_if_changed()
        errors: List[str] = []

        def step(label, func, *args, **kwargs):
            try:
                if not func(*args, **kwargs):
                    errors.append(label)
            except (ValueError, TypeError, KeyError) as e:
                errors.append(f"{label} ({e})")

        if "polling_rate" in profile:
            step("taux de rafraîchissement", self.set_polling_rate, profile["polling_rate"])
        if "debounce_ms" in profile:
            step("anti-rebond", self.set_debounce, profile["debounce_ms"])
        if isinstance(profile.get("sleep"), dict):
            s = profile["sleep"]
            step("veille", self.set_sleep_time, s.get("light_sleep_sec", 120), s.get("deep_sleep_sec", 1680))
        if isinstance(profile.get("options"), dict):
            o = profile["options"]
            step("options", self.set_keyboard_options, win_lock=o.get("win_lock"),
                 wasd_swap=o.get("wasd_swap"), os_mode=o.get("os_mode"), gaming_mode=o.get("gaming_mode"))
        if isinstance(profile.get("rgb"), dict):
            r = profile["rgb"]
            step("éclairage", self.set_rgb, mode=r.get("mode", "wave"), speed=r.get("speed", 3),
                 brightness=r.get("brightness", 4), direction=r.get("direction", 0),
                 color=r.get("color"), colorful=r.get("colorful", r.get("mode", "wave") == "wave"))
        if isinstance(profile.get("side_rgb"), dict):
            sr = profile["side_rgb"]
            step("éclairage latéral", self.set_side_rgb, mode=sr.get("mode", "rainbow"),
                 speed=sr.get("speed", 3), brightness=sr.get("brightness", 4),
                 color=sr.get("color"), colorful=sr.get("colorful", False))

        for section, fn_layer in (("key_remaps", False), ("fn_remaps", True)):
            wanted = profile.get(section)
            if not isinstance(wanted, dict):
                continue
            current = dict(self.config.get(section, {}))
            for key_name in current:
                if key_name not in wanted:
                    step(f"restauration {key_name}", self._remap, key_name, "default", None, fn_layer)
            for key_name, action in wanted.items():
                if current.get(key_name) != action:
                    step(f"remappage {key_name}", self._remap, key_name, action, None, fn_layer)

        if errors:
            self.last_error = "Échec: " + ", ".join(errors)
            return False
        return True


if __name__ == "__main__":
    driver = SkillkorpK20Driver()
    print(f"Device Node: {driver.dev_path}")
    print(f"Connected: {driver.is_connected()}")
    print(f"Wireless: {driver.is_wireless()}")
    print("Firmware:", driver.get_firmware_version())
    print("Battery:", driver.get_battery())
