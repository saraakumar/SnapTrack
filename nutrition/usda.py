"""Match a food description to USDA SR Legacy nutrition facts.

lookup() returns per-100g facts plus a match quality signal the caller can
use to decide whether to trust the match over the LLM's own estimate.
"""
import os
import re
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'usda.sqlite')

_local = threading.local()


def available():
    return os.path.exists(DB_PATH)


def _db():
    if getattr(_local, 'db', None) is None:
        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        _local.db = conn
    return _local.db


def _tokens(text):
    return [t for t in re.findall(r'[a-z]+', text.lower()) if len(t) > 2]


def lookup(query):
    """Best USDA match for a food query, or None.

    Returns {description, kcal, protein_g, carbs_g, fat_g, overlap} where
    nutrition is per 100 g and overlap is the fraction of query tokens found
    in the matched description (1.0 = every word matched).
    """
    if not available():
        return None
    tokens = _tokens(query)
    if not tokens:
        return None

    # OR of prefix-matches so singular/plural forms both hit
    # ("carrot*" finds "Carrots, raw"); bm25 ranks the best description.
    variants = {t.rstrip('s') for t in tokens}
    fts_query = ' OR '.join(f'"{v}"*' for v in variants if len(v) > 2)
    try:
        rows = _db().execute(
            'SELECT rowid, description, bm25(foods_fts) AS rank FROM foods_fts '
            'WHERE foods_fts MATCH ? ORDER BY rank LIMIT 25', (fts_query,)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None

    def coverage(row):
        desc = row['description'].lower()
        return sum(1 for t in tokens if t in desc or t.rstrip('s') in desc) / len(tokens)

    # A match to a different PREPARATION is worse than a weaker name match:
    # dried/flour/raw variants can be 3-8x the energy density of the food
    # the query meant. Penalize such modifiers when the query didn't ask.
    HAZARDS = ('dried', 'dehydrated', 'powder', 'flour', 'dry', 'raw',
               'uncooked', 'leaves', 'frozen', 'concentrate', 'babyfood')

    def score(row):
        desc = row['description'].lower()
        penalty = sum(0.25 for h in HAZARDS if h in desc and h not in tokens)
        # USDA names lead with the food's identity ("Noodles, egg, ...") -
        # identity words the query never said mean it's a different food
        head = _tokens(desc.split(',')[0])
        variants_all = {v for t in tokens for v in (t, t.rstrip('s'))}
        penalty += sum(0.3 for h in head if h not in variants_all and h.rstrip('s') not in variants_all)
        return coverage(row) - penalty

    best = max(rows, key=lambda r: (score(r), -r['rank']))
    food = _db().execute('SELECT * FROM foods WHERE id = ?', (best['rowid'],)).fetchone()
    return {
        'description': food['description'],
        'kcal': food['kcal'],
        'protein_g': food['protein_g'],
        'carbs_g': food['carbs_g'],
        'fat_g': food['fat_g'],
        'overlap': round(coverage(best), 2),
    }
