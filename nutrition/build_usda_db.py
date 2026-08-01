"""Build a local USDA nutrition database for grounded calorie estimation.

Downloads USDA FoodData Central's SR Legacy dataset (~7,800 foods with
lab-analyzed per-100g nutrition) and builds nutrition/usda.sqlite with an
FTS5 index over food descriptions, so the app can match "grilled chicken
breast" to real nutrition facts with no API calls or rate limits.

Usage: ./venv/bin/python nutrition/build_usda_db.py
"""
import csv
import io
import os
import sqlite3
import sys
import urllib.request
import zipfile

URL = 'https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip'
DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, 'usda.sqlite')

# FDC nutrient ids -> our column names (per 100 g)
NUTRIENTS = {'1008': 'kcal', '1003': 'protein_g', '1005': 'carbs_g', '1004': 'fat_g'}


def read_csv(zf, name):
    # The zip nests files under a folder whose name varies; match exact basename
    member = next(n for n in zf.namelist() if n.rsplit('/', 1)[-1] == name)
    with zf.open(member) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))


def main():
    print(f'Downloading SR Legacy dataset ({URL.rsplit("/", 1)[1]}) ...')
    with urllib.request.urlopen(URL, timeout=120) as r:
        data = r.read()
    zf = zipfile.ZipFile(io.BytesIO(data))

    print('Parsing foods ...')
    foods = {}
    for row in read_csv(zf, 'food.csv'):
        if row['data_type'] == 'sr_legacy_food':
            foods[row['fdc_id']] = {'description': row['description']}

    print('Parsing nutrients ...')
    for row in read_csv(zf, 'food_nutrient.csv'):
        food = foods.get(row['fdc_id'])
        col = NUTRIENTS.get(row['nutrient_id'])
        if food is not None and col and col not in food:
            try:
                food[col] = float(row['amount'])
            except ValueError:
                pass

    complete = [f for f in foods.values() if 'kcal' in f]
    print(f'{len(complete)} foods with energy data')

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.execute('''CREATE TABLE foods (
        id INTEGER PRIMARY KEY, description TEXT,
        kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL)''')
    db.execute('CREATE VIRTUAL TABLE foods_fts USING fts5(description, content=foods, content_rowid=id)')
    for f in complete:
        cur = db.execute(
            'INSERT INTO foods (description, kcal, protein_g, carbs_g, fat_g) VALUES (?, ?, ?, ?, ?)',
            (f['description'], f['kcal'], f.get('protein_g'), f.get('carbs_g'), f.get('fat_g')))
        db.execute('INSERT INTO foods_fts (rowid, description) VALUES (?, ?)',
                   (cur.lastrowid, f['description']))
    db.commit()
    db.close()
    size_mb = os.path.getsize(DB_PATH) / 1e6
    print(f'Wrote {DB_PATH} ({size_mb:.1f} MB)')


if __name__ == '__main__':
    main()
