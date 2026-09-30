"""Bounded IP-header capture inside an already selected Linux network namespace.

Run via nsenter on the strict workload's network namespace, never the host namespace.
Only loopback interfaces are accepted. Payloads and transport headers are not saved.
This diagnostic is separate from the application and requires CAP_NET_RAW.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import socket
import struct
import time
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=45, choices=range(1, 61))
    args = parser.parse_args()
    interfaces = socket.if_nameindex()
    if not interfaces or any(name != "lo" for _, name in interfaces):
        raise SystemExit("Refusing capture: select a loopback-only workload namespace")
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = args.output / f"namespace-headers-{stamp}.pcap"
    summary_path = path.with_suffix(".json")
    packets = external = omitted = 0
    protocols: dict[str, int] = {}
    start_utc = datetime.now(UTC).isoformat()
    started = time.monotonic()
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as capture:
        capture.settimeout(0.25)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            control.bind(("127.0.0.1", 0))
            control.sendto(b"blackbox-capture-positive-control", control.getsockname())
            control.recvfrom(128)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            error = probe.connect_ex(("203.0.113.1", 443))
        with path.open("xb") as output:
            output.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 96, 1))
            while time.monotonic() - started < args.seconds:
                try:
                    frame, _ = capture.recvfrom(65535)
                except TimeoutError:
                    continue
                if len(frame) < 34:
                    omitted += 1
                    continue
                ether_type = struct.unpack("!H", frame[12:14])[0]
                if ether_type == 0x0800:
                    header_size = 14 + (frame[14] & 15) * 4
                    addresses = [
                        ipaddress.ip_address(frame[26:30]),
                        ipaddress.ip_address(frame[30:34]),
                    ]
                    protocol = "IPv4"
                elif ether_type == 0x86DD and len(frame) >= 54:
                    header_size = 54
                    addresses = [
                        ipaddress.ip_address(frame[22:38]),
                        ipaddress.ip_address(frame[38:54]),
                    ]
                    protocol = "IPv6"
                else:
                    omitted += 1
                    continue
                header = frame[:header_size]
                now = time.time()
                output.write(
                    struct.pack(
                        "<IIII", int(now), int((now % 1) * 1_000_000), len(header), len(frame)
                    )
                )
                output.write(header)
                packets += 1
                external += int(any(not addr.is_loopback for addr in addresses))
                protocols[protocol] = protocols.get(protocol, 0) + 1
            received, dropped = struct.unpack("II", capture.getsockopt(263, 6, 8))
    summary = {
        "started_utc": start_utc,
        "ended_utc": datetime.now(UTC).isoformat(),
        "observed_seconds": round(time.monotonic() - started, 3),
        "network_namespace": os.readlink("/proc/self/ns/net"),
        "interfaces": interfaces,
        "pcap": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "captured_ip_packets": packets,
        "non_loopback_ip_packets": external,
        "omitted_non_ip_or_short_frames": omitted,
        "ip_protocol_counts": protocols,
        "kernel_received": received,
        "kernel_dropped": dropped,
        "native_outbound_probe": {
            "destination": "203.0.113.1:443",
            "errno": error,
            "blocked": error != 0,
        },
        "loopback_positive_control": True,
        "limitations": [
            "Observed namespace and time window only; not host-wide or permanent certification.",
            "IP headers only; transport headers and payloads omitted intentionally.",
            "Packet counts include the synthetic loopback positive control and may include duplicates.",
            "No claim about other namespaces, GPU deployment, filesystem channels or administrator compromise.",
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
