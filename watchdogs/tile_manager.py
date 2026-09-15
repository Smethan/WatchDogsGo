"""OSM tile downloader, converter & renderer for ESP32 Watch Dogs.

Downloads CartoDB Dark Matter raster tiles for a given GPS area,
converts them to pyxel's 16-color palette, and renders them
as a dark retro-styled offline map layer.
"""

import json
import math
import time
import zlib
from collections import OrderedDict
from pathlib import Path
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Pyxel default palette (RGB tuples)
# ---------------------------------------------------------------------------
PYXEL_PALETTE = [
    (0x00, 0x00, 0x00),  # 0  black
    (0x2B, 0x33, 0x5F),  # 1  dark blue
    (0x7E, 0x20, 0x72),  # 2  purple
    (0x19, 0x95, 0x9C),  # 3  teal
    (0x8B, 0x48, 0x52),  # 4  brown
    (0x39, 0x5C, 0x98),  # 5  blue
    (0xA9, 0xC1, 0xFF),  # 6  light blue
    (0xEE, 0xEE, 0xEE),  # 7  white
    (0xD4, 0x18, 0x6C),  # 8  pink/red
    (0xD3, 0x84, 0x41),  # 9  orange
    (0xE9, 0xC3, 0x5B),  # 10 yellow
    (0x70, 0xC6, 0xA9),  # 11 green
    (0x76, 0x96, 0xDE),  # 12 medium blue
    (0xA3, 0xA3, 0xA3),  # 13 gray
    (0xFF, 0x97, 0x98),  # 14 light pink
    (0xED, 0xB4, 0xA1),  # 15 peach
]

# Game zoom → OSM tile zoom mapping (limited for RPi performance)
GAME_TO_OSM = {
    7: 12, 8: 12, 9: 13, 10: 13,
    11: 14, 12: 15, 13: 16,
}

# Download tiers: fewer zoom levels = less disk, faster rendering
DOWNLOAD_TIERS = [
    (12, 10),    # z12: 10km radius (~6 tiles)
    (13, 5),     # z13: 5km  radius (~12 tiles)
    (15, 1.5),   # street detail near the download center
    (16, 0.75),  # close-up detail, bounded area and request count
    (14, 3),     # z14: 3km  radius (~24 tiles)
]

OSM_TILE_SIZE = 256
USER_AGENT = "WatchDogsGo/1.0 (security-research-game)"
_UNPACK_NIBBLES = tuple(bytes((value >> 4, value & 0x0F))
                        for value in range(256))

# Stadia Maps — Alidade Smooth Dark style
# Free tier, raster PNG tiles, dark theme matching game aesthetic
# API key loaded from secrets.conf (STADIA_API_KEY=...)
# Sign up free at https://stadiamaps.com
_STADIA_BASE = "https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}@2x.png"


def _load_stadia_key() -> str:
    """Load Stadia Maps API key from secrets.conf."""
    try:
        from pathlib import Path as _Path
        conf = _Path(__file__).resolve().parent.parent / "secrets.conf"
        if conf.is_file():
            for line in conf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("STADIA_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _tile_url(z: int, x: int, y: int) -> str:
    """Build Stadia Maps tile URL with API key if configured."""
    url = _STADIA_BASE.format(z=z, x=x, y=y)
    key = _load_stadia_key()
    if key:
        url += f"?api_key={key}"
    return url


# Legacy constant kept for compatibility
TILE_URL = _STADIA_BASE


# ---------------------------------------------------------------------------
# Tile coordinate math
# ---------------------------------------------------------------------------

def _lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to OSM tile x, y at given zoom."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad))
             / math.pi) / 2.0 * n)
    return max(0, min(x, n - 1)), max(0, min(y, n - 1))


