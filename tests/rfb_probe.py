#!/usr/bin/env python3
"""Check the VNC listener and a captured Xvfb framebuffer through RFB 3.8."""

import argparse
import errno
import socket
import struct
import sys
import time


BANNER = b"RFB 003.008\n"
MAX_DIMENSION = 4096
MAX_DESKTOP_NAME = 1024
MAX_UPDATE_BYTES = 128 << 20
FRAME_TIMEOUT = 10


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def receive(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise AssertionError(f"RFB peer closed with {count - len(data)} bytes missing")
        data.extend(part)
    return bytes(data)


def expect_no_listener(address, family):
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect(address)
        except OSError as error:
            if error.errno == errno.ECONNREFUSED:
                return True
            if error.errno in (errno.EADDRNOTAVAIL, errno.ENETUNREACH,
                               errno.EHOSTUNREACH):
                return False  # No usable route; isolation is inconclusive.
            raise AssertionError(f"Could not test isolation at {address}: {error}") from error
        raise AssertionError(f"VNC listener accepts connections at {address}")


def check_loopback_only(port):
    if socket.has_ipv6:
        expect_no_listener(("::1", port), socket.AF_INET6)

    # A UDP connect chooses the route's local source address without sending a
    # packet. Do not rely on hostname resolution in hosted runners.
    addresses = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("192.0.2.1", 9))
            addresses.add(route.getsockname()[0])
    except OSError:
        pass
    addresses = {address for address in addresses
                 if not address.startswith("127.") and address != "0.0.0.0"}
    return sum(expect_no_listener((address, port), socket.AF_INET)
               for address in addresses)


class RFB:
    def __init__(self, sock):
        self.sock = sock
        require(receive(sock, 12) == BANNER, "Expected RFB 3.8 banner")
        sock.sendall(BANNER)
        count = receive(sock, 1)[0]
        require(count > 0, "Server rejected security negotiation")
        security = receive(sock, count)
        require(1 in security, f"None security type missing: {security!r}")
        sock.sendall(b"\x01")
        require(struct.unpack("!I", receive(sock, 4))[0] == 0,
                "None authentication failed")
        sock.sendall(b"\x01")  # Shared desktop request.

        header = receive(sock, 24)
        self.width, self.height = struct.unpack("!HH", header[:4])
        require(0 < self.width <= MAX_DIMENSION and
                0 < self.height <= MAX_DIMENSION,
                f"Invalid framebuffer size {self.width}x{self.height}")
        pixel_format = struct.unpack("!BBBBHHHBBB3x", header[4:20])
        # LibVNCServer serializes trueColour as either 1 or 255. Both are true
        # under the RFB boolean convention.
        normalized = pixel_format[:3] + (bool(pixel_format[3]),) + pixel_format[4:]
        require(normalized == (32, 24, 0, True, 255, 255, 255, 16, 8, 0),
                f"Unexpected pixel format {pixel_format}")
        name_length = struct.unpack("!I", header[20:])[0]
        require(0 < name_length <= MAX_DESKTOP_NAME,
                f"Invalid desktop name length {name_length}")
        self.name = receive(sock, name_length).decode("utf-8")
        self.pixels = bytearray(self.width * self.height * 4)
        self.raw_rectangles = 0
        self.updated_points = set()

        # Raw pixels plus resize/cursor notifications. Separating the cursor
        # avoids contaminating the Xvfb color sample with pointer artwork.
        encodings = (0, -223, -239, -232)
        sock.sendall(struct.pack("!BBH4i", 2, 0, len(encodings), *encodings))

    def sample_points(self):
        return {(self.width // 4, self.height // 4),
                (3 * self.width // 4, self.height // 4),
                (self.width // 4, 3 * self.height // 4),
                (3 * self.width // 4, 3 * self.height // 4)}

    def request(self):
        self.sock.sendall(struct.pack("!BBHHHH", 3, 0, 0, 0,
                                      self.width, self.height))

    def update(self):
        message = receive(self.sock, 1)[0]
        require(message == 0, f"Unexpected RFB server message type {message}")
        _, count = struct.unpack("!BH", receive(self.sock, 3))
        require(count <= 1024, f"Too many framebuffer rectangles: {count}")
        remaining_bytes = MAX_UPDATE_BYTES
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack("!HHHHi", receive(self.sock, 12))
            if encoding == -223:  # DesktopSize.
                require(0 < width <= MAX_DIMENSION and
                        0 < height <= MAX_DIMENSION, "Invalid resize")
                self.width, self.height = width, height
                self.pixels = bytearray(width * height * 4)
                self.updated_points.clear()
            elif encoding == -239:  # XCursor.
                require(width <= 512 and height <= 512, "Cursor rectangle too large")
                size = width * height * 4 + ((width + 7) // 8) * height
                require(size <= remaining_bytes, "Framebuffer update too large")
                remaining_bytes -= size
                receive(self.sock, size)
            elif encoding == -232:  # PointerPos, no payload.
                pass
            else:
                require(encoding == 0, f"Unexpected encoding {encoding}")
                require(width > 0 and height > 0 and
                        x + width <= self.width and y + height <= self.height,
                        "Raw rectangle exceeds framebuffer")
                size = width * height * 4
                require(size <= remaining_bytes, "Framebuffer update too large")
                remaining_bytes -= size
                pixels = receive(self.sock, size)
                for row in range(height):
                    destination = ((y + row) * self.width + x) * 4
                    source = row * width * 4
                    self.pixels[destination:destination + width * 4] = \
                        pixels[source:source + width * 4]
                self.raw_rectangles += 1
                self.updated_points.update((sx, sy) for sx, sy in self.sample_points()
                                           if x <= sx < x + width and y <= sy < y + height)

    def verify_frame(self, expect_rgb):
        expected_bgr = bytes(reversed(expect_rgb)) if expect_rgb else None
        deadline = time.monotonic() + FRAME_TIMEOUT
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.1, min(3, deadline - time.monotonic())))
            self.request()
            self.update()
            if self.raw_rectangles == 0:
                continue
            if expected_bgr is None:
                return
            for x, y in self.updated_points:
                offset = (y * self.width + x) * 4
                if self.pixels[offset:offset + 3] == expected_bgr:
                    return
        if self.raw_rectangles == 0:
            raise AssertionError("No raw framebuffer update received")
        raise AssertionError(f"Raw framebuffer did not contain RGB #{expect_rgb.hex()}")


def parse_rgb(value):
    value = value.removeprefix("#")
    if len(value) != 6:
        raise argparse.ArgumentTypeError("expected six hexadecimal RGB digits")
    try:
        return bytes.fromhex(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected six hexadecimal RGB digits") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expect-rgb", type=parse_rgb,
                        help="Expected color of the Xvfb root screen, as RRGGBB")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    interfaces = check_loopback_only(args.port)
    with socket.create_connection(("127.0.0.1", args.port), timeout=3) as sock:
        sock.settimeout(3)
        client = RFB(sock)
        client.verify_frame(args.expect_rgb)
        print(f"RFB 3.8, None security, {client.width}x{client.height} raw framebuffer, "
              f"{interfaces} non-loopback interface(s) checked", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, UnicodeDecodeError, struct.error) as error:
        print(f"RFB probe failed: {error}", file=sys.stderr)
        sys.exit(1)
