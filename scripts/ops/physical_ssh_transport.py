"""Windows byte transport for SSH; bind this connection to a physical adapter.

OpenSSH still owns authentication and pinned host verification. No system route,
proxy configuration, credentials, or remote state is changed here.
"""
from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import socket
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable


@dataclass(frozen=True)
class DirectRoute:
    index: int
    address: str
    metric: int


def select_route(rows: list[dict[str, Any]]) -> DirectRoute:
    candidates = []
    for row in rows:
        if row.get("physical") is not True:
            continue
        try:
            index, metric = int(row["index"]), int(row["metric"])
            address = ipaddress.IPv4Address(row["address"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 < index < 2**32 or metric < 0:
            continue
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            continue
        candidates.append(DirectRoute(index, str(address), metric))
    if not candidates:
        raise OSError("no_physical_ipv4_default_route")
    return min(candidates, key=lambda r: (r.metric, r.index, r.address))


def discover_route() -> DirectRoute:
    # Use current addresses and metrics: Wi-Fi/DHCP changes must not reuse a
    # hard-coded address or interface index from an earlier successful retry.
    script = r"""$ErrorActionPreference = 'Stop'
$rows = foreach ($adapter in @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up')) {
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue |
        Where-Object NextHop -ne '0.0.0.0' | Sort-Object RouteMetric | Select-Object -First 1
    if ($null -eq $route) { continue }
    $address = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue |
        Where-Object { $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource -and $_.IPAddress -notlike '169.254.*' } |
        Sort-Object IPAddress | Select-Object -First 1
    if ($null -eq $address) { continue }
    $ipif = Get-NetIPInterface -AddressFamily IPv4 -InterfaceIndex $adapter.ifIndex
    [pscustomobject]@{index=[int]$adapter.ifIndex;address=$address.IPAddress;metric=([int]$route.RouteMetric+[int]$ipif.InterfaceMetric);physical=$true}
}
ConvertTo-Json -InputObject @($rows) -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False, capture_output=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[-400:]
        raise OSError("physical_route_inventory_failed:" + detail)
    rows = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(rows, list):
        raise OSError("invalid_physical_route_inventory")
    return select_route(rows)


def connect_direct(
    route: DirectRoute, host: str, port: int, *, factory: Callable[..., Any] = socket.socket,
) -> socket.socket:
    conn = factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows IP_UNICAST_IF expects the index in network byte order.
        conn.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", route.index))
        conn.bind((route.address, 0))
        conn.settimeout(12)
        conn.connect((host, port))
        conn.settimeout(60)
        return conn
    except BaseException:
        conn.close()
        raise


def relay(conn: socket.socket, source: BinaryIO, target: BinaryIO) -> None:
    errors: list[OSError] = []

    def outgoing() -> None:
        try:
            while True:
                data = source.read(65536)
                if not data:
                    conn.shutdown(socket.SHUT_WR)
                    return
                conn.sendall(data)
        except OSError as exc:
            errors.append(exc)
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    worker = threading.Thread(target=outgoing, daemon=True)
    worker.start()
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                break
            remaining = memoryview(data)
            while remaining:
                try:
                    count = target.write(remaining)
                except OSError as exc:
                    # The SSH client can close its pipe after a valid exit.
                    # Only a closed output pipe is benign, not socket errors.
                    if exc.errno in (errno.EPIPE, errno.EINVAL):
                        return
                    raise
                if not count:
                    raise OSError("ssh_output_pipe_write_failed")
                remaining = remaining[count:]
    finally:
        worker.join(timeout=0.1)
    if errors:
        raise errors[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", type=ipaddress.IPv4Address)
    parser.add_argument("port", type=int)
    args = parser.parse_args(argv)
    if sys.platform != "win32" or not 0 < args.port <= 65535:
        parser.error("Windows and a valid IPv4 destination port are required")
    try:
        route = discover_route()
        with connect_direct(route, str(args.host), args.port) as conn:
            # Unbuffered binary streams avoid text conversion and a buffered
            # stdout flush error when the parent SSH process closes the pipe.
            relay(conn, sys.stdin.buffer.raw, sys.stdout.buffer.raw)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"physical_ssh_transport:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
