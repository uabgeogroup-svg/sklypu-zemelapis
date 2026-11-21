# -*- coding: utf-8 -*-
"""Sukuria nedidelį bandomąjį LAZ ir ribą (.exe patikrai GitHub Actions)."""
import json, os, sys
import numpy as np, laspy

out = sys.argv[1] if len(sys.argv) > 1 else 'ci'
os.makedirs(out, exist_ok=True)
rng = np.random.default_rng(1)
x0, y0 = 596150.0, 6073250.0
N = 120000
x = x0 + rng.random(N) * 300; y = y0 + rng.random(N) * 250
cls = np.where(rng.random(N) < 0.8, 2, 5).astype(np.uint8)
z = 150 + 0.03 * (x - 596000) + 0.02 * (y - 6073000) + 2 * np.sin(x / 40)
z = z + np.where(cls == 2, rng.normal(0, 0.02, N), rng.random(N) * 10 + 0.5)
h = laspy.LasHeader(point_format=6, version='1.4'); h.offsets = [x0, y0, 0]; h.scales = [0.01, 0.01, 0.01]
las = laspy.LasData(h); las.x = x; las.y = y; las.z = z; las.classification = cls
las.write(os.path.join(out, 'bandymas.laz'))
ring = [[596200, 6073300], [596400, 6073300], [596400, 6073450], [596200, 6073450], [596200, 6073300]]
json.dump({'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {},
          'geometry': {'type': 'Polygon', 'coordinates': [ring]}}]}, open(os.path.join(out, 'riba.geojson'), 'w'))
print('ok', out)
