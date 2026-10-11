#!/usr/bin/env bash
# ==============================================================================
# Installation de SkillKorp Suite (clavier SkillKorp K20 + souris SkillKorp M20)
#
#   ./install.sh              détecte la distribution et installe le paquet natif
#                             (.deb, .rpm ou paquet Arch), sinon installe dans /usr
#   ./install.sh --system     installation directe dans /usr
#   ./install.sh --user       installation dans ~/.local (+ règle udev via sudo)
#   ./install.sh --uninstall  désinstallation complète (les profils sont conservés)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"
PKG_NAME="skillkorp-suite"
RULE="70-skillkorp-suite.rules"
OLD_RULE="99-skillkorp-suite.rules"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

info() { printf '\033[1;34m%s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

require_python() {
    command -v python3 >/dev/null 2>&1 || die "python3 est requis."
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "Python 3.10 ou plus récent est requis."
}

detect_distro() {
    DISTRO_ID="unknown"; DISTRO_LIKE=""
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        DISTRO_ID="${ID:-unknown}"
        DISTRO_LIKE="${ID_LIKE:-}"
    fi
}

reload_udev() {
    $SUDO rm -f "/etc/udev/rules.d/$OLD_RULE" "/usr/lib/udev/rules.d/$OLD_RULE"
    if command -v udevadm >/dev/null 2>&1; then
        $SUDO udevadm control --reload-rules || true
        $SUDO udevadm trigger --subsystem-match=hidraw --action=change || true
        $SUDO sh "$SCRIPT_DIR/packaging/common/restrict-access.sh" || true
    fi
}

remove_legacy_layout() {
    # Files installed by 1.0.x outside of a package manager.
    $SUDO rm -f /usr/share/icons/hicolor/256x256/apps/skillkorp-suite.png
    rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/skillkorp-suite.png"
}

build_packages() {
    info "📦 Construction des paquets..."
    python3 "$SCRIPT_DIR/packaging/build_packages.py" "$@"
}

install_system_direct() {
    info "⚙️  Installation directe dans /usr..."
    require_python
    remove_legacy_layout
    $SUDO python3 "$SCRIPT_DIR/packaging/build_packages.py" --install /
    reload_udev
    $SUDO update-desktop-database -q /usr/share/applications 2>/dev/null || true
    $SUDO gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
}

install_user_mode() {
    info "👤 Installation utilisateur dans ~/.local..."
    require_python
    remove_legacy_layout
    python3 "$SCRIPT_DIR/packaging/build_packages.py" --install / --prefix "$HOME/.local" --user
    update-desktop-database -q "$HOME/.local/share/applications" 2>/dev/null || true
    info "🔑 Règle udev (accès sans root au clavier et à la souris) :"
    $SUDO install -D -m 0644 "$SCRIPT_DIR/udev/$RULE" "/etc/udev/rules.d/$RULE"
    reload_udev
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) printf '\n⚠ Ajoutez ~/.local/bin à votre PATH pour utiliser skillkorpctl.\n' ;;
    esac
}

install_native_package() {
    local pkg=""
    if echo "$DISTRO_ID $DISTRO_LIKE" | grep -Eqw 'debian|ubuntu|linuxmint|pop'; then
        command -v apt-get >/dev/null 2>&1 || return 1
        [ -n "$(ls "$DIST_DIR"/${PKG_NAME}_*_all.deb 2>/dev/null)" ] || build_packages --formats deb || return 1
        pkg=$(ls -t "$DIST_DIR"/${PKG_NAME}_*_all.deb 2>/dev/null | head -n 1)
        [ -n "$pkg" ] || return 1
        info "📦 Installation de $(basename "$pkg") via APT..."
        $SUDO apt-get install -y "$pkg"
    elif echo "$DISTRO_ID $DISTRO_LIKE" | grep -Eqw 'suse|opensuse|opensuse-tumbleweed|opensuse-leap'; then
        command -v zypper >/dev/null 2>&1 || return 1
        pkg=$(ls -t "$DIST_DIR"/${PKG_NAME}-*.suse.noarch.rpm 2>/dev/null | head -n 1)
        [ -n "$pkg" ] || { command -v rpmbuild >/dev/null 2>&1 && build_packages --formats rpm \
            && pkg=$(ls -t "$DIST_DIR"/${PKG_NAME}-*.suse.noarch.rpm 2>/dev/null | head -n 1); } || return 1
        [ -n "$pkg" ] || return 1
        info "📦 Installation de $(basename "$pkg") via zypper..."
        $SUDO zypper --non-interactive install --allow-unsigned-rpm "$pkg"
    elif echo "$DISTRO_ID $DISTRO_LIKE" | grep -Eqw 'fedora|rhel|centos|rocky|almalinux|nobara'; then
        command -v dnf >/dev/null 2>&1 || return 1
        pkg=$(ls -t "$DIST_DIR"/${PKG_NAME}-*.fc.noarch.rpm 2>/dev/null | head -n 1)
        [ -n "$pkg" ] || { command -v rpmbuild >/dev/null 2>&1 && build_packages --formats rpm \
            && pkg=$(ls -t "$DIST_DIR"/${PKG_NAME}-*.fc.noarch.rpm 2>/dev/null | head -n 1); } || return 1
        [ -n "$pkg" ] || return 1
        info "📦 Installation de $(basename "$pkg") via DNF..."
        $SUDO dnf install -y "$pkg"
    elif echo "$DISTRO_ID $DISTRO_LIKE" | grep -Eqw 'arch|manjaro|endeavouros|cachyos|garuda'; then
        command -v pacman >/dev/null 2>&1 || return 1
        pkg=$(ls -t "$DIST_DIR"/${PKG_NAME}-*-any.pkg.tar.zst 2>/dev/null | head -n 1)
        if [ -z "$pkg" ] && command -v makepkg >/dev/null 2>&1; then
            build_packages --formats src,arch
            info "📦 Construction et installation via makepkg..."
            (cd "$DIST_DIR" && makepkg -si --noconfirm -p PKGBUILD)
            return 0
        fi
        [ -n "$pkg" ] || return 1
        info "📦 Installation de $(basename "$pkg") via pacman..."
        $SUDO pacman -U --noconfirm "$pkg"
    else
        return 1
    fi
}

