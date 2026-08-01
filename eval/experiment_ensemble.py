"""Does self-consistency (median of 3 passes) beat a single analysis pass?

Reuses the latest single-pass predictions as pass 1, runs two more passes,
and scores per-photo median totals against ground truth. If the gain is
real, production can run passes concurrently so latency barely moves.

Usage: ./venv/bin/python eval/experiment_ensemble.py eval/manifest.json
"""
import json
import os
import statistics
import sys
import time

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import analyze_food  # noqa: E402

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(EVAL_DIR, 'results')
PASSES = 3
FIELDS = ('calories', 'protein_g', 'carbs_g', 'fat_g')


def run_pass(photo_path):
    image = Image.open(photo_path)
    image.load()
    return analyze_food(image)['totals']


def mape(rows, key):
    apes = [abs(r[key] - r['truth']) / r['truth'] * 100 for r in rows if r[key] is not None and r['truth']]
    return round(statistics.mean(apes), 1)


def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(EVAL_DIR, 'manifest.json')
    with open(manifest_path) as f:
        manifest = json.load(f)
    with open(os.path.join(RESULTS_DIR, 'manifest-predictions.json')) as f:
        pass1 = json.load(f)

    cache_path = os.path.join(RESULTS_DIR, 'ensemble-passes.json')
    extra = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            extra = json.load(f)

    rows = []
    try:
        for i, entry in enumerate(manifest):
            photo = entry['photo']
            if photo not in pass1:
                continue
            passes = extra.setdefault(photo, [])
            while len(passes) < PASSES - 1:
                path = os.path.join(EVAL_DIR, photo)
                print(f"[{i + 1}/{len(manifest)}] {photo} pass {len(passes) + 2} ... ", flush=True)
                try:
                    passes.append(run_pass(path))
                except Exception as e:
                    print(f"  failed: {e}")
                    break
                time.sleep(3)
            totals = [pass1[photo]['totals']] + passes
            cals = [t['calories'] for t in totals if t.get('calories') is not None]
            if not cals:
                continue
            rows.append({
                'truth': entry['calories'],
                'single': totals[0]['calories'],
                'median3': statistics.median(cals) if len(cals) == PASSES else None,
            })
    finally:
        with open(cache_path, 'w') as f:
            json.dump(extra, f, indent=2)

    complete = [r for r in rows if r['median3'] is not None]
    print(f"\n{len(complete)} dishes with all {PASSES} passes")
    print(f"single-pass calorie MAPE:  {mape(complete, 'single')}%")
    print(f"median-of-{PASSES} calorie MAPE: {mape(complete, 'median3')}%")


if __name__ == '__main__':
    main()
