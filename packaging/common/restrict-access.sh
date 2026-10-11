#!/bin/sh
# Shared by the deb postinst, the RPM %post and the Arch .INSTALL.
# 1.2: only the vendor interface stays open; drop the ACLs that older rules
# gave on the keystroke/pointer interfaces (they would last until replug).
# The uaccess tag stays in the udev database ("sticky") until the device is
# removed, and logind re-applies the ACL from it: replay remove + add for
# those interfaces so the database matches the new rule, then drop the ACL.
stale=""
for n in /sys/class/hidraw/hidraw*; do
    m=$(cat "$n/device/../modalias" 2>/dev/null) || continue
    case "$m" in
        usb:v3151p401[15]*in02|usb:v1D57pFA6[01]*in02) ;;
        usb:v3151p401[15]*|usb:v1D57pFA6[01]*)
            if command -v udevadm >/dev/null 2>&1; then
                udevadm trigger --action=remove "$n" 2>/dev/null || true
                udevadm trigger --action=add "$n" 2>/dev/null || true
            fi
            stale="$stale /dev/${n##*/}" ;;
    esac
done
if [ -n "$stale" ]; then
    command -v udevadm >/dev/null 2>&1 && udevadm settle --timeout=10 2>/dev/null || true
    for d in $stale; do
        command -v setfacl >/dev/null 2>&1 && setfacl -b "$d" 2>/dev/null || true
    done
fi
