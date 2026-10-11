#!/bin/sh
# Shared by the deb postinst, the RPM %post and the Arch .INSTALL.
# 1.2: only the vendor interface stays open; drop the ACLs that older rules
# gave on the keystroke/pointer interfaces (they would last until replug).
for n in /sys/class/hidraw/hidraw*; do
    m=$(cat "$n/device/../modalias" 2>/dev/null) || continue
    case "$m" in
        usb:v3151p401[15]*in02|usb:v1D57pFA6[01]*in02) ;;
        usb:v3151p401[15]*|usb:v1D57pFA6[01]*)
            command -v setfacl >/dev/null 2>&1 && setfacl -b "/dev/${n##*/}" 2>/dev/null || true ;;
    esac
done