uninstall() {
    info "🗑️  Désinstallation de SkillKorp Suite..."
    if command -v dpkg >/dev/null 2>&1 && dpkg -s "$PKG_NAME" >/dev/null 2>&1; then
        $SUDO apt-get remove -y "$PKG_NAME"
    elif command -v rpm >/dev/null 2>&1 && rpm -q "$PKG_NAME" >/dev/null 2>&1; then
        if command -v zypper >/dev/null 2>&1; then $SUDO zypper --non-interactive remove "$PKG_NAME"
        else $SUDO dnf remove -y "$PKG_NAME"; fi
    elif command -v pacman >/dev/null 2>&1 && pacman -Q "$PKG_NAME" >/dev/null 2>&1; then
        $SUDO pacman -R --noconfirm "$PKG_NAME"
    fi
    command -v skillkorpctl >/dev/null 2>&1 && skillkorpctl tray stop >/dev/null 2>&1 || true

    $SUDO rm -rf "/usr/share/$PKG_NAME"
    $SUDO rm -f /usr/bin/skillkorpctl /usr/bin/skillkorp-gui /usr/bin/skillkorp-tray
    $SUDO rm -f "/usr/lib/udev/rules.d/$RULE" "/etc/udev/rules.d/$RULE" \
               "/usr/lib/udev/rules.d/$OLD_RULE" "/etc/udev/rules.d/$OLD_RULE"
    $SUDO rm -f /usr/lib/systemd/user/skillkorp-daemon.service
    $SUDO rm -f /etc/xdg/autostart/io.github.skillkorp.suite.tray.desktop
    $SUDO rm -rf /usr/share/gnome-shell/extensions/skillkorp-focus@skillkorp.github.io
    $SUDO rm -f /usr/share/applications/io.github.skillkorp.suite.desktop \
               /usr/share/metainfo/io.github.skillkorp.suite.metainfo.xml \
               /usr/share/icons/hicolor/512x512/apps/skillkorp-suite.png \
               /usr/share/icons/hicolor/256x256/apps/skillkorp-suite.png
    $SUDO rm -rf "/usr/share/doc/$PKG_NAME" "/usr/share/licenses/$PKG_NAME"

    rm -rf "$HOME/.local/share/$PKG_NAME" "$HOME/.local/share/doc/$PKG_NAME" "$HOME/.local/share/licenses/$PKG_NAME"
    rm -f "$HOME/.local/bin/skillkorpctl" "$HOME/.local/bin/skillkorp-gui" "$HOME/.local/bin/skillkorp-tray"
    rm -rf "$HOME/.local/share/gnome-shell/extensions/skillkorp-focus@skillkorp.github.io"
    rm -f "$HOME/.local/share/applications/io.github.skillkorp.suite.desktop" \
          "$HOME/.local/share/metainfo/io.github.skillkorp.suite.metainfo.xml" \
          "$HOME/.local/share/icons/hicolor/512x512/apps/skillkorp-suite.png" \
          "$HOME/.local/share/icons/hicolor/256x256/apps/skillkorp-suite.png"
    rm -f "$HOME/.config/autostart/io.github.skillkorp.suite.tray.desktop"
    command -v udevadm >/dev/null 2>&1 && $SUDO udevadm control --reload-rules || true
    ok "Désinstallation terminée."
    echo "  (Vos profils dans ~/.config/skillkorp sont conservés ; supprimez-les si besoin.)"
}

usage() {
    sed -n '3,10p' "$0" | sed 's/^# \{0,1\}//'
}

MODE="auto"
for arg in "$@"; do
    case "$arg" in
        --user) MODE="user" ;;
        --system) MODE="system" ;;
        --package) MODE="package" ;;
        --uninstall) MODE="uninstall" ;;
        -h|--help) usage; exit 0 ;;
        *) die "Option inconnue : $arg (voir --help)" ;;
    esac
done

[ "$MODE" = "uninstall" ] && { uninstall; exit 0; }

echo "============================================================"
echo "   Installation de SkillKorp Suite (clavier K20 + souris M20)"
echo "============================================================"
detect_distro
info "🔍 Distribution détectée : $DISTRO_ID ${DISTRO_LIKE:+(famille : $DISTRO_LIKE)}"

case "$MODE" in
    user) install_user_mode ;;
    system) install_system_direct ;;
    package) require_python; install_native_package || die "Aucun paquet natif utilisable pour cette distribution." ;;
    auto)
        require_python
        if ! install_native_package; then
            info "Pas de paquet natif pour cette distribution : installation directe."
            install_system_direct
        fi
        ;;
esac

echo
ok "Installation terminée."
echo "  • skillkorpctl status    vue d'ensemble clavier + souris"
echo "  • skillkorpctl doctor    vérification de l'installation"
echo "  • skillkorp-gui          application graphique"
echo "  • skillkorp-tray         indicateur de la barre système"
echo "Débranchez puis rebranchez le récepteur si les périphériques ne sont pas accessibles."
