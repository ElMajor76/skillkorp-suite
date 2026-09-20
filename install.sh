#!/usr/bin/env bash
set -e

# ==============================================================================
# Script d'installation universel pour SkillKorp Suite sous Linux
# (clavier SkillKorp K20 Ultimate + souris SkillKorp M20 Ultimate)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"
PKG_NAME="skillkorp-suite"

detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        DISTRO_ID="$ID"
        DISTRO_LIKE="${ID_LIKE:-$ID}"
    else
        DISTRO_ID="unknown"
        DISTRO_LIKE="unknown"
    fi
}

install_system_direct() {
    echo "⚙️  Installation directe dans le système (/usr)..."
    sudo mkdir -p "/usr/share/$PKG_NAME"
    sudo mkdir -p /usr/bin
    sudo mkdir -p /usr/lib/udev/rules.d
    sudo mkdir -p /usr/share/applications
    sudo mkdir -p /usr/share/icons/hicolor/256x256/apps

    sudo cp -r "$SCRIPT_DIR/skillkorp" "/usr/share/$PKG_NAME/skillkorp"
    sudo find "/usr/share/$PKG_NAME/skillkorp" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    sudo install -m 0755 "$SCRIPT_DIR/bin/skillkorpctl" "/usr/share/$PKG_NAME/skillkorpctl"
    sudo cp -r "$SCRIPT_DIR/assets" "/usr/share/$PKG_NAME/"

    sudo ln -sf "/usr/share/$PKG_NAME/skillkorpctl" /usr/bin/skillkorpctl
    printf '#!/bin/sh\nexec env PYTHONPATH="/usr/share/%s:$PYTHONPATH" python3 -m skillkorp.gui "$@"\n' "$PKG_NAME" | sudo tee /usr/bin/skillkorp-gui >/dev/null
    printf '#!/bin/sh\nexec env PYTHONPATH="/usr/share/%s:$PYTHONPATH" python3 -m skillkorp.tray "$@"\n' "$PKG_NAME" | sudo tee /usr/bin/skillkorp-tray >/dev/null
    sudo chmod 0755 /usr/bin/skillkorp-gui /usr/bin/skillkorp-tray

    sudo install -m 0644 "$SCRIPT_DIR/udev/99-skillkorp-suite.rules" /usr/lib/udev/rules.d/99-skillkorp-suite.rules
    sudo install -m 0644 "$SCRIPT_DIR/io.github.skillkorp.suite.desktop" /usr/share/applications/io.github.skillkorp.suite.desktop
    sudo install -m 0644 "$SCRIPT_DIR/assets/skillkorp-suite.png" /usr/share/icons/hicolor/256x256/apps/skillkorp-suite.png

    sudo udevadm control --reload-rules 2>/dev/null || true
    sudo udevadm trigger --subsystem-match=hidraw 2>/dev/null || true
    sudo update-desktop-database /usr/share/applications 2>/dev/null || true
}

install_user_mode() {
    echo "👤 Installation en mode utilisateur (~/.local)..."
    mkdir -p "$HOME/.local/share/$PKG_NAME"
    mkdir -p "$HOME/.local/bin"
    mkdir -p "$HOME/.local/share/applications"
    mkdir -p "$HOME/.local/share/icons/hicolor/256x256/apps"

    cp -r "$SCRIPT_DIR/skillkorp" "$HOME/.local/share/$PKG_NAME/skillkorp"
    find "$HOME/.local/share/$PKG_NAME/skillkorp" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

    ln -sf "$SCRIPT_DIR/bin/skillkorpctl" "$HOME/.local/bin/skillkorpctl"
    printf '#!/bin/sh\nexec env PYTHONPATH="%s:$PYTHONPATH" python3 -m skillkorp.gui "$@"\n' "$HOME/.local/share/$PKG_NAME" > "$HOME/.local/bin/skillkorp-gui"
    printf '#!/bin/sh\nexec env PYTHONPATH="%s:$PYTHONPATH" python3 -m skillkorp.tray "$@"\n' "$HOME/.local/share/$PKG_NAME" > "$HOME/.local/bin/skillkorp-tray"
    chmod +x "$HOME/.local/bin/skillkorp-gui" "$HOME/.local/bin/skillkorp-tray"

    cp -f "$SCRIPT_DIR/assets/skillkorp-suite.png" "$HOME/.local/share/icons/hicolor/256x256/apps/skillkorp-suite.png"
    cp -f "$SCRIPT_DIR/io.github.skillkorp.suite.desktop" "$HOME/.local/share/applications/"
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

    echo ""
    echo "🔑 Pour que le clavier et la souris soient reconnus sans droits root, installation de la règle udev :"
    if [ "$EUID" -eq 0 ]; then
        cp -f "$SCRIPT_DIR/udev/99-skillkorp-suite.rules" /etc/udev/rules.d/
        udevadm control --reload-rules
        udevadm trigger --subsystem-match=hidraw
    else
        sudo cp -f "$SCRIPT_DIR/udev/99-skillkorp-suite.rules" /etc/udev/rules.d/
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=hidraw
    fi
}

