#!/usr/bin/env python3
"""
kalay-probe.py — confirm a unit speaks ThroughTek/Kalay on your LAN.

Bitdefender's audit notes Kalay devices answer device-ID discovery on UDP 63616.
This is a heuristic LAN check: if your camera's IP responds on that port, it's
Kalay (matches the wyze-bridge family) and the native-bridge path is viable.

    python3 kalay-probe.py                 # broadcast probe, listen 5s
    python3 kalay-probe.py 192.168.1.50    # probe one IP

Note: the exact discovery payload differs across TUTK SDK versions. We send a
few benign probe variants and report ANY responder on 63616. A response (not the
specific bytes) is the signal. Run tcpdump alongside for ground truth:
    tcpdump -ni any udp port 63616
"""

import socket
import sys
import time

PORT = 63616

# Candidate discovery probes seen across TUTK LAN-search implementations.
# These are small "are you there" packets; the device replies with its info.
PROBES = [
    bytes.fromhex("f1411388"),          # common TUTK LAN-search header variant
    b"\x00\x00\x00\x00",                # null probe (some SDKs echo)
    b"TUTK_SEARCH",                     # plaintext fallback
]


def probe(target: str | None) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(0.5)
    try:
        s.bind(("", PORT))
    except OSError:
        # Port busy is fine; we can still send + recv on an ephemeral port.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.5)

    dest = target or "255.255.255.255"
    print(f"Probing {dest}:{PORT} (Kalay/TUTK discovery) ...")
    for p in PROBES:
        try:
            s.sendto(p, (dest, PORT))
        except OSError as e:
            print(f"  send error: {e}")

    responders: set[str] = set()
    end = time.time() + 5
    while time.time() < end:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        if addr[0] not in responders:
            responders.add(addr[0])
            print(f"  RESPONSE from {addr[0]}:{addr[1]}  ({len(data)} bytes)")
            print(f"    hex: {data[:48].hex()}")

    if responders:
        print(f"\n[+] {len(responders)} responder(s) on UDP {PORT} -> Kalay/TUTK confirmed.")
    else:
        print(f"\n[-] No responders on UDP {PORT}. Either not Kalay, blocked by VLAN/firewall,")
        print("    or the camera only does cloud P2P. Confirm via the mitmproxy capture instead.")


if __name__ == "__main__":
    probe(sys.argv[1] if len(sys.argv) > 1 else None)
