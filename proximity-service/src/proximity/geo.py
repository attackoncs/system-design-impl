import math

ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"
EARTH = 6371008.8
RADII = {500: 6, 1000: 5, 2000: 5, 5000: 4, 20000: 4}


def coordinates(latitude, longitude):
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        raise ValueError("coordinates must be numbers")
    lat, lon = float(latitude), float(longitude)
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("coordinates outside valid range")
    return lat, lon


def encode(lat, lon, precision=6):
    lat, lon = coordinates(lat, lon)
    if not 1 <= precision <= 12:
        raise ValueError("precision must be 1..12")
    bounds = [[-180.0, 180.0], [-90.0, 90.0]]
    bits = []
    for i in range(precision * 5):
        axis = i % 2
        value = lon if axis == 0 else lat
        mid = sum(bounds[axis]) / 2
        bit = int(value >= mid)
        bits.append(bit)
        bounds[axis][1 - bit] = mid
    return "".join(ALPHABET[sum(bits[i + j] << (4-j) for j in range(5))] for i in range(0, len(bits), 5))


def bounds(code):
    ranges = [[-180.0, 180.0], [-90.0, 90.0]]
    i = 0
    for char in code:
        number = ALPHABET.index(char)
        for shift in range(4, -1, -1):
            axis = i % 2
            bit = (number >> shift) & 1
            ranges[axis][1-bit] = sum(ranges[axis]) / 2
            i += 1
    return ranges[1][0], ranges[1][1], ranges[0][0], ranges[0][1]


def distance(lat, lon, other_lat, other_lon):
    p, q = math.radians(lat), math.radians(other_lat)
    a = math.sin((q-p)/2)**2 + math.cos(p)*math.cos(q)*math.sin(math.radians(other_lon-lon)/2)**2
    return 2 * EARTH * math.asin(math.sqrt(max(0, min(1, a))))


def cover(lat, lon, radius, max_cells=4096):
    lat, lon = coordinates(lat, lon)
    if radius not in RADII or isinstance(radius, bool):
        raise ValueError("radius must be 500,1000,2000,5000,20000 meters")
    angular = radius / EARTH
    delta = math.degrees(angular)
    south, north = max(-90, lat-delta), min(90, lat+delta)
    if south <= -90 or north >= 90:
        intervals = [(-180, 180)]
    else:
        width = math.degrees(math.asin(min(1, math.sin(angular)/math.cos(math.radians(lat)))))
        west, east = lon-width, lon+width
        intervals = ([(west+360, 180), (-180, east)] if west < -180 else
                     [(west, 180), (-180, east-360)] if east > 180 else [(west, east)])
    for precision in range(RADII[radius], 0, -1):
        lon_bits, lat_bits = (5*precision+1)//2, (5*precision)//2
        nx, ny = 2**lon_bits, 2**lat_bits
        dx, dy = 360/nx, 180/ny
        # Include touching cells with a small margin so exact grid boundaries cannot omit candidates.
        y0 = max(0, math.floor((south+90)/dy - 1e-10))
        y1 = min(ny-1, math.floor((north+90)/dy + 1e-10))
        ranges = [(max(0, math.floor((w+180)/dx-1e-10)), min(nx-1, math.floor((e+180)/dx+1e-10))) for w,e in intervals]
        count = (y1-y0+1)*sum(b-a+1 for a,b in ranges)
        if count <= max_cells:
            cells = {encode(-90+(y+0.5)*dy, -180+(x+0.5)*dx, precision)
                     for a,b in ranges for x in range(a,b+1) for y in range(y0,y1+1)}
            return precision, sorted(cells)
    raise ValueError("cell budget too small")
