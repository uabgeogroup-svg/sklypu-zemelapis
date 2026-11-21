#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEO GROUP – reljefas iš LiDAR pagal plotą (sklypai, nubraižytas poligonas, DXF riba).

Ką daro:
  1. Gauna ploto ribą: kadastro Nr. (iš RC atvirųjų duomenų), GeoJSON, DXF arba žemėlapio užsakymo failą.
  2. Nustato reikalingas geoportal.lt LiDAR (Lidar_DR_LT) 1×1 km lapų dalis ir parsisiunčia TIK jas
     (HTTP Range iš ZIP archyvo, su tęsimu nutrūkus, su podėliu).
  3. Iškerpa taškus pagal plotą + buferį, iš žemės taškų (2 klasė) sudaro DTM.
  4. Išsaugo: DXF (horizontalės, taškai su aukščiais, riba), DTM GeoTIFF, taškų XYZ/CSV,
     apkarpytą LAZ ir ataskaitą.

Pavyzdžiai:
  python lidar_reljefas.py --kad 4107/0100:0281 4107/0100:0469
  python lidar_reljefas.py plotas.geojson --intervalas 0.25 --taskai 2
  python lidar_reljefas.py riba.dxf --taskai 0
  python lidar_reljefas.py --kad 410701000281 --tik-sarasas
