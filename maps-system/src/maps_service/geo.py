import math
import struct
import zlib

EARTH = 6371008.8
MAX_LAT = 85.0511287798066


def coordinates(lat, lon):
    try:
        valid = (
            type(lat) in (float, int)
            and type(lon) in (float, int)
            and math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid WGS84 coordinates")
    return float(lat), float(lon)


def distance(a, b):
    lat, lon = coordinates(*a)
    other_lat, other_lon = coordinates(*b)
    p, q = math.radians(lat), math.radians(other_lat)
    delta = math.radians(math.remainder(other_lon - lon, 360))
    sin_p, cos_p, sin_q, cos_q = math.sin(p), math.cos(p), math.sin(q), math.cos(q)
    sine = math.hypot(
        cos_q * math.sin(delta),
        cos_p * sin_q - sin_p * cos_q * math.cos(delta),
    )
    cosine = sin_p * sin_q + cos_p * cos_q * math.cos(delta)
    return EARTH * math.atan2(sine, cosine)


def bearing(a, b):
    """Initial spherical heading clockwise from north; None for coincident points."""
    lat, lon = coordinates(*a)
    other_lat, other_lon = coordinates(*b)
    if distance((lat, lon), (other_lat, other_lon)) < 1e-6:
        return None
    p, q = math.radians(lat), math.radians(other_lat)
    delta = math.radians(math.remainder(other_lon - lon, 360))
    return (
        math.degrees(
            math.atan2(
                math.sin(delta) * math.cos(q),
                math.cos(p) * math.sin(q) - math.sin(p) * math.cos(q) * math.cos(delta),
            )
        )
        % 360
    )


def project(lat, lon):
    lat, lon = coordinates(lat, lon)
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2
    return (lon + 180) / 360, max(0.0, min(1.0, y))


def unproject(x, y):
    if (
        type(x) not in (int, float)
        or type(y) not in (int, float)
        or not 0 <= x <= 1
        or not 0 <= y <= 1
    ):
        raise ValueError("normalized projection coordinates must be in [0,1]")
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y)))), x * 360 - 180


def tile(z, x, y):
    if (
        any(type(v) is not int for v in (z, x, y))
        or not 0 <= z <= 21
        or not 0 <= y < 2**z
    ):
        raise ValueError("invalid XYZ tile")
    return z, x % (2**z), y


def cell(lat, lon, z=16):
    if type(z) is not int or not 0 <= z <= 21:
        raise ValueError("invalid cell zoom")
    x, y = project(lat, lon)
    return min(2**z - 1, max(0, int(x * 2**z))), min(2**z - 1, max(0, int(y * 2**z)))


def boxes(a, b):
    a, b = coordinates(*a), coordinates(*b)
    south, north = sorted((a[0], b[0]))
    west, east = sorted((a[1], b[1]))
    if east - west > 180:
        return [(east, 180, south, north), (-180, west, south, north)]
    return [(west, east, south, north)]


def nearby_boxes(lat, lon, meters):
    lat, lon = coordinates(lat, lon)
    if type(meters) not in (int, float) or not 0 <= meters <= math.pi * EARTH:
        raise ValueError("invalid spherical radius")
    delta = math.degrees(meters / EARTH)
    south, north = max(-90, lat - delta), min(90, lat + delta)
    if south == -90 or north == 90:
        return [(-180, 180, south, north)]
    width = math.degrees(
        math.asin(min(1, math.sin(meters / EARTH) / math.cos(math.radians(lat))))
    )
    west, east = lon - width, lon + width
    if west < -180:
        return [(west + 360, 180, south, north), (-180, east, south, north)]
    if east > 180:
        return [(west, 180, south, north), (-180, east - 360, south, north)]
    return [(west, east, south, north)]


def match_point(point, a, b):
    # Local tangent approximation. Long segments and poles require a production matcher.
    lat, lon = coordinates(*point)
    a, b = coordinates(*a), coordinates(*b)
    scale = max(1e-10, math.cos(math.radians(lat)))

    def xy(p):
        return math.radians(
            (p[1] - lon + 180) % 360 - 180
        ) * EARTH * scale, math.radians(p[0] - lat) * EARTH

    ax, ay = xy(a)
    _, by = xy(b)
    # Unwrap the segment relative to its first endpoint, not independently
    # around the query: a dateline road must never pass through Greenwich.
    bx = ax + math.radians(math.remainder(b[1] - a[1], 360)) * EARTH * scale
    dx, dy = bx - ax, by - ay
    fraction = (
        max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        if dx * dx + dy * dy
        else 0
    )
    return (
        math.hypot(ax + fraction * dx, ay + fraction * dy),
        fraction,
        (math.degrees(math.atan2(dx, dy)) + 360) % 360,
    )


def clipped_segments(a, b, z, x, y):
    _, x, y = tile(z, x, y)
    n = 2**z
    ax, ay = project(*a)
    bx, by = project(*b)
    if bx - ax > 0.5:
        bx -= 1
    elif ax - bx > 0.5:
        bx += 1
    output = []
    seen = set()
    for shift in (-1, 0, 1):
        x0, y0 = (ax + shift) * n - x, ay * n - y
        x1, y1 = (bx + shift) * n - x, by * n - y
        dx, dy = x1 - x0, y1 - y0
        lo, hi = 0.0, 1.0
        valid = True
        for p, q in ((-dx, x0), (dx, 1 - x0), (-dy, y0), (dy, 1 - y0)):
            if abs(p) < 1e-15:
                if q < 0:
                    valid = False
                    break
            else:
                ratio = q / p
                if p < 0:
                    lo = max(lo, ratio)
                else:
                    hi = min(hi, ratio)
                if lo > hi:
                    valid = False
                    break
        if valid:
            segment = tuple(
                (
                    max(0.0, min(1.0, x0 + bound * dx)),
                    max(0.0, min(1.0, y0 + bound * dy)),
                )
                for bound in (lo, hi)
            )
            if segment not in seen:
                seen.add(segment)
                output.append(segment)
    return output


def png(segments):
    width = height = 256
    pixels = bytearray(bytes((242, 240, 231)) * width * height)
    for a, b in segments:
        x0, y0 = (max(0, min(255, round(v * 255))) for v in a)
        x1, y1 = (max(0, min(255, round(v * 255))) for v in b)
        count = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(count + 1):
            px, py = (
                round(x0 + (x1 - x0) * i / count),
                round(y0 + (y1 - y0) * i / count),
            )
            for xx in range(max(0, px - 1), min(255, px + 1) + 1):
                for yy in range(max(0, py - 1), min(255, py + 1) + 1):
                    start = (yy * width + xx) * 3
                    pixels[start : start + 3] = bytes((80, 95, 110))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(
        b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3]
        for row in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
