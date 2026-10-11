#!/usr/bin/env python3
"""
Capture the HID configuration traffic of a USB device with the kernel usbmon
binary interface (/dev/usbmonN), e.g. while the vendor's Windows software
drives the device from a VM with USB pass-through.

    sudo modprobe usbmon
    sudo setfacl -m u:$USER:r /dev/usbmon7        # or run this script with sudo
    python3 tools/usbcapture.py --vid 3151 --pid 4011 -o capture.txt

Control transfers (SET_REPORT / GET_REPORT) are decoded with their data.
Interrupt transfers are only counted: they carry the keystrokes typed on the
keyboard, so their content is never logged, except for the vendor report IDs
explicitly requested with --report-id (e.g. 5 for the K20 receiver's
acknowledgements). Stop with Ctrl+C.
"""

import argparse
import ctypes
import fcntl
import glob
import os
import sys
import time

MON_IOC_MAGIC = 0x92


class MonBinHdr(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint64), ("type", ctypes.c_ubyte), ("xfer_type", ctypes.c_ubyte),
        ("epnum", ctypes.c_ubyte), ("devnum", ctypes.c_ubyte), ("busnum", ctypes.c_ushort),
        ("flag_setup", ctypes.c_char), ("flag_data", ctypes.c_char), ("ts_sec", ctypes.c_int64),
        ("ts_usec", ctypes.c_int32), ("status", ctypes.c_int32), ("len_urb", ctypes.c_uint32),
        ("len_cap", ctypes.c_uint32), ("setup", ctypes.c_ubyte * 8), ("interval", ctypes.c_int32),
        ("start_frame", ctypes.c_int32), ("xfer_flags", ctypes.c_uint32), ("ndesc", ctypes.c_uint32),
    ]


class MonGetArg(ctypes.Structure):
    _fields_ = [("hdr", ctypes.POINTER(MonBinHdr)), ("data", ctypes.c_void_p), ("alloc", ctypes.c_size_t)]


def _iow(nr, size):
    return (1 << 30) | (size << 16) | (MON_IOC_MAGIC << 8) | nr


MON_IOCX_GETX = _iow(10, ctypes.sizeof(MonGetArg))
XFER = {0: "ISO", 1: "INT", 2: "CTRL", 3: "BULK"}
REQ = {0x09: "SET_REPORT", 0x01: "GET_REPORT", 0x0A: "SET_IDLE", 0x0B: "SET_PROTOCOL", 0x06: "GET_DESCRIPTOR"}
RTYPE = {1: "Input", 2: "Output", 3: "Feature"}


def find_device(vid: str, pid: str):
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                v = f.read().strip()
            with open(os.path.join(d, "idProduct")) as f:
                p = f.read().strip()
            if v == vid.lower() and p == pid.lower():
                with open(os.path.join(d, "busnum")) as f:
                    bus = int(f.read())
                with open(os.path.join(d, "devnum")) as f:
                    dev = int(f.read())
                return bus, dev
        except OSError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vid", default="3151")
    ap.add_argument("--pid", default="4011")
    ap.add_argument("-o", "--output", help="also write the log to this file")
    ap.add_argument("--report-id", type=lambda v: int(v, 0), action="append", default=[],
                    help="log interrupt reports with this report ID (vendor reports only; "
                         "never use it for keyboard/consumer key reports)")
    args = ap.parse_args()

    found = find_device(args.vid, args.pid)
    if not found:
        print(f"Périphérique {args.vid}:{args.pid} introuvable.", file=sys.stderr)
        return 1
    bus, devnum = found
    path = f"/dev/usbmon{bus}"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        print(f"Impossible d'ouvrir {path} ({e}). Lancez : sudo modprobe usbmon && "
              f"sudo setfacl -m u:$USER:r {path}", file=sys.stderr)
        return 1
    out = open(args.output, "w") if args.output else None

    def log(line):
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    log(f"# capture {args.vid}:{args.pid} bus {bus} device {devnum} — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    hdr = MonBinHdr()
    buf = ctypes.create_string_buffer(4096)
    arg = MonGetArg(ctypes.pointer(hdr), ctypes.cast(buf, ctypes.c_void_p), len(buf))
    pending = {}
    int_count = 0
    try:
        while True:
            fcntl.ioctl(fd, MON_IOCX_GETX, arg)
            if hdr.devnum != devnum:
                continue
            data = buf.raw[:hdr.len_cap]
            ts = time.strftime("%H:%M:%S", time.localtime(hdr.ts_sec)) + f".{hdr.ts_usec // 1000:03d}"
            kind = chr(hdr.type)  # S submit, C complete, E error
            ep = hdr.epnum & 0x7F
            direction = "IN" if hdr.epnum & 0x80 else "OUT"
            if hdr.xfer_type == 2:
                if kind == "S" and hdr.flag_setup == b"\x00":
                    s = bytes(hdr.setup)
                    bm, breq = s[0], s[1]
                    w_value = s[2] | (s[3] << 8)
                    w_index = s[4] | (s[5] << 8)
                    w_len = s[6] | (s[7] << 8)
                    name = REQ.get(breq, f"req {breq:#04x}")
                    detail = ""
                    if breq in (0x09, 0x01) and bm & 0x60 == 0x20:
                        detail = f" {RTYPE.get(w_value >> 8, w_value >> 8)} id={w_value & 0xFF:#04x}"
                    line = f"{ts} {name}{detail} iface={w_index} len={w_len} bm={bm:#04x}"
                    if bm & 0x80 == 0:
                        line += f"\n        OUT: {data.hex(' ')}"
                    pending[hdr.id] = line
                elif kind in "CE":
                    line = pending.pop(hdr.id, f"{ts} CTRL completion")
                    status = "" if hdr.status == 0 else f" status={hdr.status}"
                    if data and hdr.epnum & 0x80:
                        line += f"\n        IN ({len(data)}): {data.hex(' ')}"
                    log(line + status)
            elif hdr.xfer_type == 1 and kind == "C" and data:
                int_count += 1
                # Endpoint 1 is the boot keyboard: never logged.
                if ep != 1 and data[0] in args.report_id:
                    log(f"{ts} INT ep{ep} {direction}: {data.hex(' ')}")
    except KeyboardInterrupt:
        pass
    finally:
        log(f"# fin — {int_count} rapports d'interruption (contenu non enregistré)")
        os.close(fd)
        if out:
            out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