uninstall() {
    echo "🗑️  Désinstallation de SkillKorp Suite..."

    if command -v dnf >/dev/null 2>&1 && rpm -q "$PKG_NAME" >/dev/null 2>&1; then
        sudo dnf remove -y "$PKG_NAME"
    elif command -v dpkg >/dev/null 2>&1 && dpkg -s "$PKG_NAME" >/dev/null 2>&1; then
        sudo apt-get remove -y "$PKG_NAME"
    elif command -v pacman >/dev/null 2>&1 && pacman -Q "$PKG_NAME" >/dev/null 2>&1; then
        sudo pacman -R "$PKG_NAME"
    fi

    sudo rm -rf "/usr/share/$PKG_NAME"
    sudo rm -f /usr/bin/skillkorpctl /usr/bin/skillkorp-gui /usr/bin/skillkorp-tray
    sudo rm -f /usr/lib/udev/rules.d/99-skillkorp-suite.rules /etc/udev/rules.d/99-skillkorp-suite.rules
    sudo rm -f /usr/share/applications/io.github.skillkorp.suite.desktop
    sudo rm -f /usr/share/icons/hicolor/256x256/apps/skillkorp-suite.png

    rm -rf "$HOME/.local/share/$PKG_NAME"
    rm -f "$HOME/.local/bin/skillkorpctl" "$HOME/.local/bin/skillkorp-gui" "$HOME/.local/bin/skillkorp-tray"
    rm -f "$HOME/.local/share/applications/io.github.skillkorp.suite.desktop"
    rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/skillkorp-suite.png"
    rm -f "$HOME/.config/autostart/io.github.skillkorp.suite.tray.desktop"

    sudo udevadm control --reload-rules 2>/dev/null || true
    echo "✓ Désinstallation terminée."
    echo "  (Les anciens dossiers ~/.config/skillkorp-k20, ~/.config/skillkorp-m20 et"
    echo "   ~/.config/skillkorp ne sont pas supprimés — effacez-les vous-même si besoin.)"
    exit 0
}

MODE="auto"
for arg in "$@"; do
    case "$arg" in
        --user) MODE="user" ;;
        --system) MODE="system" ;;
        --package) MODE="package" ;;
        --uninstall) uninstall ;;
        -h|--help)
            echo "Usage: ./install.sh [OPTION]"
            echo ""
            echo "Options :"
            echo "  (par défaut)  Détecte automatiquement votre distribution et installe via le gestionnaire de paquets"
            echo "  --package     Force l'installation via le paquet natif (.rpm ou .deb)"
            echo "  --system      Installe directement dans /usr (système complet)"
            echo "  --user        Installe dans ~/.local/bin et installe la règle udev"
            echo "  --uninstall   Désinstalle complètement SkillKorp Suite"
            echo "  --help        Affiche cette aide"
            exit 0
            ;;
    esac
done

echo "============================================================"
echo "   Installation de SkillKorp Suite (clavier K20 + souris M20)"
echo "============================================================"

detect_distro
echo "🔍 Distribution détectée : $DISTRO_ID (famille: $DISTRO_LIKE)"

if [ "$MODE" = "user" ]; then
    install_user_mode
elif [ "$MODE" = "system" ]; then
    install_system_direct
else
    INSTALLED_VIA_PKG=false

    if [ ! -d "$DIST_DIR" ] || [ -z "$(ls -A "$DIST_DIR" 2>/dev/null)" ]; then
        echo "📦 Construction préalable des paquets d'installation..."
        python3 "$SCRIPT_DIR/packaging/build_packages.py" || true
    fi

    if echo "$DISTRO_ID $DISTRO_LIKE" | grep -Eq 'fedora|rhel|centos|suse'; then
        RPM_PKG=$(find "$DIST_DIR" -name "${PKG_NAME}-*.rpm" 2>/dev/null | head -n 1)
        if [ -n "$RPM_PKG" ] && command -v dnf >/dev/null 2>&1; then
            echo "📦 Installation du paquet RPM via DNF..."
            sudo dnf install -y "$RPM_PKG"
            INSTALLED_VIA_PKG=true
        fi
    fi

    if [ "$INSTALLED_VIA_PKG" = false ] && echo "$DISTRO_ID $DISTRO_LIKE" | grep -Eq 'debian|ubuntu|mint|pop'; then
        DEB_PKG=$(find "$DIST_DIR" -name "${PKG_NAME}_*_all.deb" 2>/dev/null | head -n 1)
        if [ -n "$DEB_PKG" ] && command -v apt-get >/dev/null 2>&1; then
            echo "📦 Installation du paquet DEB via APT..."
            sudo apt-get install -y "$DEB_PKG"
            INSTALLED_VIA_PKG=true
        fi
    fi

    if [ "$INSTALLED_VIA_PKG" = false ] && echo "$DISTRO_ID $DISTRO_LIKE" | grep -Eq 'arch'; then
        if command -v makepkg >/dev/null 2>&1; then
            echo "📦 Construction et installation via makepkg..."
            (cd "$SCRIPT_DIR/packaging/arch" && makepkg -si --noconfirm)
            INSTALLED_VIA_PKG=true
        fi
    fi

    if [ "$INSTALLED_VIA_PKG" = false ]; then
        install_system_direct
    fi
fi

echo ""
echo "============================================================"
echo "✓ Installation terminée avec succès !"
echo "============================================================"
echo "Vous pouvez dès à présent utiliser :"
echo "  • skillkorpctl status   (dans un terminal, vue clavier + souris)"
echo "  • skillkorp-gui         (ou via l'icône dans la liste d'applications)"
echo "  • skillkorp-tray        (indicateur unique dans la barre des tâches / systray)"
echo "============================================================"
