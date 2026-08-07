"""Generate small PNG/ICO derivatives from the deterministic RockCore mark."""

import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "branding"
ORANGE = (200, 86, 32, 255)
LIGHT = (243, 163, 75, 255)
DARK = (142, 53, 25, 255)
WHITE = (255, 250, 244, 255)


def polygon(points, width, height, color, pixels):
    min_x = max(0, int(min(x for x, _ in points)))
    max_x = min(width - 1, int(max(x for x, _ in points)))
    min_y = max(0, int(min(y for _, y in points)))
    max_y = min(height - 1, int(max(y for _, y in points)))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            inside = False
            j = len(points) - 1
            for i, (px, py) in enumerate(points):
                qx, qy = points[j]
                if ((py > y) != (qy > y)) and x < (qx - px) * (y - py) / (qy - py) + px:
                    inside = not inside
                j = i
            if inside:
                pixels[y][x] = color


def make_pixels(size):
    pixels = [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]
    s = size / 256
    def pts(values):
        return [(x * s, y * s) for x, y in values]
    polygon(pts([(128, 12), (226, 68), (226, 188), (128, 244), (30, 188), (30, 68)]), size, size, ORANGE, pixels)
    polygon(pts([(128, 12), (226, 68), (128, 125), (30, 68)]), size, size, LIGHT, pixels)
    polygon(pts([(30, 68), (128, 125), (128, 244), (30, 188)]), size, size, DARK, pixels)
    polygon(pts([(88, 77), (136, 77), (176, 108), (155, 137), (182, 179), (154, 179), (132, 142), (114, 142), (114, 179), (88, 179)]), size, size, WHITE, pixels)
    return pixels


def png_bytes(pixels):
    height, width = len(pixels), len(pixels[0])
    raw = b"".join(b"\0" + b"".join(bytes(pixel) for pixel in row) for row in pixels)
    def chunk(name, value):
        return struct.pack(">I", len(value)) + name + value + struct.pack(">I", zlib.crc32(name + value) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico_bytes(pixels):
    size = len(pixels)
    bgra = b"".join(bytes((b, g, r, a)) for row in reversed(pixels) for r, g, b, a in row)
    mask_row = b"\0" * ((size + 31) // 32 * 4)
    dib = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(bgra), 0, 0, 0, 0)
    dib += bgra + mask_row * size
    header = struct.pack("<HHH", 0, 1, 1)
    directory = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(dib), 22)
    return header + directory + dib


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "rockinnov_logo.png").write_bytes(png_bytes(make_pixels(256)))
    (OUT / "rockcore.ico").write_bytes(ico_bytes(make_pixels(256)))


if __name__ == "__main__":
    main()
