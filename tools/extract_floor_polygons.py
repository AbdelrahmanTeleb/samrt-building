"""Derive room polygons from the supplied floor-plan image (colour segmentation).

Run: python tools/extract_floor_polygons.py   (needs requirements-tools.txt)
Writes app/data/map_config.json (normalised 0..1 coordinates). Room numbers and
names were read by eye from the plan and are entered in LABELS below; anything
not listed is deliberately left unmapped.
"""
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "static/images/university_floor_1_plan.png"
# (room number, label, type, approx x, approx y, unused) - matched to the nearest coloured region.
LABELS = [
    ("01.624", "SR 2", "seminar", 396, 128, 0), ("01.625", "SR 3", "seminar", 513, 126, 0),
    ("01.629", "SR 4", "seminar", 573, 132, 0), ("01.630", "SR 5", "seminar", 633, 137, 0),
    ("01.632", "SR 6", "seminar", 693, 141, 0), ("01.634", "HS 6", "lecture_hall", 558, 303, 0),
    ("01.303", "HS 7", "lecture_hall", 662, 466, 0), ("01.305", "HS 8", "lecture_hall", 540, 476, 0),
    ("01.314", "HS 9", "lecture_hall", 303, 496, 0), ("MENSA", "Mensa", "canteen", 195, 325, 0),
]
CAPACITY = {}  # room_number -> seats. Empty on purpose: real capacities are unknown.


def polygon_for(mask):
    import cv2
    mask = ndi.binary_closing(mask, iterations=6).astype(np.uint8)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cnts, key=cv2.contourArea)
    return cv2.approxPolyDP(c, 0.012 * cv2.arcLength(c, True), True).reshape(-1, 2)


def main():
    img = Image.open(PLAN).convert("RGB")
    w, h = img.size
    a = np.array(img).astype(int)
    regions = []
    for target in (a[130, 450], a[270, 100]):  # blue room fill, green canteen fill
        lab, n = ndi.label(np.abs(a - target).sum(axis=2) < 54)
        for i in range(1, n + 1):
            ys, xs = np.where(lab == i)
            if len(ys) >= 1500:
                regions.append((xs.mean(), ys.mean(), lab == i))
    rooms = []
    for num, name, rtype, cx, cy, _ in LABELS:
        best = min(regions, key=lambda r: (r[0] - cx) ** 2 + (r[1] - cy) ** 2)
        poly = polygon_for(best[2])
        rooms.append({"room_number": num, "label": name, "room_type": rtype, "capacity": CAPACITY.get(num),
                      "polygon": [[round(float(x) / w, 5), round(float(y) / h, 5)] for x, y in poly],
                      "geometry_source": "colour-segmented from supplied plan; verify visually"})
    cfg = {
        "campus": {"image": "/static/images/university_campus_map.jpg", "width": 609, "height": 401,
                   "buildings": [
                       # Letters are printed on the supplied campus drawing; x,y are where the letters sit.
                       # Which real building each letter is, and where OMP1 sits, is NOT stated in the image.
                       {"code": c, "name": f"Building {c}", "x": round(x / 609, 4), "y": round(y / 401, 4)}
                       for c, x, y in [("A", 545, 150), ("B", 240, 240), ("C", 100, 135),
                                       ("D", 368, 96), ("G", 177, 48), ("N", 236, 37)]]},
        "floor_plan": {"image": "/static/images/university_floor_1_plan.png", "width": w, "height": h,
                       "title": "1 OG - Oskar-Morgenstern-Platz 1", "building_code": None, "rooms": rooms},
    }
    (ROOT / "app/data/map_config.json").write_text(json.dumps(cfg, indent=1))
    print(f"wrote {len(rooms)} room polygons")


if __name__ == "__main__":
    main()
