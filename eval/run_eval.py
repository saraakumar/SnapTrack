"""Accuracy eval for SnapTrack's photo analysis.

Runs the app's real analyze_food() pipeline over a manifest of photos with
known nutrition and reports calorie/macro error. See eval/README.md.

Usage:
    ./venv/bin/python eval/run_eval.py eval/manifest.json
    ./venv/bin/python eval/run_eval.py eval/manifest.json --limit 5   # first N photos
    ./venv/bin/python eval/run_eval.py eval/manifest.json --fresh     # ignore cached results
"""
import argparse
import json
import os
import statistics
import sys
import time

from PIL import Image

# Import the production analysis path from the app itself, so the eval
# measures exactly what users get
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import analyze_food, client  # noqa: E402

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(EVAL_DIR, 'results')

MACROS = ('protein_g', 'carbs_g', 'fat_g')
SECONDS_BETWEEN_CALLS = 3  # stay friendly to the free-tier rate limit


def load_manifest(path):
    with open(path) as f:
        entries = json.load(f)
    for e in entries:
        missing = [k for k in ('photo', 'calories') if k not in e]
        if missing:
            raise SystemExit(f"Manifest entry {e} is missing required keys: {missing}")
    return entries


def analyze_photo(photo_path):
    image = Image.open(photo_path)
    image.load()
    result = analyze_food(image)
    return {
        'items': [i['description'] for i in result['items']],
        'totals': result['totals'],
    }


def pct_error(predicted, truth):
    """Absolute percentage error; None when it can't be computed"""
    if predicted is None or truth is None or truth == 0:
        return None
    return abs(predicted - truth) / truth * 100


def evaluate(entries, cache, fresh=False):
    rows = []
    for i, entry in enumerate(entries):
        photo = entry['photo']
        photo_path = photo if os.path.isabs(photo) else os.path.join(EVAL_DIR, photo)
        if not os.path.exists(photo_path):
            print(f"[{i + 1}/{len(entries)}] SKIP {photo} (file not found)")
            continue

        if not fresh and photo in cache:
            prediction = cache[photo]
            print(f"[{i + 1}/{len(entries)}] {photo} (cached)")
        else:
            print(f"[{i + 1}/{len(entries)}] {photo} ... ", end='', flush=True)
            try:
                prediction = analyze_photo(photo_path)
            except Exception as e:
                print(f"FAILED: {e}")
                continue
            cache[photo] = prediction
            print(f"{round(prediction['totals']['calories'] or 0)} kcal predicted")
            time.sleep(SECONDS_BETWEEN_CALLS)

        row = {
            'photo': photo,
            'label': entry.get('label', ''),
            'truth_calories': entry['calories'],
            'pred_calories': prediction['totals'].get('calories'),
            'pred_items': prediction['items'],
            'calorie_ape': pct_error(prediction['totals'].get('calories'), entry['calories']),
        }
        for macro in MACROS:
            row[f'truth_{macro}'] = entry.get(macro)
            row[f'pred_{macro}'] = prediction['totals'].get(macro)
            row[f'{macro}_ape'] = pct_error(prediction['totals'].get(macro), entry.get(macro))
        rows.append(row)
    return rows


def summarize(rows):
    apes = [r['calorie_ape'] for r in rows if r['calorie_ape'] is not None]
    if not apes:
        return None
    abs_errors = [abs(r['pred_calories'] - r['truth_calories'])
                  for r in rows if r['pred_calories'] is not None]
    summary = {
        'photos': len(rows),
        'calorie_mape': round(statistics.mean(apes), 1),
        'calorie_median_ape': round(statistics.median(apes), 1),
        'calorie_mae_kcal': round(statistics.mean(abs_errors), 1),
        'within_20_pct': round(100 * sum(a <= 20 for a in apes) / len(apes), 1),
        'within_30_pct': round(100 * sum(a <= 30 for a in apes) / len(apes), 1),
    }
    for macro in MACROS:
        macro_apes = [r[f'{macro}_ape'] for r in rows if r[f'{macro}_ape'] is not None]
        if macro_apes:
            summary[f'{macro}_mape'] = round(statistics.mean(macro_apes), 1)
    return summary


def write_report(rows, summary, path):
    lines = [
        '# SnapTrack accuracy eval',
        '',
        f"Photos evaluated: **{summary['photos']}**",
        '',
        '| Metric | Value |',
        '|---|---|',
        f"| Calorie MAPE | {summary['calorie_mape']}% |",
        f"| Calorie median APE | {summary['calorie_median_ape']}% |",
        f"| Calorie MAE | {summary['calorie_mae_kcal']} kcal |",
        f"| Within 20% of truth | {summary['within_20_pct']}% of photos |",
        f"| Within 30% of truth | {summary['within_30_pct']}% of photos |",
    ]
    for macro in MACROS:
        if f'{macro}_mape' in summary:
            lines.append(f"| {macro.replace('_g', '').title()} MAPE | {summary[f'{macro}_mape']}% |")
    lines += ['', '## Per-photo results', '',
              '| Photo | Label | Truth kcal | Predicted kcal | Error |', '|---|---|---|---|---|']
    for r in sorted(rows, key=lambda r: -(r['calorie_ape'] or 0)):
        pred = round(r['pred_calories']) if r['pred_calories'] is not None else '—'
        ape = f"{round(r['calorie_ape'])}%" if r['calorie_ape'] is not None else '—'
        lines.append(f"| {r['photo']} | {r['label']} | {r['truth_calories']} | {pred} | {ape} |")
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', help='JSON manifest of photos with known nutrition')
    parser.add_argument('--limit', type=int, help='only evaluate the first N photos')
    parser.add_argument('--fresh', action='store_true', help='re-analyze even if cached')
    args = parser.parse_args()

    if client is None:
        raise SystemExit('GEMINI_API_KEY is not set - add it to .env first')

    entries = load_manifest(args.manifest)
    if args.limit:
        entries = entries[:args.limit]

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.manifest))[0]
    cache_path = os.path.join(RESULTS_DIR, f'{stem}-predictions.json')
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cache = json.load(f)

    try:
        rows = evaluate(entries, cache, fresh=args.fresh)
    finally:
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)

    summary = summarize(rows)
    if not summary:
        raise SystemExit('No results to summarize (no photos analyzed successfully).')

    report_path = os.path.join(RESULTS_DIR, f'{stem}-report.md')
    write_report(rows, summary, report_path)

    print('\n=== Summary ===')
    for key, value in summary.items():
        print(f'{key:>22}: {value}')
    print(f'\nFull report: {report_path}')


if __name__ == '__main__':
    main()