def _tile_to_lat_lon(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Convert tile x, y (top-left corner) to lat/lon."""
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def _tiles_in_radius(lat: float, lon: float, radius_km: float,
                     zoom: int) -> list[tuple[int, int]]:
    """Return list of (x, y) tile coords covering radius around lat/lon."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    x_min, y_max = _lat_lon_to_tile(lat - dlat, lon - dlon, zoom)
    x_max, y_min = _lat_lon_to_tile(lat + dlat, lon + dlon, zoom)
    tiles = []
    for tx in range(x_min, x_max + 1):
        for ty in range(y_min, y_max + 1):
            tiles.append((tx, ty))
    return tiles


# ---------------------------------------------------------------------------
# PNG → 16-color conversion (dark tile optimized)
# ---------------------------------------------------------------------------

def _nearest_palette_index(r: int, g: int, b: int) -> int:
    """Find nearest pyxel palette color (Euclidean distance in RGB)."""
    best_i, best_d = 0, 999999
    for i, (pr, pg, pb) in enumerate(PYXEL_PALETTE):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def _dark_tile_to_palette(r: int, g: int, b: int) -> int:
    """Map CartoDB Dark Matter pixel directly to pyxel palette index.

    CartoDB Dark Matter color ranges:
    - Background:  ~(38,38,38) very dark gray
    - Buildings:   ~(28,28,30) slightly darker than bg
    - Minor roads: ~(52,52,52) subtle gray
    - Major roads: ~(70,70,70) lighter gray
    - Labels:      ~(180-255) white/near-white
    - Water:       has blue tint (saturation > 20)
    - Parks:       has green tint

    We map these to specific palette colors for the dark theme look:
    - 0 (black):     tile background — OPAQUE, covers water
    - 1 (dark blue): buildings — subtle contrast on black
    - 5 (blue):      minor roads/boundaries
    - 13 (gray):     major roads
    - 7 (white):     labels and bright features
    - 3 (teal):      colored features (water edges, etc)
    """
    brightness = (r + g + b) // 3
    max_c = max(r, g, b)
    min_c = min(r, g, b)
    saturation = max_c - min_c

    # Colored pixels (water, parks, transit lines, etc.)
    if saturation > 20:
        if b > r and b > g:
            return 5   # blue features → palette blue
        if g > r and g > b:
            return 3   # green features → teal
        if r > g and r > b:
            return 4   # red/brown features → brown
        return 3       # fallback teal

    # Grayscale pixels — classify by brightness
    if brightness < 35:
        return 0       # background → black (OPAQUE)
    if brightness < 48:
        return 1       # buildings/dark features → dark blue
    if brightness < 72:
        return 5       # minor roads → blue
    if brightness < 120:
        return 13      # major roads → gray
    if brightness < 180:
        return 13      # road labels/features → gray
    return 7           # bright labels → white


def _convert_tile_pil(png_bytes: bytes) -> bytes:
    """Convert PNG bytes to 4-bit indexed array.

    Uses Pillow for PNG decoding. Each byte stores 2 pixels (high nibble first).
    Tiles are 512x512 (@2x retina) downscaled to 256x256 for storage.
    """
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    # @2x tiles are 512x512 — downscale to 256x256 with LANCZOS for quality
    if img.size != (OSM_TILE_SIZE, OSM_TILE_SIZE):
        img = img.resize((OSM_TILE_SIZE, OSM_TILE_SIZE), Image.LANCZOS)

    pixels = img.load()
    lut: dict[tuple[int, int, int], int] = {}
    out = bytearray(OSM_TILE_SIZE * OSM_TILE_SIZE // 2)
    idx = 0
    for y in range(OSM_TILE_SIZE):
        for x in range(0, OSM_TILE_SIZE, 2):
            k0 = pixels[x, y]
            k1 = pixels[x + 1, y]
            if k0 not in lut:
                lut[k0] = _dark_tile_to_palette(*k0)
            if k1 not in lut:
                lut[k1] = _dark_tile_to_palette(*k1)
            out[idx] = (lut[k0] << 4) | lut[k1]
            idx += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_tiles(lat: float, lon: float, maps_dir: Path,
                   radius_km: float = 100.0,
                   callback=None) -> dict:
    """Download Stadia Maps Alidade Smooth Dark tiles around lat/lon.

    Uses tiered radii per zoom level to keep download size reasonable.
    @2x (retina) tiles are downloaded for better label quality.

    callback(pct: float, msg: str) is called with progress updates.
    Returns manifest dict.
    """
    maps_dir.mkdir(parents=True, exist_ok=True)

    # Count total tiles using tiered radii
    all_tiles: list[tuple[int, int, int]] = []  # (z, x, y)
    for z, tier_radius in DOWNLOAD_TIERS:
        r = min(tier_radius, radius_km)
        for tx, ty in _tiles_in_radius(lat, lon, r, z):
            all_tiles.append((z, tx, ty))

    total = len(all_tiles)
    if callback:
        callback(0.0, f"Downloading {total} tiles...")

    done = 0
    skipped = 0
    errors = 0

    for z, tx, ty in all_tiles:
        tile_dir = maps_dir / str(z)
        tile_dir.mkdir(parents=True, exist_ok=True)
        tile_path = tile_dir / f"{tx}_{ty}.dat"

        # Skip already downloaded
        if tile_path.exists():
            skipped += 1
            done += 1
            if callback and done % 20 == 0:
                callback(done / total * 100, f"{done}/{total} (skip:{skipped})")
            continue

        # Check cancel via callback (raises InterruptedError)
        if callback:
            callback(done / total * 100, f"{done}/{total} tiles")

        url = _tile_url(z, tx, ty)
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=10) as resp:
                png_data = resp.read()

            indexed = _convert_tile_pil(png_data)
            compressed = zlib.compress(indexed, 6)

            tile_path.write_bytes(compressed)
        except Exception as e:
            errors += 1
            if callback and errors <= 5:
                callback(done / total * 100, f"ERR tile {z}/{tx}_{ty}: {e}")

        done += 1
        if callback and done % 20 == 0:
            callback(done / total * 100, f"{done}/{total} tiles")

        # Throttle: 50ms between requests (polite to Stadia free tier)
        time.sleep(0.05)

    manifest = {
        "center": [lat, lon],
        "radius_km": radius_km,
        "tiers": {str(z): r for z, r in DOWNLOAD_TIERS},
        "tile_count": total,
        "skipped": skipped,
        "errors": errors,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (maps_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    if callback:
        callback(100.0, f"Done! {total} tiles ({errors} errors)")

    return manifest


# ---------------------------------------------------------------------------
# Tile renderer (pyxel integration)
# ---------------------------------------------------------------------------

class TileRenderer:
    """Load cached tiles and prepare native Pyxel images for repeated draws."""

    def __init__(self, maps_dir: Path):
        self.maps_dir = maps_dir
        self._cache: OrderedDict[tuple[int, int, int], object] = OrderedDict()
        # A viewport can need child tiles and their cropped parents together.
        # Sixteen entries caused a cyclic LRU miss on ordinary z13 views.
        self._max_cache = 64  # 64 KiB each, at most 4 MiB decoded
        self._missing: set[tuple[int, int, int]] = set()
        self._resolved: dict[tuple[int, int, int], tuple | None] = {}
        self._display_cache: OrderedDict[tuple, tuple[object, int]] = OrderedDict()
        self._display_cache_bytes = 0
        self._max_display_cache_bytes = 8 * 1024 * 1024
        self._manifest: dict | None = None
        self._load_manifest()
        self.tiles_visible = False
        self._fb = None  # framebuffer reference (set on first draw)

    def _load_manifest(self):
        mf = self.maps_dir / "manifest.json"
        if mf.exists():
            try:
                self._manifest = json.loads(mf.read_text())
            except Exception:
                self._manifest = None

    def has_tiles(self) -> bool:
        return (self._manifest is not None
                and self._manifest.get("tile_count", 0) > 0)

    def reload_manifest(self):
        self._load_manifest()
        self._cache.clear()
        self._missing.clear()
        self._resolved.clear()
        self._display_cache.clear()
        self._display_cache_bytes = 0
        self._fb = None

    def draw(self, proj, W: int, H: int, HUD_TOP: int, TERM_Y: int) -> bool:
        """Render visible tiles onto pyxel screen.

        Returns True if any tiles were drawn (caller can skip coastlines).
        """
        import pyxel as px

        self.tiles_visible = False
        game_zoom = proj.zoom
        osm_zoom = GAME_TO_OSM.get(game_zoom)
        if osm_zoom is None:
            return False  # Too far out for tiles

        # Viewport bounds in lat/lon
        lat_n = proj.center_lat + proj.lat_span / 2
        lat_s = proj.center_lat - proj.lat_span / 2
        lon_w = proj.center_lon - proj.lon_span / 2
        lon_e = proj.center_lon + proj.lon_span / 2

        # Tile range
        tx_min, ty_min = _lat_lon_to_tile(lat_n, lon_w, osm_zoom)
        tx_max, ty_max = _lat_lon_to_tile(lat_s, lon_e, osm_zoom)

        drawn = False
        native_clip = callable(getattr(px, "clip", None))
        if native_clip:
            px.clip(0, HUD_TOP, W, TERM_Y - HUD_TOP)
        try:
            for tx in range(tx_min, tx_max + 1):
                for ty in range(ty_min, ty_max + 1):
                    if self._draw_tile(px, proj, osm_zoom, tx, ty,
                                       W, H, HUD_TOP, TERM_Y):
                        drawn = True
        finally:
            if native_clip:
                px.clip()

        self.tiles_visible = drawn
        return drawn

    def _draw_tile(self, px, proj, z: int, tx: int, ty: int,
                   W: int, H: int, HUD_TOP: int, TERM_Y: int) -> bool:
        """Render a single tile via direct framebuffer write. Returns True if drawn."""
        lat_tl, lon_tl = _tile_to_lat_lon(tx, ty, z)
        lat_br, lon_br = _tile_to_lat_lon(tx + 1, ty + 1, z)

        sx_tl, sy_tl = proj.geo_to_screen(lat_tl, lon_tl)
        sx_br, sy_br = proj.geo_to_screen(lat_br, lon_br)

        tile_w = sx_br - sx_tl
        tile_h = sy_br - sy_tl
        if tile_w <= 0 or tile_h <= 0:
            return False

        if sx_br < 0 or sx_tl > W or sy_br < HUD_TOP or sy_tl > TERM_Y:
            return False

        # Clip to visible area
        x_start = max(sx_tl, 0)
        x_end = min(sx_br, W)
        y_start = max(sy_tl, HUD_TOP)
        y_end = min(sy_br, TERM_Y)

        if x_end <= x_start or y_end <= y_start:
            return False

        # Build a correctly sized Pyxel image once, then use the engine's native
        # blitter on every subsequent frame.  Keeping width and height in the
        # key preserves the existing equirectangular/Mercator projection shape.
        display_key = (z, tx, ty, tile_w, tile_h)
        display = self._display_cache.get(display_key)
        if display is not None:
            self._display_cache.move_to_end(display_key)
            prepared = display[0]
        else:
            resolved = self._resolve_tile(z, tx, ty)
            if resolved is None:
                return False
            img, source_size, source_x, source_y = resolved
            prepared = self._prepare_display_image(
                px, img, source_size, source_x, source_y, tile_w, tile_h)
            if prepared is not None:
                self._remember_display(display_key, prepared, tile_w * tile_h)

        if prepared is not None and callable(getattr(px, "blt", None)):
            px.blt(sx_tl, sy_tl, prepared, 0, 0, tile_w, tile_h)
            return True

        # Compatibility path for tests or old Pyxel builds without Image/blt.
        resolved = self._resolve_tile(z, tx, ty)
        if resolved is None:
            return False
        img, source_size, source_x, source_y = resolved

        # Direct framebuffer access (100x faster than pset loop)
        try:
            if self._fb is None:
                self._fb = px.screen.data_ptr()
            fb = self._fb
            fb_w = W
        except Exception:
            fb = None

        inv_tw = source_size / tile_w
        inv_th = source_size / tile_h

        if fb is not None:
            # Fast path: direct numpy/memoryview write
            for sy in range(y_start, y_end):
                tp_y = int(source_y + (sy - sy_tl) * inv_th)
                if tp_y < 0 or tp_y >= OSM_TILE_SIZE:
                    continue
                row_off = tp_y * OSM_TILE_SIZE
                fb_row = sy * fb_w
                for sx in range(x_start, x_end):
                    tp_x = int(source_x + (sx - sx_tl) * inv_tw)
                    if 0 <= tp_x < OSM_TILE_SIZE:
                        fb[fb_row + sx] = img[row_off + tp_x]
        else:
            # Fallback: pset (slow but works everywhere)
            for sy in range(y_start, y_end):
                tp_y = int(source_y + (sy - sy_tl) * inv_th)
                if tp_y < 0 or tp_y >= OSM_TILE_SIZE:
                    continue
                row_off = tp_y * OSM_TILE_SIZE
                for sx in range(x_start, x_end):
                    tp_x = int(source_x + (sx - sx_tl) * inv_tw)
                    if 0 <= tp_x < OSM_TILE_SIZE:
                        px.pset(sx, sy, img[row_off + tp_x])

        return True

    def _resolve_tile(self, z: int, tx: int, ty: int):
        """Resolve a requested tile to itself or a cached cropped parent."""
        key = (z, tx, ty)
        if key not in self._resolved:
            spec = None
            if self._get_tile_image(*key) is not None:
                spec = (key, OSM_TILE_SIZE, 0, 0)
            else:
                for parent_z in range(z - 1, 11, -1):
                    factor = 2 ** (z - parent_z)
                    parent_key = (parent_z, tx // factor, ty // factor)
                    if self._get_tile_image(*parent_key) is not None:
                        source_size = OSM_TILE_SIZE // factor
                        spec = (parent_key, source_size,
                                (tx % factor) * source_size,
                                (ty % factor) * source_size)
                        break
            self._resolved[key] = spec

        spec = self._resolved[key]
        if spec is None:
            return None
        source_key, source_size, source_x, source_y = spec
        img = self._get_tile_image(*source_key)
        if img is None:
            # A map directory changed without reload_manifest().  Recover on
            # the next draw instead of retaining a stale resolution forever.
            self._resolved.pop(key, None)
            return None
        return img, source_size, source_x, source_y

    @staticmethod
    def _prepare_display_image(px, img, source_size, source_x, source_y,
                               width: int, height: int):
        """Scale a palette tile crop into a reusable Pyxel Image."""
        image_ctor = getattr(px, "Image", None)
        if not callable(image_ctor):
            return None
        try:
            prepared = image_ctor(width, height)
            out = prepared.data_ptr()
        except Exception:
            return None

        x_lookup = [min(OSM_TILE_SIZE - 1,
                        int(source_x + x * source_size / width))
                    for x in range(width)]
        for y in range(height):
            src_y = min(OSM_TILE_SIZE - 1,
                        int(source_y + y * source_size / height))
            src_row = src_y * OSM_TILE_SIZE
            dst_row = y * width
            for x, src_x in enumerate(x_lookup):
                out[dst_row + x] = img[src_row + src_x]
        return prepared

    def _remember_display(self, key: tuple, image, size: int):
        while (self._display_cache and
               self._display_cache_bytes + size > self._max_display_cache_bytes):
            _old_key, (_old_image, old_size) = self._display_cache.popitem(last=False)
            self._display_cache_bytes -= old_size
        self._display_cache[key] = (image, size)
        self._display_cache_bytes += size

    def _get_tile_image(self, z: int, tx: int, ty: int):
        """Load tile from cache or disk. Returns flat array of palette indices."""
        key = (z, tx, ty)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if key in self._missing:
            return None

        tile_path = self.maps_dir / str(z) / f"{tx}_{ty}.dat"
        if not tile_path.exists():
            self._missing.add(key)
            return None

        try:
            compressed = tile_path.read_bytes()
            raw = zlib.decompress(compressed)
            if len(raw) != OSM_TILE_SIZE * OSM_TILE_SIZE // 2:
                self._missing.add(key)
                return None
            # Joining a 256-entry lookup runs in C for most of the work and is
            # several times faster than assigning 65,536 nibbles in Python.
            pixels = bytearray(b"".join(_UNPACK_NIBBLES[byte] for byte in raw))
        except Exception:
            self._missing.add(key)
            return None

        # LRU eviction
        if len(self._cache) >= self._max_cache:
            self._cache.popitem(last=False)
        self._cache[key] = pixels
        return pixels