"""
import argparse, datetime, json, math, os, re, ssl, struct, sys, time, urllib.parse, urllib.request, zlib

VERSION = 'lidar_reljefas 2.1 (2026-11-21)'
BASE = os.environ.get('LIDAR_BASE', 'https://www.geoportal.lt/download/lidar_data')
RC_QUERY = os.environ.get('RC_QUERY', 'https://maps.planuojustatau.lt/arcgis/rest/services/map/sklypai/MapServer/0/query')
YEARS = [2026, 2025, 2022, 2021, 2020, 2019]
UA = {'User-Agent': 'GeoGroup-LiDAR-reljefas/2.0'}
LT_BOX = (290000, 5960000, 690000, 6270000)  # LKS-94 E/N ribos patikrai

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def log(msg=''):
    print(msg, flush=True)


class Klaida(Exception):
    pass


# ---------------------------------------------------------------- HTTP
_CTX = ssl.create_default_context()


def http(url, method='GET', rng=None, timeout=60, retries=8):
    last = None
    for k in range(retries):
        try:
            h = dict(UA)
            if rng is not None:
                h['Range'] = 'bytes=%d-%d' % rng
            req = urllib.request.Request(url, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                data = r.read() if method == 'GET' else b''
                return r.status, r.headers, data
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 410):
                return e.code, e.headers, b''
            last = e
            if e.code in (429, 503):  # serveris prašo palaukti
                wait = e.headers.get('Retry-After')
                wait = int(wait) if wait and wait.isdigit() else min(180, 15 * (k + 1))
                log('   serveris užimtas (%d) – laukiama %d s…' % (e.code, wait)); time.sleep(wait); continue
        except Exception as e:  # tinklo klaida – kartojame
            last = e
        time.sleep(min(60, 2 ** k))
    raise Klaida('Nepavyko pasiekti %s (%s)' % (url, last))


def download_range(url, start, length, dest, label=''):
    """Parsisiunčia baitus [start, start+length) į dest viena užklausa (srautu); nutrūkus tęsia nuo .part."""
    part = dest + '.part'
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have > length:
        os.remove(part); have = 0
    t0 = time.time(); last_print = 0; tries = 0; got0 = have
    while have < length:
        try:
            h = dict(UA); h['Range'] = 'bytes=%d-%d' % (start + have, start + length - 1)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=120, context=_CTX) as r, open(part, 'ab') as f:
                if r.status != 206:
                    raise Klaida('Serveris nepalaiko dalinio siuntimo (Range), atsakymas %s' % r.status)
                while have < length:
                    b = r.read(1024 * 1024)
                    if not b:
                        break
                    f.write(b); have += len(b)
                    if time.time() - last_print > 3 or have >= length:
                        sp = (have - got0) / max(0.1, time.time() - t0) / 1e6
                        log('   %s %5.1f %%  (%d / %d MB, %.1f MB/s)' % (label, have * 100 / length, have // 1e6, length // 1e6, sp))
                        last_print = time.time()
        except Klaida:
            raise
        except urllib.error.HTTPError as e:
            tries += 1
            if tries > 10:
                raise Klaida('Parsisiuntimas nutrūko per daug kartų (%s)' % e)
            wait = min(180, 15 * tries) if e.code in (429, 503) else min(60, 2 ** tries)
            log('   klaida %s – kartojama po %d s…' % (e.code, wait)); time.sleep(wait)
        except Exception as e:
            tries += 1
            if tries > 10:
                raise Klaida('Parsisiuntimas nutrūko per daug kartų (%s)' % e)
            log('   ryšys nutrūko (%s) – tęsiama po %d s…' % (e.__class__.__name__, min(60, 2 ** tries))); time.sleep(min(60, 2 ** tries))
    os.replace(part, dest)


# ---------------------------------------------------------------- ZIP (tik centrinis katalogas per Range)
def parse_zip_directory(tail, tail_start, fetch):
    """tail – paskutiniai archyvo baitai nuo tail_start. fetch(a,b) – baitų gavimas.
    Grąžina {vardas: (metodas, suspaustas, išskleistas, lokalaus_antrašto_poslinkis)}"""
    i = tail.rfind(b'PK\x05\x06')
    if i < 0:
        raise Klaida('ZIP pabaigos įrašas nerastas')
    _, _, _, _, n, cd_size, cd_off, _ = struct.unpack('<4sHHHHIIH', tail[i:i + 22])
    if cd_off == 0xFFFFFFFF or n == 0xFFFF or cd_size == 0xFFFFFFFF:
        j = tail.rfind(b'PK\x06\x07', 0, i)
        if j < 0:
            raise Klaida('ZIP64 lokatorius nerastas')
        _, _, z64off, _ = struct.unpack('<4sIQI', tail[j:j + 20])
        rec = tail[z64off - tail_start:z64off - tail_start + 56] if z64off >= tail_start else fetch(z64off, z64off + 55)
        sig, _, _, _, _, _, _, n, cd_size, cd_off = struct.unpack('<4sQHHIIQQQQ', rec[:56])
        if sig != b'PK\x06\x06':
            raise Klaida('ZIP64 įrašas sugadintas')
    cd = tail[cd_off - tail_start:cd_off - tail_start + cd_size] if cd_off >= tail_start else fetch(cd_off, cd_off + cd_size - 1)
    out = {}; p = 0
    for _ in range(n):
        f = struct.unpack('<4sHHHHHHIIIHHHHHII', cd[p:p + 46])
        if f[0] != b'PK\x01\x02':
            raise Klaida('ZIP katalogo įrašas sugadintas')
        method, csize, usize, nl, el, cl, loff = f[4], f[8], f[9], f[10], f[11], f[12], f[16]
        name = cd[p + 46:p + 46 + nl].decode('utf-8', 'replace')
        extra = cd[p + 46 + nl:p + 46 + nl + el]
        q = 0
        while q + 4 <= len(extra):
            hid, hl = struct.unpack('<HH', extra[q:q + 4]); body = extra[q + 4:q + 4 + hl]; r = 0
            if hid == 1:
                if usize == 0xFFFFFFFF: usize = struct.unpack('<Q', body[r:r + 8])[0]; r += 8
                if csize == 0xFFFFFFFF: csize = struct.unpack('<Q', body[r:r + 8])[0]; r += 8
                if loff == 0xFFFFFFFF: loff = struct.unpack('<Q', body[r:r + 8])[0]; r += 8
            q += 4 + hl
        out[name] = (method, csize, usize, loff)
        p += 46 + nl + el + cl
    return out


_dir_cache = {}
DIR_CACHE_FILE = None


def _load_dir_cache():
    global _dir_cache
    if DIR_CACHE_FILE and os.path.exists(DIR_CACHE_FILE) and time.time() - os.path.getmtime(DIR_CACHE_FILE) < 7 * 86400:
        try:
            raw = json.load(open(DIR_CACHE_FILE, encoding='utf-8'))
            _dir_cache = {k: (None if v is None else (v[0], {n: tuple(t) for n, t in v[1].items()})) for k, v in raw.items()}
        except Exception:
            _dir_cache = {}


def _save_dir_cache():
    if DIR_CACHE_FILE:
        try:
            os.makedirs(os.path.dirname(DIR_CACHE_FILE), exist_ok=True)
            json.dump(_dir_cache, open(DIR_CACHE_FILE, 'w', encoding='utf-8'))
        except Exception:
            pass


def archive_dir(url):
    if url in _dir_cache:
        return _dir_cache[url]
    st, hd, _ = http(url, method='HEAD', timeout=30)
    if st != 200:
        _dir_cache[url] = None; _save_dir_cache(); return None
    size = int(hd.get('Content-Length'))
    ts = max(0, size - 65536)
    _, _, tail = http(url, rng=(ts, size - 1))
    fetch = lambda a, b: http(url, rng=(a, b))[2]
    d = parse_zip_directory(tail, ts, fetch)
    _dir_cache[url] = (size, d); _save_dir_cache()
    return _dir_cache[url]


# ---------------------------------------------------------------- lapai ir dalys
def parts_for_bbox(xmin, ymin, xmax, ymax):
    """5×5 km lapai (c/r) ir 1×1 km dalys 1..25 (nuo ŠV kampo eilutėmis)."""
    out = []
    for c in range(int((xmin - 2e5) // 5000), int((xmax - 2e5) // 5000) + 1):
        for r in range(int((ymin - 5.9e6) // 5000), int((ymax - 5.9e6) // 5000) + 1):
            sx = 2e5 + 5000 * c; sy1 = 5.9e6 + 5000 * (r + 1)
            for n in range(1, 26):
                row, col = (n - 1) // 5, (n - 1) % 5
                x0 = sx + 1000 * col; y1 = sy1 - 1000 * row
                if x0 < xmax and x0 + 1000 > xmin and y1 - 1000 < ymax and y1 > ymin:
                    out.append((c, r, n, (x0, y1 - 1000, x0 + 1000, y1)))
    return out


def member_for(d, c, r, n):
    pat = re.compile(r'(?:^|/)%d_%d[-_]%02d\.laz$' % (c, r, n), re.I)
    for name in d:
        if pat.search(name):
            return name
    return None


def default_cache():
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'GeoGroupLiDAR', 'podelis')


def clean_cache(cache, days):
    if days <= 0 or not os.path.isdir(cache):
        return
    lim = time.time() - days * 86400; n = 0; mb = 0
    for fn in os.listdir(cache):
        p = os.path.join(cache, fn)
        if os.path.isfile(p) and os.path.getmtime(p) < lim:
            mb += os.path.getsize(p) / 1e6; os.remove(p); n += 1
    if n:
        log('Podėlis: ištrinta %d senų failų (%.0f MB), senesnių nei %d d.' % (n, mb, days))


def fetch_part(c, r, n, cache, only_list=False):
    """Randa naujausių metų archyvą su šia dalimi ir parsisiunčia LAZ (arba grąžina iš podėlio)."""
    for y in YEARS:
        cached = os.path.join(cache, '%d_%d_%d-%02d.laz' % (y, c, r, n))
        if os.path.exists(cached) and not only_list:
            os.utime(cached, None)
            return {'metai': y, 'failas': cached, 'is_podelio': True, 'mb': os.path.getsize(cached) / 1e6, 'url': '%s/%d/%d_%d.zip' % (BASE, y, c, r)}
    for y in YEARS:
        url = '%s/%d/%d_%d.zip' % (BASE, y, c, r)
        ad = archive_dir(url)
        if not ad:
            continue
        size, d = ad
        name = member_for(d, c, r, n)
        if not name:
            continue
        method, csize, usize, loff = d[name]
        info = {'metai': y, 'url': url, 'narys': name, 'mb': csize / 1e6, 'is_podelio': False}
        if only_list:
            return info
        os.makedirs(cache, exist_ok=True)
        dest = os.path.join(cache, '%d_%d_%d-%02d.laz' % (y, c, r, n))
        _, _, lh = http(url, rng=(loff, loff + 1023))
        if lh[:4] != b'PK\x03\x04':
            raise Klaida('Netikėtas ZIP nario antraštas (%s)' % name)
        nl, el = struct.unpack('<HH', lh[26:30])
        doff = loff + 30 + nl + el
        label = '%d %d_%d-%02d' % (y, c, r, n)
        if method == 0:
            download_range(url, doff, csize, dest, label)
        elif method == 8:
            tmp = dest + '.deflate'
            download_range(url, doff, csize, tmp, label)
            log('   išskleidžiama…')
            dec = zlib.decompressobj(-15)
            with open(tmp, 'rb') as fi, open(dest + '.tmp', 'wb') as fo:
                while True:
                    b = fi.read(8 * 1024 * 1024)
                    if not b:
                        break
                    fo.write(dec.decompress(b))
                fo.write(dec.flush())
            if os.path.getsize(dest + '.tmp') != usize:
                raise Klaida('Išskleistas dydis nesutampa (%s)' % name)
            os.replace(dest + '.tmp', dest); os.remove(tmp)
        else:
            raise Klaida('Nepalaikomas ZIP glaudinimo metodas %d' % method)
        info['failas'] = dest
        return info
    return None


# ---------------------------------------------------------------- koordinatės
def wgs_to_lks(lon, lat):
    """WGS84/ETRS89 → LKS-94 (EPSG:3346, TM lon0=24°, k=0.9998, FE=500000)."""
    a = 6378137.0; f = 1 / 298.257222101; e2 = f * (2 - f); k0 = 0.9998
    phi = math.radians(lat); lam = math.radians(lon - 24.0)
    n = f / (2 - f); A = a / (1 + n) * (1 + n * n / 4 + n ** 4 / 64)
    al = [n / 2 - 2 * n * n / 3 + 5 * n ** 3 / 16, 13 * n * n / 48 - 3 * n ** 3 / 5, 61 * n ** 3 / 240]
    e = math.sqrt(e2)
    t = math.sinh(math.atanh(math.sin(phi)) - e * math.atanh(e * math.sin(phi)))
    xi = math.atan2(t, math.cos(lam)); eta = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    E = eta + sum(al[j] * math.cos(2 * (j + 1) * xi) * math.sinh(2 * (j + 1) * eta) for j in range(3))
    N = xi + sum(al[j] * math.sin(2 * (j + 1) * xi) * math.cosh(2 * (j + 1) * eta) for j in range(3))
    return 500000 + k0 * A * E, k0 * A * N


def norm_kad(s):
    s = s.strip()
    d = re.sub(r'\D', '', s)
    m = re.match(r'^(\d{4})\D+(\d{4})\D+(\d{1,4})$', s)
    if m:
        return '%s/%s:%s' % (m.group(1), m.group(2), m.group(3).zfill(4))
    if 9 <= len(d) <= 12:
        return '%s/%s:%s' % (d[:4], d[4:8], d[8:].zfill(4))
    raise Klaida('Neatpažintas kadastro Nr.: %s' % s)


def rings_from_rc(kads):
    nrs = [norm_kad(k) for k in kads]
    where = 'kadastro_nr IN (%s)' % ','.join("'%s'" % k for k in nrs)
    q = urllib.parse.urlencode({'where': where, 'outFields': 'kadastro_nr,skl_plotas', 'returnGeometry': 'true', 'outSR': '3346', 'f': 'json'})
    st, _, data = http(RC_QUERY + '?' + q, timeout=60)
    if st != 200:
        raise Klaida('RC sklypų servisas neatsakė (%s)' % st)
    js = json.loads(data.decode('utf-8'))
    if 'error' in js:
        raise Klaida('RC sklypų servisas: %s' % js['error'].get('message'))
    rings = []; found = set()
    for f in js.get('features', []):
        found.add(f['attributes']['kadastro_nr'])
        rings += [[(float(x), float(y)) for x, y in r] for r in f['geometry']['rings']]
    miss = [k for k in nrs if k not in found]
    if miss:
        raise Klaida('RC rinkinyje nerasta: %s' % ', '.join(miss))
    return rings, nrs


def fix_ring(r):
    r = [(float(p[0]), float(p[1])) for p in r]
    if all(abs(x) <= 180 and abs(y) <= 90 for x, y in r):
        r = [wgs_to_lks(x, y) for x, y in r]
    elif all(5.9e6 < x < 6.3e6 for x, _ in r):
        r = [(y, x) for x, y in r]  # sukeistos X/Y
    if r[0] != r[-1]:
        r.append(r[0])
    for x, y in r:
        if not (LT_BOX[0] < x < LT_BOX[2] and LT_BOX[1] < y < LT_BOX[3]):
            raise Klaida('Koordinatės ne LKS-94 Lietuvos ribose: %.2f %.2f' % (x, y))
    return r


def rings_from_geojson(path):
    js = json.load(open(path, encoding='utf-8-sig'))
    feats = js['features'] if js.get('type') == 'FeatureCollection' else [js]
    rings = []
    for f in feats:
        g = f.get('geometry', f)
        if g['type'] == 'Polygon':
            rings += [fix_ring(r) for r in g['coordinates']]
        elif g['type'] == 'MultiPolygon':
            for p in g['coordinates']:
                rings += [fix_ring(r) for r in p]
    if not rings:
        raise Klaida('GeoJSON faile poligonų nėra')
    return rings


def rings_from_dxf(path):
    import ezdxf
    doc = ezdxf.readfile(path)
    rings = []; opens = []
    for e in doc.modelspace().query('LWPOLYLINE POLYLINE'):
        if e.dxftype() == 'LWPOLYLINE':
            pts = [(p[0], p[1]) for p in e.get_points('xy')]; closed = e.closed
        else:
            pts = [(v.dxf.location[0], v.dxf.location[1]) for v in e.vertices]; closed = e.is_closed
        if len(pts) < 3:
            continue
        if closed or (pts[0] == pts[-1]):
            rings.append(fix_ring(pts))
        else:
            opens.append(pts)
    if not rings and opens:
        rings = [fix_ring(max(opens, key=len))]
        log('Pastaba: DXF uždarų polilinijų nėra – naudojama ilgiausia polilinija (uždaroma).')
    if not rings:
        raise Klaida('DXF faile polilinijų (ribų) nerasta')
    return rings


# ---------------------------------------------------------------- geometrija
def ring_area(r):
    return abs(sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1] for i in range(len(r) - 1)) / 2)


def inside(x, y, rings):
    import numpy as np
    res = np.zeros(x.shape, bool)
    for r in rings:
        a = np.asarray(r)
        for i in range(len(a) - 1):
            xi, yi = a[i]; xj, yj = a[i + 1]
            if yi == yj:
                continue
            c = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi) + xi)
            res ^= c
    return res


def dist_to_rings(x, y, rings):
    import numpy as np
    d = np.full(x.shape, np.inf)
    for r in rings:
        a = np.asarray(r)
        for i in range(len(a) - 1):
            x0, y0 = a[i]; x1, y1 = a[i + 1]; dx, dy = x1 - x0, y1 - y0; L2 = dx * dx + dy * dy
            t = np.clip(((x - x0) * dx + (y - y0) * dy) / L2, 0, 1) if L2 else 0
            d = np.minimum(d, np.hypot(x - x0 - t * dx, y - y0 - t * dy))
    return d


def in_zone(x, y, rings, buf):
    m = inside(x, y, rings)
    if buf > 0 and (~m).any():
        o = ~m
        m[o] = dist_to_rings(x[o], y[o], rings) <= buf
    return m


# ---------------------------------------------------------------- LAZ
def clip_laz(path, rings, buf, bbox, out_laz):
    """Grąžina žemės taškus (x,y,z) zonoje, taškų skaičius; įrašo apkarpytą LAZ (visos klasės)."""
    import numpy as np, laspy
    gx, gy, gz = [], [], []; n_all = 0; n_tot = 0; writer = None; info = {}
    with laspy.open(path) as fh:
        h = fh.header
        info = {'taskai_dalyje': h.point_count, 'formatas': h.point_format.id, 'versija': str(h.version),
                'aprėptis': [round(h.mins[0], 2), round(h.mins[1], 2), round(h.maxs[0], 2), round(h.maxs[1], 2)]}
        if h.maxs[0] < bbox[0] or h.mins[0] > bbox[2] or h.maxs[1] < bbox[1] or h.mins[1] > bbox[3]:
            return np.zeros((0, 3)), 0, info
        if out_laz:
            nh = laspy.LasHeader(point_format=h.point_format, version=h.version)
            nh.offsets = h.offsets; nh.scales = h.scales
            for v in h.vlrs:
                if type(v).__name__ not in ('LasZipVlr', 'ExtraBytesVlr'):
                    nh.vlrs.append(v)
            try:
                nh.global_encoding = h.global_encoding
            except Exception:
                pass
        for ch in fh.chunk_iterator(2_000_000):
            n_tot += len(ch)
            x = np.asarray(ch.x); y = np.asarray(ch.y)
            m = (x >= bbox[0]) & (x <= bbox[2]) & (y >= bbox[1]) & (y <= bbox[3])
            if not m.any():
                continue
            idx = np.nonzero(m)[0]
            z = in_zone(x[idx], y[idx], rings, buf)
            idx = idx[z]
            if not len(idx):
                continue
            pts = ch[idx]
            n_all += len(idx)
            if out_laz:
                if writer is None:
                    writer = laspy.open(out_laz, mode='w', header=nh, do_compress=True)
                writer.write_points(pts)
            cls = np.asarray(pts.classification)
            g = cls == 2
            gx.append(np.asarray(pts.x)[g]); gy.append(np.asarray(pts.y)[g]); gz.append(np.asarray(pts.z)[g])
    if writer is not None:
        writer.close()
    info['taskai_zonoje'] = int(n_all)
    if not gx:
        return np.zeros((0, 3)), n_all, info
    return np.column_stack([np.concatenate(gx), np.concatenate(gy), np.concatenate(gz)]), n_all, info


# ---------------------------------------------------------------- DTM
def build_dtm(G, bbox, res, rings, buf, max_gap):
    import numpy as np
    from scipy.spatial import cKDTree, Delaunay
    from scipy.interpolate import LinearNDInterpolator
    x0 = math.floor(bbox[0] / res) * res; y1 = math.ceil(bbox[3] / res) * res
    nx = int(math.ceil((bbox[2] - x0) / res)); ny = int(math.ceil((y1 - bbox[1]) / res))
    if nx * ny > 60e6:
        raise Klaida('Plotas per didelis gardelei %.2f m (%d×%d) – padidinkite --gardele' % (res, nx, ny))
    i = np.floor((G[:, 0] - x0) / res).astype(np.int64); j = np.floor((y1 - G[:, 1]) / res).astype(np.int64)
    ok = (i >= 0) & (i < nx) & (j >= 0) & (j < ny)
    k = j[ok] * nx + i[ok]
    s = np.bincount(k, weights=G[ok, 2], minlength=nx * ny); c = np.bincount(k, minlength=nx * ny)
    Z = np.full(nx * ny, np.nan); v = c > 0; Z[v] = s[v] / c[v]
    Z = Z.reshape(ny, nx)
    xc = x0 + (np.arange(nx) + .5) * res; yc = y1 - (np.arange(ny) + .5) * res
    XX, YY = np.meshgrid(xc, yc)
    zone = in_zone(XX.ravel(), YY.ravel(), rings, buf + res).reshape(ny, nx)
    Z[~zone] = np.nan
    gaps = zone & np.isnan(Z); filled = 0
    if gaps.any() and (~np.isnan(Z)).any():
        vy, vx = np.nonzero(~np.isnan(Z)); gy_, gx_ = np.nonzero(gaps)
        vpts = np.column_stack([xc[vx], yc[vy]]); gpts = np.column_stack([xc[gx_], yc[gy_]])
        tree = cKDTree(vpts)
        d, _ = tree.query(gpts, distance_upper_bound=max_gap + 1e-6)
        fill = np.isfinite(d)
        if fill.any():
            near = tree.query_ball_point(gpts[fill], max_gap + res)
            use = np.unique(np.fromiter((q for lst in near for q in lst), dtype=np.int64))
            if len(use) >= 3:
                tri = Delaunay(vpts[use])
                f = LinearNDInterpolator(tri, Z[vy[use], vx[use]])
                val = f(gpts[fill])
                gi = np.nonzero(fill)[0]; good = ~np.isnan(val)
                Z[gy_[gi[good]], gx_[gi[good]]] = val[good]; filled = int(good.sum())
    return {'Z': Z, 'x0': x0, 'y1': y1, 'res': res, 'nx': nx, 'ny': ny, 'xc': xc, 'yc': yc, 'uzpildyta': filled}


def smooth_nan(Z, sigma):
    import numpy as np
    from scipy.ndimage import gaussian_filter
    if sigma <= 0:
        return Z.copy()
    m = ~np.isnan(Z); A = np.where(m, Z, 0.0)
    num = gaussian_filter(A, sigma); den = gaussian_filter(m.astype(float), sigma)
    out = np.where(m & (den > 1e-6), num / np.maximum(den, 1e-6), np.nan)
    return out


def sample(D, x, y):
    """Dvitiesinė interpoliacija; NaN, jei trūksta bent vieno kaimyno."""
    import numpy as np
    fx = (x - D['x0']) / D['res'] - .5; fy = (D['y1'] - y) / D['res'] - .5
    i = np.floor(fx).astype(int); j = np.floor(fy).astype(int)
    ok = (i >= 0) & (j >= 0) & (i < D['nx'] - 1) & (j < D['ny'] - 1)
    out = np.full(x.shape, np.nan)
    i, j, a, b = i[ok], j[ok], (fx - np.floor(fx))[ok], (fy - np.floor(fy))[ok]
    Z = D['Z']
    out[ok] = Z[j, i] * (1 - a) * (1 - b) + Z[j, i + 1] * a * (1 - b) + Z[j + 1, i] * (1 - a) * b + Z[j + 1, i + 1] * a * b
    return out


def chaikin(pts, it, closed):
    import numpy as np
    p = np.asarray(pts, float)
    for _ in range(it):
        if len(p) < 3:
            break
        if closed:
            q = np.roll(p, -1, axis=0)
            p = np.column_stack([.75 * p + .25 * q, .25 * p + .75 * q]).reshape(-1, 2)
        else:
            a, b = p[:-1], p[1:]
            mid = np.column_stack([.75 * a + .25 * b, .25 * a + .75 * b]).reshape(-1, 2)
            p = np.vstack([p[:1], mid, p[-1:]])
    return p


def contours(D, iv, rings, sigma, simplify, min_len=10.0, rounds=3):
    import numpy as np, contourpy
    Zs = smooth_nan(D['Z'], sigma / D['res'])
    XX, YY = np.meshgrid(D['xc'], D['yc'])
    m = inside(XX.ravel(), YY.ravel(), rings).reshape(Zs.shape)
    # leidžiame horizontalėms siekti ribą: vienas langelis už ribos
    from scipy.ndimage import binary_dilation
    m = binary_dilation(m, iterations=1)
    Zm = np.ma.masked_invalid(np.where(m, Zs, np.nan))
    if Zm.count() < 4:
        return []
    lo = math.ceil(float(Zm.min()) / iv) * iv; hi = float(Zm.max())
    gen = contourpy.contour_generator(D['xc'], D['yc'], Zm, line_type='Separate')
    out = []; lev = lo
    while lev <= hi + 1e-9:
        for ln in gen.lines(lev):
            if len(ln) < 2:
                continue
            L = float(np.hypot(*np.diff(ln, axis=0).T).sum())
            if L < min_len:  # smulkūs triukšmo žiedeliai ir trupiniai
                continue
            closed = bool(np.allclose(ln[0], ln[-1]))
            ln = dp(ln, simplify) if simplify > 0 else ln
            if rounds:
                ln = chaikin(ln[:-1] if closed else ln, rounds, closed)
                if closed:
                    ln = np.vstack([ln, ln[:1]])
            major = abs(lev / (iv * 5) - round(lev / (iv * 5))) < 1e-6
            out.append((round(lev, 3), ln, major, closed))
        lev = round(lev + iv, 6)
    return out


def dp(pts, tol):
    import numpy as np
    pts = np.asarray(pts)
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool); keep[0] = keep[-1] = True; st = [(0, len(pts) - 1)]
    while st:
        a, b = st.pop()
        if b <= a + 1:
            continue
        p, q = pts[a], pts[b]; d = q - p; L = math.hypot(*d)
        seg = pts[a + 1:b]
        dist = np.abs(d[0] * (seg[:, 1] - p[1]) - d[1] * (seg[:, 0] - p[0])) / L if L else np.hypot(*(seg - p).T)
        k = int(np.argmax(dist))
        if dist[k] > tol:
            m = a + 1 + k; keep[m] = True; st += [(a, m), (m, b)]
    return pts[keep]


def grid_points(D, rings, step):
    import numpy as np
    xs = [p[0] for r in rings for p in r]; ys = [p[1] for r in rings for p in r]
    gx = np.arange(math.ceil(min(xs) / step) * step, max(xs) + 1e-9, step)
    gy = np.arange(math.ceil(min(ys) / step) * step, max(ys) + 1e-9, step)
    XX, YY = np.meshgrid(gx, gy); X = XX.ravel(); Y = YY.ravel()
    m = inside(X, Y, rings); X, Y = X[m], Y[m]
    H = sample(D, X, Y); ok = ~np.isnan(H)
    return X[ok], Y[ok], H[ok]


# ---------------------------------------------------------------- išvestis
def write_geotiff(path, D, meta):
    import numpy as np
    Z = np.where(np.isnan(D['Z']), -9999.0, D['Z']).astype('<f4')
    h, w = Z.shape; data = Z.tobytes()
    gdal_meta = ('<GDALMetadata>' + ''.join('<Item name="%s">%s</Item>' % (k, str(v).replace('&', '&amp;').replace('<', '&lt;')) for k, v in meta.items()) + '</GDALMetadata>').encode('utf-8') + b'\0'
    nodata = b'-9999\0'
    geokeys = [1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, 3346]
    entries = []  # (tag, type, count, value_bytes)
    def add(tag, typ, vals):
        fmt = {3: 'H', 4: 'I', 12: 'd', 2: 's'}[typ]
        if typ == 2:
            b = vals; cnt = len(vals)
        else:
            b = struct.pack('<%d%s' % (len(vals), fmt), *vals); cnt = len(vals)
        entries.append((tag, typ, cnt, b))
    add(256, 4, [w]); add(257, 4, [h]); add(258, 3, [32]); add(259, 3, [1]); add(262, 3, [1])
    add(273, 4, [0]); add(277, 3, [1]); add(278, 4, [h]); add(279, 4, [len(data)]); add(284, 3, [1]); add(339, 3, [3])
    add(33550, 12, [D['res'], D['res'], 0.0]); add(33922, 12, [0, 0, 0, D['x0'], D['y1'], 0])
    add(34735, 3, geokeys); add(42112, 2, gdal_meta); add(42113, 2, nodata)
    entries.sort(key=lambda e: e[0])
    ifd_off = 8; ifd_size = 2 + 12 * len(entries) + 4; ext_off = ifd_off + ifd_size
    ext = b''; raw = []
    for tag, typ, cnt, b in entries:
        if len(b) <= 4:
            raw.append(struct.pack('<HHI', tag, typ, cnt) + b.ljust(4, b'\0'))
        else:
            if len(ext) % 2: ext += b'\0'
            raw.append(struct.pack('<HHII', tag, typ, cnt, ext_off + len(ext))); ext += b
    data_off = ext_off + len(ext)
    raw = [r if struct.unpack('<H', r[:2])[0] != 273 else struct.pack('<HHII', 273, 4, 1, data_off) for r in raw]
    with open(path, 'wb') as f:
        f.write(b'II*\0' + struct.pack('<I', ifd_off) + struct.pack('<H', len(entries)) + b''.join(raw) + b'\0\0\0\0' + ext + data)


def write_dxf(path, rings, cont, pts, iv, step, labels, info_lines):
    import ezdxf
    doc = ezdxf.new('R2010', setup=False); doc.header['$INSUNITS'] = 6
    for name, col in [('PLOTO_RIBA', 5), ('HORIZONTALES', 8), ('HORIZONTALES_PAGR', 30), ('TASKAI', 1), ('TASKU_AUKSCIAI', 7), ('INFO', 7)]:
        doc.layers.add(name, color=col)
    msp = doc.modelspace()
    zmean = float(pts[2].mean()) if pts is not None and len(pts[2]) else 0.0
    for r in rings:
        msp.add_lwpolyline([(x, y) for x, y in r[:-1]], close=True, dxfattribs={'layer': 'PLOTO_RIBA', 'elevation': zmean})
    for lev, ln, major, closed in cont:
        pts_ = [(float(x), float(y)) for x, y in (ln[:-1] if closed else ln)]
        msp.add_lwpolyline(pts_, close=closed, dxfattribs={'layer': 'HORIZONTALES_PAGR' if major else 'HORIZONTALES', 'elevation': lev})
    if pts is not None and step:
        th = max(.25, min(1.5, step * .15))
        for x, y, h in zip(*pts):
            msp.add_point((float(x), float(y), float(h)), dxfattribs={'layer': 'TASKAI'})
            if labels:
                msp.add_text('%.2f' % h, height=th, dxfattribs={'layer': 'TASKU_AUKSCIAI', 'insert': (float(x) + th * .3, float(y) + th * .3, float(h))})
    xs = [p[0] for r in rings for p in r]; ys = [p[1] for r in rings for p in r]
    bx = max(xs) + 5; by = max(ys)
    for t in info_lines:
        msp.add_text(t, height=1.2, dxfattribs={'layer': 'INFO', 'insert': (bx, by)}); by -= 2
    doc.saveas(path)


def safe(s):
    return re.sub(r'[^0-9A-Za-z_.-]+', '_', s).strip('_')[:80] or 'plotas'


# ---------------------------------------------------------------- pagrindinė eiga
def main(argv=None):
    ap = argparse.ArgumentParser(description='GEO GROUP – LiDAR reljefas pagal plotą (' + VERSION + ')')
    ap.add_argument('failas', nargs='?', help='GeoJSON, DXF arba žemėlapio užsakymo (.json) failas su ribomis')
    ap.add_argument('--kad', nargs='+', help='kadastro Nr. (vienas ar keli, galima be skiriamųjų ženklų)')
    ap.add_argument('--pavadinimas', help='rezultatų aplanko ir failų pavadinimas')
    ap.add_argument('--intervalas', type=float, default=0.5, help='horizontalių intervalas, m (numatyta 0.5)')
    ap.add_argument('--taskai', type=float, default=5, help='taškų tankumas DXF, m; 0 – nedėti (numatyta 5)')
    ap.add_argument('--be-uzrasu', action='store_true', help='nedėti aukščių užrašų prie taškų')
    ap.add_argument('--buferis', type=float, default=20, help='buferis aplink plotą, m (numatyta 20)')
    ap.add_argument('--gardele', type=float, default=0.5, help='DTM gardelė, m (numatyta 0.5)')
    ap.add_argument('--max-tarpas', type=float, default=2.0, help='didžiausias užpildomas tarpas be žemės taškų, m')
    ap.add_argument('--lyginimas', type=float, default=1.5, help='horizontalių lyginimas, m (Gauso sigma; 0 – be lyginimo)')
    ap.add_argument('--min-ilgis', type=float, default=10.0, help='trumpesnės horizontalės (triukšmas) nebraižomos, m')
    ap.add_argument('--apvalinimas', type=int, default=3, help='horizontalių kampų apvalinimo kartai (0 – be)')
    ap.add_argument('--isvestis', help='kur kurti rezultatų aplanką (numatyta – Atsisiuntimai)')
    ap.add_argument('--podelis', default=default_cache(), help='LiDAR dalių podėlis')
    ap.add_argument('--podelio-dienos', type=int, default=30, help='trinti podėlio failus, nenaudotus N dienų (0 – netrinti)')
    ap.add_argument('--be-laz', action='store_true', help='neišsaugoti apkarpyto LAZ')
    ap.add_argument('--vietiniai', nargs='+', help='naudoti jau turimus LAZ failus vietoj parsisiuntimo')
    ap.add_argument('--tik-sarasas', action='store_true', help='tik parodyti, kokias LiDAR dalis reikėtų parsisiųsti')
    ap.add_argument('--atidaryti', action='store_true', help='baigus atidaryti rezultatų aplanką')
    a = ap.parse_args(argv)
    import numpy as np
    t0 = time.time()
    log('=' * 64); log(VERSION); log('=' * 64)

    # --- ribos
    name = a.pavadinimas; params_from = {}
    if a.kad:
        log('Imamos sklypų ribos iš RC: ' + ', '.join(a.kad))
        rings, nrs = rings_from_rc(a.kad)
        name = name or '_'.join(n.replace('/', '_').replace(':', '_') for n in nrs[:3]) + ('_ir_kt' if len(nrs) > 3 else '')
        src = 'RC sklypai: ' + ', '.join(nrs)
    elif a.failas:
        p = a.failas; ext = os.path.splitext(p)[1].lower()
        if ext == '.dxf':
            rings = rings_from_dxf(p); src = 'DXF: ' + os.path.basename(p)
        else:
            js = json.load(open(p, encoding='utf-8-sig'))
            if js.get('geogroup_lidar_uzsakymas'):
                params_from = js.get('parametrai', {})
                if js.get('ribos'):
                    rings = [fix_ring(r) for r in js['ribos']]
                    src = ('Sklypai: ' + ', '.join(js['kadastro_nr'])) if js.get('kadastro_nr') else 'Žemėlapyje nubraižytas plotas'
                else:
                    rings, nrs = rings_from_rc(js['kadastro_nr']); src = 'RC sklypai: ' + ', '.join(nrs)
                name = name or js.get('pavadinimas')
            else:
                rings = rings_from_geojson(p); src = 'GeoJSON: ' + os.path.basename(p)
        name = name or os.path.splitext(os.path.basename(p))[0]
    else:
        ap.error('nurodykite --kad arba failą su ribomis')
    for k, v in params_from.items():
        if k in ('intervalas', 'taskai', 'buferis', 'gardele') and getattr(a, k) == ap.get_default(k):
            setattr(a, k, float(v))
    name = safe(name)
    area = sum(ring_area(r) for r in rings)
    xs = [p[0] for r in rings for p in r]; ys = [p[1] for r in rings for p in r]
    bbox = (min(xs) - a.buferis, min(ys) - a.buferis, max(xs) + a.buferis, max(ys) + a.buferis)
    log('Plotas: %.4f ha (%s), buferis %g m' % (area / 1e4, src, a.buferis))
    if area > 2000e4:
        raise Klaida('Plotas per didelis (%.0f ha). Šiam įrankiui – iki 2000 ha.' % (area / 1e4))

    # --- LiDAR dalys
    files = []
    if a.vietiniai:
        files = [{'failas': f, 'metai': None, 'is_podelio': True, 'mb': os.path.getsize(f) / 1e6, 'url': 'vietinis'} for f in a.vietiniai]
    else:
        global DIR_CACHE_FILE
        DIR_CACHE_FILE = os.path.join(a.podelis, 'archyvu_katalogai.json'); _load_dir_cache()
        need = parts_for_bbox(*bbox)
        log('Reikalingos LiDAR dalys (1×1 km): ' + ', '.join('%d_%d-%02d' % (c, r, n) for c, r, n, _ in need))
        if not a.tik_sarasas:
            clean_cache(a.podelis, a.podelio_dienos)
        for c, r, n, pb in need:
            log('• %d_%d-%02d' % (c, r, n))
            inf = fetch_part(c, r, n, a.podelis, only_list=a.tik_sarasas)
            if not inf:
                log('   duomenų nėra jokių metų archyve (dalis gali būti už LiDAR aprėpties)'); continue
            inf.update({'dalis': '%d_%d-%02d' % (c, r, n)})
            log('   %d m., %.0f MB%s' % (inf['metai'], inf['mb'], ' – iš podėlio' if inf.get('is_podelio') else ''))
            files.append(inf)
        if a.tik_sarasas:
            tot = sum(f['mb'] for f in files if not f.get('is_podelio'))
            log('Iš viso reikėtų parsisiųsti ~%.0f MB.' % tot); return 0
    if not files:
        raise Klaida('LiDAR duomenų šiam plotui nerasta')

    # --- aplankas
    outroot = a.isvestis or os.path.join(os.path.expanduser('~'), 'Downloads')
    out = os.path.join(outroot, 'LiDAR_' + name)
    if os.path.exists(out):
        out += '_' + datetime.datetime.now().strftime('%Y%m%d_%H%M')
    os.makedirs(out, exist_ok=True)

    # --- apkarpymas
    log('Apkarpoma pagal plotą + %g m…' % a.buferis)
    G = []; parts_info = []; n_all = 0
    for f in files:
        lbl = f.get('dalis') or os.path.splitext(os.path.basename(f['failas']))[0]
        out_laz = None if a.be_laz else os.path.join(out, '%s_%s%s_apkarpyta.laz' % (name, lbl, '_%d' % f['metai'] if f.get('metai') else ''))
        g, n, inf = clip_laz(f['failas'], rings, a.buferis, bbox, out_laz)
        if out_laz and n == 0 and os.path.exists(out_laz):
            os.remove(out_laz)
        log('   %s: zonoje %d taškų, iš jų žemės (2 kl.) %d' % (lbl, n, len(g)))
        if len(g):
            G.append(g)
        n_all += n; inf.update({'dalis': lbl, 'metai': f.get('metai'), 'url': f.get('url'), 'zemes_tasku': int(len(g))}); parts_info.append(inf)
    if not G:
        raise Klaida('Plote nėra žemės (2 klasės) taškų')
    G = np.vstack(G)
    dens = len(G) / max(1.0, area + 2 * a.buferis * math.sqrt(area) * 4)
    log('Žemės taškų iš viso: %d (≈ %.1f tšk./m²)' % (len(G), dens))

    # --- DTM
    log('Sudaromas DTM %.2f m…' % a.gardele)
    D = build_dtm(G, bbox, a.gardele, rings, a.buferis, a.max_tarpas)
    Zin = D['Z'][~np.isnan(D['Z'])]
    log('   H %.2f – %.2f m, tarpų užpildyta %d langelių' % (Zin.min(), Zin.max(), D['uzpildyta']))

    # --- horizontalės ir taškai
    log('Skaičiuojamos horizontalės kas %g m…' % a.intervalas)
    cont = contours(D, a.intervalas, rings, a.lyginimas, simplify=0.05, min_len=a.min_ilgis, rounds=a.apvalinimas)
    pts = grid_points(D, rings, a.taskai) if a.taskai > 0 else None
    if pts is not None and len(pts[0]) > 200000:
        raise Klaida('Per daug taškų (%d) – pasirinkite retesnį --taskai' % len(pts[0]))
    years = sorted({p['metai'] for p in parts_info if p.get('metai')})
    today = datetime.date.today().isoformat()
    info = ['LiDAR reljefas: %s' % src,
            'Šaltinis: Lidar_DR_LT (geoportal.lt) %s, žemės taškai (2 klasė), %d tšk.' % ('/'.join(map(str, years)) or 'vietiniai', len(G)),
            'DTM %.2f m (vidurkis langelyje, tarpai ≤ %g m užpildyti TIN); horizontalės kas %g m%s' % (a.gardele, a.max_tarpas, a.intervalas, (', lygintos %g m, suapvalintos, trumpesnės nei %g m praleistos' % (a.lyginimas, a.min_ilgis)) if a.lyginimas > 0 else ''),
            ('Taškai kas %g m: %d vnt. (sluoksnis TASKAI, Z = aukštis)' % (a.taskai, len(pts[0]))) if pts is not None else 'Taškai nededami',
            'LKS-94 (EPSG:3346); aukštis – kaip LiDAR faile (vertikali sistema faile nenurodyta)',
            '%s, %s. Orientacinė informacija, ne topografinė nuotrauka.' % (VERSION, today)]
    base = os.path.join(out, name)
    iv_s = ('%g' % a.intervalas).replace('.', '_'); st_s = ('%g' % a.taskai).replace('.', '_')
    dxf_path = '%s_horiz_%sm%s.dxf' % (base, iv_s, ('_taskai_%sm' % st_s) if pts is not None else '')
    write_dxf(dxf_path, rings, cont, pts, a.taskai, a.taskai, not a.be_uzrasu, info)
    log('   DXF: %d horizontalių, %d taškų' % (len(cont), len(pts[0]) if pts is not None else 0))
    meta = {'SALTINIS': 'Lidar_DR_LT ' + '/'.join(map(str, years)), 'GARDELE_M': a.gardele, 'METODAS': 'vidurkis + TIN tarpams',
            'KOORDINACIU_SISTEMA': 'LKS-94 (EPSG:3346)', 'NODATA': -9999, 'VERSIJA': VERSION, 'DATA': today}
    write_geotiff('%s_DTM_%sm.tif' % (base, ('%g' % a.gardele).replace('.', '_')), D, meta)
    if pts is not None:
        with open('%s_taskai_%sm.xyz' % (base, st_s), 'w', encoding='utf-8') as f:
            f.write('# X(siaure) Y(rytai) H  -  LKS-94, Lidar_DR_LT\n')
            for x, y, h in zip(*pts):
                f.write('%.2f %.2f %.3f\n' % (y, x, h))
        with open('%s_taskai_%sm.csv' % (base, st_s), 'w', encoding='utf-8-sig') as f:
            f.write('Nr;X (šiaurė);Y (rytai);H\n')
            for i, (x, y, h) in enumerate(zip(*pts), 1):
                f.write('%d;%s;%s;%s\n' % (i, ('%.2f' % y).replace('.', ','), ('%.2f' % x).replace('.', ','), ('%.3f' % h).replace('.', ',')))
    rep = {'versija': VERSION, 'data': today, 'ribos_saltinis': src, 'plotas_ha': round(area / 1e4, 4), 'buferis_m': a.buferis,
           'dalys': parts_info, 'tasku_zonoje': int(n_all), 'zemes_tasku': int(len(G)), 'gardele_m': a.gardele,
           'H_min': round(float(Zin.min()), 3), 'H_max': round(float(Zin.max()), 3), 'H_vid': round(float(Zin.mean()), 3),
           'horizontaliu_intervalas_m': a.intervalas, 'horizontaliu': len(cont), 'tasku_tankumas_m': a.taskai,
           'tasku': int(len(pts[0])) if pts is not None else 0, 'trukme_s': round(time.time() - t0, 1)}
    json.dump(rep, open(base + '_ataskaita.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    with open(base + '_ataskaita.txt', 'w', encoding='utf-8-sig') as f:
        f.write('LiDAR reljefas – %s\n%s\n\n' % (name, VERSION))
        f.write('Ribos: %s\nPlotas: %.4f ha, buferis %g m\n' % (src, area / 1e4, a.buferis))
        f.write('LiDAR dalys:\n' + ''.join('  %s – %s m., zonoje %s tšk., žemės %s\n' % (p['dalis'], p.get('metai') or '?', p.get('taskai_zonoje', 0), p['zemes_tasku']) for p in parts_info))
        f.write('Aukščiai: %.2f – %.2f m (vid. %.2f)\nDTM gardelė %.2f m; horizontalės kas %g m (%d); taškai kas %g m (%d)\n' %
                (Zin.min(), Zin.max(), Zin.mean(), a.gardele, a.intervalas, len(cont), a.taskai, rep['tasku']))
        f.write('\nAukščiai – iš LiDAR žemės taškų (2 klasė), koordinatės LKS-94. Vertikali sistema LiDAR faile nenurodyta.\n'
                'Tai orientacinė informacija, ne topografinė nuotrauka.\n')
    log('-' * 64)
    log('BAIGTA per %.0f s. Rezultatai: %s' % (time.time() - t0, out))
    if a.atidaryti and hasattr(os, 'startfile'):
        os.startfile(out)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Klaida as e:
        log('\nKLAIDA: %s' % e); sys.exit(2)
    except KeyboardInterrupt:
        log('\nNutraukta. Paleidus iš naujo, parsisiuntimas bus tęsiamas.'); sys.exit(3)
