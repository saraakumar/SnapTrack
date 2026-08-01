"""Build an eval manifest from Google Research's Nutrition5k dataset.

Nutrition5k (CC BY 4.0, research.google/tools/datasets/nutrition5k) is real
cafeteria dishes with nutrition measured from per-ingredient scale weights -
proper ground truth, not estimates. This pulls N overhead RGB photos spread
across the calorie range plus their measured values, and writes
eval/manifest.json ready for run_eval.py.

Usage:
    ./venv/bin/python eval/fetch_nutrition5k.py            # 50 dishes
    ./venv/bin/python eval/fetch_nutrition5k.py --count 20
"""
import argparse
import csv
import io
import json
import os
import urllib.request

BASE = 'https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset'
EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.join(EVAL_DIR, 'photos')


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def load_dishes():
    """Parse dish metadata: dish_id, totals, then repeating ingredient groups"""
    dishes = []
    for cafe in ('cafe1', 'cafe2'):
        raw = fetch(f'{BASE}/metadata/dish_metadata_{cafe}.csv').decode()
        for row in csv.reader(io.StringIO(raw)):
            if len(row) < 6 or not row[0].startswith('dish_'):
                continue
            try:
                dish = {
                    'id': row[0],
                    'calories': round(float(row[1]), 1),
                    'mass_g': round(float(row[2]), 1),
                    'fat_g': round(float(row[3]), 1),
                    'carbs_g': round(float(row[4]), 1),
                    'protein_g': round(float(row[5]), 1),
                    # ingredient groups of 7 fields start at col 6; name is 2nd
                    'ingredients': [row[i] for i in range(7, len(row), 7) if i < len(row)],
                }
            except ValueError:
                continue
            # Skip degenerate entries (empty plates, sensor glitches)
            if 50 <= dish['calories'] <= 1500 and dish['mass_g'] >= 40:
                dishes.append(dish)
    return dishes


def download_photo(dish):
    """Fetch the overhead RGB capture; False if this dish doesn't have one"""
    path = os.path.join(PHOTO_DIR, f"{dish['id']}.jpg")
    if os.path.exists(path):
        return True
    try:
        data = fetch(f"{BASE}/imagery/realsense_overhead/{dish['id']}/rgb.png")
    except Exception:
        return False
    with open(path, 'wb') as f:
        f.write(data)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=50)
    args = parser.parse_args()

    os.makedirs(PHOTO_DIR, exist_ok=True)
    dishes = load_dishes()
    print(f'{len(dishes)} usable dishes in metadata')

    # One calorie bin per requested dish, so the eval spans the full range
    # instead of clustering where the dataset is dense; within a bin, try
    # dishes in order until one has an overhead photo
    dishes.sort(key=lambda d: d['calories'])
    bin_size = len(dishes) / args.count
    bins = [dishes[int(i * bin_size):int((i + 1) * bin_size)] for i in range(args.count)]

    manifest = []
    for bin_dishes in bins:
        for dish in bin_dishes:
            if not download_photo(dish):
                continue
            ingredients = ', '.join(dish['ingredients'][:5])
            manifest.append({
                'photo': f"photos/{dish['id']}.jpg",
                'label': ingredients or dish['id'],
                'calories': dish['calories'],
                'protein_g': dish['protein_g'],
                'carbs_g': dish['carbs_g'],
                'fat_g': dish['fat_g'],
                'source': f"Nutrition5k {dish['id']} (per-ingredient scale weights)",
            })
            print(f"[{len(manifest)}/{args.count}] {dish['id']}  {dish['calories']} kcal  ({ingredients[:60]})")
            break

    out = os.path.join(EVAL_DIR, 'manifest.json')
    with open(out, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f'\nWrote {len(manifest)} entries to {out}')


if __name__ == '__main__':
    main()
