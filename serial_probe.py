import argparse
import os
import time
from pathlib import Path

import serial
from serial.tools import list_ports


def candidate_ports():
    candidates = []
    by_id = Path("/dev/serial/by-id")
    if by_id.exists():
        candidates.extend(str(path) for path in sorted(by_id.iterdir()))

    usb_ports = []
    other_ports = []
    for info in list_ports.comports():
        if not info.device:
            continue
        if info.device.startswith(("/dev/ttyACM", "/dev/ttyUSB")):
            usb_ports.append(info.device)
        else:
            other_ports.append(info.device)

    candidates.extend(sorted(usb_ports))
    seen = set()
    return [port for port in candidates + sorted(other_ports) if not (port in seen or seen.add(port))]


def print_port_details():
    print("Detected serial ports:")
    ports = candidate_ports()
    if not ports:
        print("  none")
        return

    info_by_device = {info.device: info for info in list_ports.comports() if info.device}
    for port in ports:
        path = Path(port)
        info = info_by_device.get(port)
        resolved = ""
        try:
            resolved = str(path.resolve()) if path.exists() or path.is_symlink() else ""
        except Exception:
            resolved = ""
        access = f"exists={path.exists()} readable={os.access(port, os.R_OK) if path.exists() else False} writable={os.access(port, os.W_OK) if path.exists() else False}"
        meta = ""
        if info:
            meta = f" description={info.description!r} hwid={info.hwid!r} manufacturer={info.manufacturer!r}"
        print(f"  {port} -> {resolved or '-'} {access}{meta}")


def resolve_port(configured_port, baud, timeout):
    if configured_port.lower() != "auto":
        return configured_port

    ports = candidate_ports()
    print("Candidate ports:", ", ".join(ports) if ports else "none")
    for port in ports:
        try:
            with serial.Serial(port, baudrate=baud, timeout=timeout):
                print(f"Auto-selected: {port}")
                return port
        except serial.SerialException as exc:
            print(f"Skipping {port}: {exc}")

    raise SystemExit("No usable serial port found.")


def main():
    parser = argparse.ArgumentParser(description="Watch raw LoRa serial lines for troubleshooting.")
    parser.add_argument("--port", default="auto")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()

    print_port_details()
    port = resolve_port(args.port, args.baud, args.timeout)
    deadline = time.time() + args.seconds
    print(f"Opening {port} @ {args.baud}. Watching for {args.seconds}s...")

    with serial.Serial(
        port=port,
        baudrate=args.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=args.timeout,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    ) as ser:
        count = 0
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            count += 1
            print(raw.decode("utf-8", errors="replace").rstrip())

    print(f"Done. Lines received: {count}")


if __name__ == "__main__":
    main()
