import base64
import io
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta

import secrets

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, render_template_string
from google import genai
from google.genai import types
from PIL import Image

from nutrition import usda

load_dotenv()

# Postgres in production (DATABASE_URL set, e.g. Neon), SQLite for local dev
DATABASE_URL = os.environ.get('DATABASE_URL')
IS_POSTGRES = bool(DATABASE_URL)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'snaptrack.db')

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row


def get_db():
    if IS_POSTGRES:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql):
    """Translate '?' placeholders to Postgres '%s' when needed"""
    return sql.replace('?', '%s') if IS_POSTGRES else sql


def init_db():
    with get_db() as db:
        id_col = 'SERIAL PRIMARY KEY' if IS_POSTGRES else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        db.execute(f'''
            CREATE TABLE IF NOT EXISTS meals (
                id {id_col},
                created_at TEXT NOT NULL,
                summary TEXT,
                items TEXT NOT NULL,
                calories REAL,
                protein_g REAL,
                carbs_g REAL,
                fat_g REAL,
                thumbnail TEXT,
                meal_type TEXT
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        if not IS_POSTGRES:
            # Migration for local SQLite databases created before meal_type existed
            cols = [r[1] for r in db.execute('PRAGMA table_info(meals)').fetchall()]
            if 'meal_type' not in cols:
                db.execute('ALTER TABLE meals ADD COLUMN meal_type TEXT')

        db.execute(f'''
            CREATE TABLE IF NOT EXISTS analysis_requests (
                id {id_col},
                created_at TEXT NOT NULL,
                model TEXT,
                retried INTEGER NOT NULL DEFAULT 0,
                had_correction INTEGER NOT NULL DEFAULT 0,
                latency_ms REAL,
                item_count INTEGER,
                grounded_count INTEGER,
                min_confidence REAL,
                error TEXT
            )
        ''')
        db.execute(f'''
            CREATE TABLE IF NOT EXISTS corrections (
                id {id_col},
                created_at TEXT NOT NULL,
                original_description TEXT NOT NULL,
                corrected_description TEXT NOT NULL
            )
        ''')


init_db()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# Set SECRET_KEY in production so sessions survive restarts
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

# When APP_PASSWORD is set (i.e. deployed publicly), the whole app requires it:
# humans get a login page, machine clients send it as an X-App-Key header
APP_PASSWORD = os.environ.get('APP_PASSWORD')

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SnapTrack - Sign in</title>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@600;700;800&display=swap" rel="stylesheet">
<style>
body { font-family: 'Nunito', sans-serif; background: #edf5ec; color: #16302a;
       min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
form { background: #fff; border: 1px solid #dde8dd; border-radius: 20px; padding: 32px; width: 100%; max-width: 380px; text-align: center; }
h1 { font-size: 1.4em; margin-bottom: 4px; } h1 span { color: #6D9773; }
p { color: #5f7268; font-size: 0.9em; margin-bottom: 20px; }
input { width: 100%; padding: 14px; border: 1px solid #dde8dd; border-radius: 12px; font-size: 1em; margin-bottom: 12px; font-family: inherit; }
button { width: 100%; padding: 14px; border: none; border-radius: 12px; background: #6D9773; color: #fff; font-weight: 700; font-size: 1em; cursor: pointer; font-family: inherit; }
.err { color: #d94f3d; font-size: 0.9em; margin-bottom: 12px; }
</style></head><body>
<form method="post">
  <h1>SnapTrack<span>.</span></h1>
  <p>Enter the access code to continue</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <input type="password" name="password" placeholder="Access code" autofocus>
  <button type="submit">Sign in</button>
</form></body></html>"""


# The Mentra glasses miniapp's WebView runs on a different origin than this
# app (unlike the normal web UI, which fetches itself), so its requests are
# cross-origin and need CORS headers or the browser blocks them outright
# (surfaces as a generic "TypeError: Load failed", no server-side signal at
# all since the browser never lets the request through).
@app.before_request
def handle_cors_preflight():
    # A preflight carries no auth and must never hit the access gate below.
    if request.method == 'OPTIONS':
        return '', 204


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-App-Key'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
    return response


@app.before_request
def require_access():
    if not APP_PASSWORD:
        return  # local development, no gate
    if request.endpoint in ('login', 'static'):
        return
    if session.get('authed'):
        return
    if request.headers.get('X-App-Key') == APP_PASSWORD:
        return
    if request.path.startswith('/api/') or request.path == '/upload':
        return jsonify({'error': 'Unauthorized'}), 401
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if secrets.compare_digest(request.form.get('password', ''), APP_PASSWORD or ''):
            session['authed'] = True
            session.permanent = True
            return redirect(url_for('index'))
        error = 'Wrong access code'
    return render_template_string(LOGIN_PAGE, error=error)

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
# 25s hard timeout per API call so a stuck request can't hang the app;
# retries disabled - we handle fallback ourselves by switching models
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=25_000, retry_options=types.HttpRetryOptions(attempts=1)),
) if GEMINI_API_KEY else None
if client:
    print(f"[STARTUP] Gemini configured with key: {GEMINI_API_KEY[:12]}...")
else:
    print("[STARTUP] GEMINI_API_KEY not set - uploads will fail until it is "
          "(add it to .env; get a key at https://aistudio.google.com/apikey)")

# gemini-2.5-* models are closed to new accounts. flash-lite is primary:
# it answers in ~3s where 3.5-flash regularly exceeds 25s on this task,
# and 3.5-flash's free tier is only 20 requests/day anyway
MODEL_CANDIDATES = ['gemini-3.1-flash-lite', 'gemini-3.5-flash']

ANALYSIS_PROMPT = """You are a food identification and nutrition expert. Analyze this food image CAREFULLY.

CRITICAL RULES:
1. ONLY describe what you can ACTUALLY SEE in the image. Do NOT guess or assume items that aren't visible.
2. Be specific about ingredients. NEVER use generic terms like "burger" alone - describe what's visible on/in it.
3. Estimate the portion size from visual cues (plate size, item dimensions) and base nutrition estimates on that portion.
4. Nutrition values are estimates for the ENTIRE visible portion of each item, not per 100g.
5. Work in two steps: first estimate each item's MASS in grams from visual cues, then compute
   calories as mass x that food's typical energy density (kcal per 100g). Do not shortcut to a
   "typical serving" calorie count - the visible portion is often much smaller or larger than typical.
6. Commit to extreme values when the food warrants it. A plain vegetable plate can be under 100 kcal
   total - do not inflate it toward a "normal meal". Calorie-dense foods (nuts, seeds, oils, dressings,
   nut butters) pack hundreds of kcal into a small volume - do not deflate them.
7. Distinguish look-alikes that differ hugely in calories: egg whites vs whole eggs, dressed vs
   undressed salad, oil-glossed vs dry-cooked vegetables. When such a detail is visible, use it.
8. For each item give "usda_name": the plain generic food name plus preparation, the way a
   nutrition database would list it ("chicken breast grilled", "egg white cooked", "rice white
   cooked"). No brand names, no adjectives like "delicious", no ingredient lists.
   IMPORTANT: only for SINGLE foods. If the item is a composite of mixed ingredients (a dressed
   salad, sandwich, stir-fry, casserole, smoothie), set usda_name to "" - no single database
   entry can represent it, and your own estimate is better.

Each distinct food item gets its own entry in "items". If the image contains no food,
return an empty items list and explain what the image shows in "summary"."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "detailed description of the food item with visible ingredients"},
                    "portion": {"type": "string", "description": "estimated portion, e.g. '1 whole 12-inch pizza' or '1 cup, approx 150g'"},
                    "mass_g": {"type": "number", "description": "estimated mass of the visible portion in grams"},
                    "usda_name": {"type": "string", "description": "plain database-style name with preparation, e.g. 'chicken breast grilled', 'egg white cooked', 'rice brown cooked'"},
                    "calories": {"type": "number"},
                    "protein_g": {"type": "number"},
                    "carbs_g": {"type": "number"},
                    "fat_g": {"type": "number"},
                    "confidence": {"type": "number", "description": "0-100"},
                },
                "required": ["name", "portion", "mass_g", "usda_name", "calories", "protein_g", "carbs_g", "fat_g", "confidence"],
            },
        },
        "summary": {"type": "string", "description": "2-3 sentence description of everything visible in the image"},
    },
    "required": ["items", "summary"],
}


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


def _to_number(value):
    """Coerce a model-provided value to a float, or None if not numeric"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Models that hit a rate/quota limit are skipped for a cooldown period
# instead of failing slowly on every request
_model_cooldown_until = {}
RATE_LIMIT_COOLDOWN_SECONDS = 600


def _is_rate_limit(error):
    text = str(error)
    return '429' in text or 'RESOURCE_EXHAUSTED' in text


_correction_hint_cache = {'key': None, 'hints': None}

# A one-off edit doesn't mean much; the same correction happening twice does
MIN_CORRECTION_PATTERN_COUNT = 2


def _correction_hints():
    """Foods the model has misidentified the same way more than once, mapped
    to what the user corrected them to. Rebuilt whenever the corrections
    table grows, otherwise served from cache."""
    with get_db() as db:
        rows = db.execute('SELECT original_description, corrected_description FROM corrections').fetchall()

    cache_key = len(rows)
    if _correction_hint_cache['key'] == cache_key:
        return _correction_hint_cache['hints']

    groups = {}
    for r in rows:
        key = r['original_description'].strip().lower()
        groups.setdefault(key, []).append(r['corrected_description'].strip())

    hints = {}
    for key, corrected_list in groups.items():
        if len(corrected_list) < MIN_CORRECTION_PATTERN_COUNT:
            continue
        counts = {}
        for c in corrected_list:
            counts[c] = counts.get(c, 0) + 1
        most_common = max(counts, key=counts.get)
        hints[key] = {'corrected': most_common, 'count': len(corrected_list)}

    _correction_hint_cache['key'] = cache_key
    _correction_hint_cache['hints'] = hints
    return hints


def _apply_correction_hints(items):
    """Flag items matching a known misidentification pattern so the UI can
    offer a one-tap fix instead of making the user type the correction again."""
    hints = _correction_hints()
    if not hints:
        return
    for item in items:
        hint = hints.get(item['description'].strip().lower())
        if hint:
            item['correction_hint'] = hint['corrected']
            item['confidence'] = min(item.get('confidence') or 90.0, 70.0)


def _record_correction(original, corrected):
    original = (original or '').strip()
    corrected = (corrected or '').strip()
    if not original or not corrected or original.lower() == corrected.lower():
        return
    try:
        with get_db() as db:
            db.execute(q(
                'INSERT INTO corrections (created_at, original_description, corrected_description) '
                'VALUES (?, ?, ?)'),
                (datetime.now().isoformat(timespec='seconds'), original, corrected))
    except Exception as e:
        app.logger.warning(f"Failed to record correction: {e}")


def _run_model(contents):
    """Run one Gemini analysis pass over `contents` (a prompt, or [prompt, image])
    and return the parsed result dict. Shared by the photo and text-only paths."""
    response = None
    last_error = None
    for model_name in MODEL_CANDIDATES:
        if _model_cooldown_until.get(model_name, 0) > time.time():
            app.logger.info(f"Skipping {model_name} (rate-limit cooldown)")
            continue
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',
                    response_schema=RESPONSE_SCHEMA,
                ),
            )
            break
        except Exception as e:
            app.logger.warning(f"{model_name} failed: {e}")
            last_error = e
            if _is_rate_limit(e):
                _model_cooldown_until[model_name] = time.time() + RATE_LIMIT_COOLDOWN_SECONDS
    if response is None:
        if last_error is None or _is_rate_limit(last_error):
            raise RuntimeError(
                'The free AI quota is temporarily used up. Wait a minute and try again.')
        raise last_error

    parsed = json.loads(response.text)

    detected_items = []
    for item in parsed.get('items', []):
        if not item.get('name'):
            continue
        detected_items.append({
            'description': item['name'],
            'portion': item.get('portion', ''),
            'mass_g': _to_number(item.get('mass_g')),
            'usda_name': (item.get('usda_name') or '').strip(),
            'calories': _to_number(item.get('calories')),
            'protein_g': _to_number(item.get('protein_g')),
            'carbs_g': _to_number(item.get('carbs_g')),
            'fat_g': _to_number(item.get('fat_g')),
            'confidence': _to_number(item.get('confidence')) or 90.0,
            'type': 'gemini',
        })

    _apply_correction_hints(detected_items)
    _ground_in_usda(detected_items)

    totals = {}
    for field in ('calories', 'protein_g', 'carbs_g', 'fat_g'):
        values = [i[field] for i in detected_items if i.get(field) is not None]
        totals[field] = round(sum(values), 1) if values else None

    return {
        'items': detected_items,
        'full_description': parsed.get('summary', ''),
        'totals': totals,
        'source': 'gemini',
        'model': model_name,
    }


# Trust a USDA match only when it covers at least this share of the query
USDA_MIN_OVERLAP = 0.67


def _ground_in_usda(items):
    """Replace LLM-invented nutrition with USDA facts where a confident match exists.

    The model does perception (identify the food, estimate grams); the USDA
    database supplies per-100g facts. Items keep the LLM numbers when there is
    no trustworthy match, and are marked with their source either way.
    """
    if not usda.available():
        return
    for item in items:
        mass = item.get('mass_g')
        query = item.get('usda_name')
        if not mass or mass <= 0 or not query:
            continue
        # Long queries mean the model is describing a composite dish despite
        # instructions - a single database row would misprice it badly
        if len(query.split()) > 4 or ' with ' in query.lower():
            continue
        match = usda.lookup(query)
        if not match or match['overlap'] < USDA_MIN_OVERLAP:
            continue
        factor = mass / 100.0
        item['calories'] = round(match['kcal'] * factor, 1)
        for field in ('protein_g', 'carbs_g', 'fat_g'):
            if match[field] is not None:
                item[field] = round(match[field] * factor, 1)
        item['type'] = 'usda'
        item['usda_match'] = match['description']


def _min_confidence(result):
    scores = [i['confidence'] for i in result['items'] if i.get('confidence') is not None]
    return min(scores) if scores else 0


def _log_analysis_request(latency_ms, had_correction, result=None, error=None):
    """Record one /upload call for the pipeline dashboard.

    Never allowed to break the actual response - a logging failure here
    is swallowed, not raised, since observability must not take down the
    feature it's observing.
    """
    try:
        items = result['items'] if result else []
        with get_db() as db:
            db.execute(q(
                'INSERT INTO analysis_requests '
                '(created_at, model, retried, had_correction, latency_ms, item_count, grounded_count, min_confidence, error) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'),
                (datetime.now().isoformat(timespec='seconds'),
                 result.get('model') if result else None,
                 int(bool(result.get('retried'))) if result else 0,
                 int(bool(had_correction)),
                 latency_ms,
                 len(items),
                 sum(1 for i in items if i.get('type') == 'usda'),
                 _min_confidence(result) if items else None,
                 error))
    except Exception as e:
        app.logger.warning(f"Failed to log analysis request: {e}")


def analyze_food(image, correction=None):
    """Analyze a meal photo; optionally honor a user correction of the identification.

    If the model reports low confidence (<90) on any item, re-run once and
    keep whichever pass it was more confident about.
    """
    prompt = ANALYSIS_PROMPT
    if correction:
        prompt += (
            "\n\nUSER CORRECTION - HIGHEST PRIORITY: The user has identified the food "
            f"themselves: \"{correction}\". Trust the user's identification over your own "
            "visual interpretation. Use the image only to estimate portion size and any "
            "details the user did not specify, and estimate nutrition for what the user says it is."
        )

    result = _run_model([prompt, image])
    result['retried'] = False

    if not correction and result['items'] and _min_confidence(result) < 90:
        app.logger.info("Low confidence result, re-running analysis once")
        try:
            second = _run_model([prompt, image])
            if _min_confidence(second) > _min_confidence(result):
                second['retried'] = True
                result = second
            else:
                result['retried'] = True
        except Exception as e:
            app.logger.warning(f"Confidence retry failed, keeping first result: {e}")

    return result


TEXT_ANALYSIS_PROMPT = """You are a food identification and nutrition expert. The user describes a \
meal in their own words, with no photo. Estimate its nutrition the same careful way you would from \
a photo:

1. Identify each distinct food item the description implies.
2. Estimate each item's MASS in grams from the description (typical serving sizes, explicit
   quantities if given, e.g. "2 eggs" or "a cup of rice").
3. Compute calories as mass x that food's typical energy density (kcal per 100g) - don't shortcut
   to a flat "typical serving" calorie count.
4. Commit to realistic values; don't pull everything toward an average meal.
5. For each item give "usda_name": the plain generic food name plus preparation, the way a
   nutrition database would list it ("chicken breast grilled", "egg white cooked"). No brand
   names. Only for SINGLE foods - set to "" for composite/mixed dishes.

If the description is too vague to identify any food, return an empty items list and explain why
in "summary"."""


def analyze_text_food(description):
    """Estimate nutrition from a plain-text meal description (no photo) - used
    by the chat assistant's log-a-meal tool."""
    prompt = f'{TEXT_ANALYSIS_PROMPT}\n\nMeal description: "{description}"'
    result = _run_model([prompt])
    result['retried'] = False
    return result


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    return jsonify({
        'gemini_configured': client is not None,
        'models': MODEL_CANDIDATES,
    })


@app.route('/upload', methods=['POST'])
def upload_file():
    """Analyze an uploaded food photo.

    Accepts multipart/form-data (the web/mobile UI), a JSON body with a
    base64 image (older glasses path), or a JSON body with a photo_url (the
    Mentra glasses UI WebView): Mentra's photo storage host doesn't send
    CORS headers permitting the WebView's origin, so the browser blocks a
    direct fetch there - this server fetches it instead, since server-to-
    server requests aren't subject to browser CORS at all. See
    glasses/README.md.
    """
    if client is None:
        return jsonify({'error': 'GEMINI_API_KEY is not configured on the server. '
                                 'Add it to .env and restart.'}), 503

    # Prefer a real multipart file when present; otherwise try to parse the
    # body as JSON regardless of the declared Content-Type. Some embedded JS
    # runtimes (e.g. the Mentra glasses background JSContext) don't set
    # Content-Type the way a browser's fetch() does, so request.is_json can't
    # be trusted here - force=True parses the body as JSON anyway.
    if 'file' not in request.files:
        data = request.get_json(silent=True, force=True) or {}
        photo_url = data.get('photo_url')
        image_b64 = data.get('image_base64')
        if photo_url:
            try:
                photo_response = requests.get(
                    photo_url, timeout=20, headers={'User-Agent': 'SnapTrack/1.0'})
                photo_response.raise_for_status()
                image = Image.open(io.BytesIO(photo_response.content))
                image.load()
            except Exception as e:
                app.logger.error(f"Failed to fetch photo_url: {e}")
                return jsonify({'error': 'Could not fetch that photo URL. It may have expired.'}), 400
        elif image_b64:
            try:
                image = Image.open(io.BytesIO(base64.b64decode(image_b64)))
                image.load()
            except Exception:
                return jsonify({'error': 'Could not decode that image. Please try another photo.'}), 400
        else:
            return jsonify({'error': 'No image_base64 or photo_url provided'}), 400
        original_description = (data.get('original_description') or '').strip()
        corrected_description = (data.get('corrected_description') or '').strip()
        correction = corrected_description or (data.get('correction') or '').strip() or None
    else:
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if not allowed_file(file.filename):
            return jsonify({'error': 'Invalid file type. Please upload an image (PNG, JPG, JPEG, GIF, WEBP)'}), 400

        try:
            image = Image.open(file.stream)
            image.load()
        except Exception:
            return jsonify({'error': 'Could not read that file as an image. Please try another photo.'}), 400

        original_description = (request.form.get('original_description') or '').strip()
        corrected_description = (request.form.get('corrected_description') or '').strip()
        correction = corrected_description or (request.form.get('correction') or '').strip() or None

    start = time.perf_counter()
    try:
        result = analyze_food(image, correction=correction)
        _log_analysis_request((time.perf_counter() - start) * 1000, bool(correction), result=result)
        _record_correction(original_description, corrected_description)
        return jsonify({
            'success': True,
            'items': result['items'],
            'full_description': result['full_description'],
            'totals': result['totals'],
            'count': len(result['items']),
            'source': result['source'],
        })
    except Exception as e:
        _log_analysis_request((time.perf_counter() - start) * 1000, bool(correction), error=str(e))
        app.logger.error(f"Analysis failed: {e}")
        return jsonify({'error': f'Image analysis failed: {e}'}), 502


def _generate_text(prompt):
    """Plain-text Gemini call with the same model fallback/cooldown as image analysis"""
    last_error = None
    for model_name in MODEL_CANDIDATES:
        if _model_cooldown_until.get(model_name, 0) > time.time():
            continue
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            return response.text.strip()
        except Exception as e:
            app.logger.warning(f"{model_name} failed: {e}")
            last_error = e
            if _is_rate_limit(e):
                _model_cooldown_until[model_name] = time.time() + RATE_LIMIT_COOLDOWN_SECONDS
    raise last_error or RuntimeError('No model available')


_coach_cache = {'key': None, 'text': None}


def _goals_line():
    goals = _get_goals()
    parts = []
    if goals['calorie_goal']:
        parts.append(f"{round(goals['calorie_goal'])} kcal")
    if goals['protein_goal']:
        parts.append(f"{round(goals['protein_goal'])}g protein")
    if not parts:
        return ''
    return f"The user's daily targets: {', '.join(parts)}. Frame advice against these targets.\n"


@app.route('/api/coach')
def coach():
    """One-sentence coaching insight about today's eating so far"""
    if client is None:
        return jsonify({'message': None})

    today = datetime.now().strftime('%Y-%m-%d')
    with get_db() as db:
        rows = db.execute(
            q('SELECT * FROM meals WHERE created_at LIKE ? ORDER BY created_at'),
            (today + '%',)).fetchall()

    if not rows:
        return jsonify({'message': None})

    cache_key = (today, len(rows), rows[-1]['id'], str(_get_goals()))
    if _coach_cache['key'] == cache_key:
        return jsonify({'message': _coach_cache['text']})

    meal_lines = []
    for row in rows:
        items = json.loads(row['items'])
        name = items[0]['description'] if items else (row['summary'] or 'meal')
        when = row['created_at'][11:16]
        meal_lines.append(
            f"- {row['meal_type'] or 'Meal'} at {when}: {name} "
            f"({round(row['calories'] or 0)} kcal, {round(row['protein_g'] or 0)}g protein, "
            f"{round(row['carbs_g'] or 0)}g carbs, {round(row['fat_g'] or 0)}g fat)")

    totals = {
        'kcal': round(sum(r['calories'] or 0 for r in rows)),
        'protein': round(sum(r['protein_g'] or 0 for r in rows)),
        'carbs': round(sum(r['carbs_g'] or 0 for r in rows)),
        'fat': round(sum(r['fat_g'] or 0 for r in rows)),
    }

    prompt = f"""You are a friendly, no-nonsense nutrition coach inside a food tracking app.
The current time is {datetime.now().strftime('%H:%M')}. Here is what the user has eaten today:

{chr(10).join(meal_lines)}

Today's totals so far: {totals['kcal']} kcal, {totals['protein']}g protein, {totals['carbs']}g carbs, {totals['fat']}g fat.
{_goals_line()}
Write ONE or TWO short sentences (max 40 words total) of genuinely useful, specific observation or advice.
Reference their actual food or numbers. Consider what meals are still likely ahead today given the time.
Be warm but direct. No greetings, no emoji, no generic platitudes like "keep it up", no lecturing about health."""

    try:
        message = _generate_text(prompt)
        _coach_cache['key'] = cache_key
        _coach_cache['text'] = message
        return jsonify({'message': message})
    except Exception as e:
        app.logger.warning(f"Coach generation failed: {e}")
        return jsonify({'message': None})


_insight_cache = {'key': None, 'text': None}


@app.route('/api/insights/weekly')
def weekly_insight():
    """2-3 sentence Gemini read on the last 7 days of eating"""
    if client is None:
        return jsonify({'message': None})

    week_ago = (datetime.now() - timedelta(days=6)).strftime('%Y-%m-%d')
    with get_db() as db:
        rows = db.execute(
            q('SELECT * FROM meals WHERE created_at >= ? ORDER BY created_at'),
            (week_ago,)).fetchall()

    # Not enough data for trends to mean anything yet
    if len(rows) < 3:
        return jsonify({'message': None})

    cache_key = (datetime.now().strftime('%Y-%m-%d'), len(rows), rows[-1]['id'], str(_get_goals()))
    if _insight_cache['key'] == cache_key:
        return jsonify({'message': _insight_cache['text']})

    # One line per day: date, meal count, kcal, macros
    days = {}
    for row in rows:
        day = row['created_at'][:10]
        d = days.setdefault(day, {'meals': 0, 'kcal': 0, 'protein': 0, 'carbs': 0, 'fat': 0})
        d['meals'] += 1
        d['kcal'] += row['calories'] or 0
        d['protein'] += row['protein_g'] or 0
        d['carbs'] += row['carbs_g'] or 0
        d['fat'] += row['fat_g'] or 0

    day_lines = [
        f"- {day}: {d['meals']} meals, {round(d['kcal'])} kcal, "
        f"{round(d['protein'])}g protein, {round(d['carbs'])}g carbs, {round(d['fat'])}g fat"
        for day, d in sorted(days.items())
    ]

    prompt = f"""You are a friendly, no-nonsense nutrition coach inside a food tracking app.
Here is the user's eating log for the past week (days with no line were not logged):

{chr(10).join(day_lines)}

{_goals_line()}
Write TWO or THREE short sentences (max 60 words total) about their WEEK as a whole:
a pattern, trend, or comparison across days that a single-day view would miss.
Reference actual numbers or days. Be warm but direct. No greetings, no emoji,
no generic platitudes like "keep it up", no lecturing about health."""

    try:
        message = _generate_text(prompt)
        _insight_cache['key'] = cache_key
        _insight_cache['text'] = message
        return jsonify({'message': message})
    except Exception as e:
        app.logger.warning(f"Weekly insight generation failed: {e}")
        return jsonify({'message': None})


def _percentile(sorted_values, pct):
    """Linear-interpolated percentile of an already-sorted list"""
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


@app.route('/api/pipeline-stats')
def pipeline_stats():
    """Aggregate recent /upload requests: latency, errors, retries, USDA
    grounding rate, and correction rate - the health of the analysis
    pipeline itself, not of any one meal."""
    with get_db() as db:
        rows = db.execute(
            'SELECT * FROM analysis_requests ORDER BY created_at DESC LIMIT 500').fetchall()

    if not rows:
        return jsonify({'count': 0})

    successes = [r for r in rows if not r['error']]
    errors = [r for r in rows if r['error']]
    latencies = sorted(r['latency_ms'] for r in rows if r['latency_ms'] is not None)
    item_total = sum(r['item_count'] or 0 for r in successes)
    grounded_total = sum(r['grounded_count'] or 0 for r in successes)

    model_counts = {}
    for r in successes:
        if r['model']:
            model_counts[r['model']] = model_counts.get(r['model'], 0) + 1

    with get_db() as db:
        correction_rows = db.execute(
            'SELECT original_description, corrected_description FROM corrections').fetchall()
    pattern_counts, pattern_fix = {}, {}
    for r in correction_rows:
        key = r['original_description'].strip().lower()
        pattern_counts[key] = pattern_counts.get(key, 0) + 1
        pattern_fix[key] = r['corrected_description']
    top_corrections = sorted(
        ({'original': k, 'corrected': pattern_fix[k], 'count': c} for k, c in pattern_counts.items() if c >= 2),
        key=lambda p: -p['count'])[:5]

    return jsonify({
        'count': len(rows),
        'error_rate': round(100 * len(errors) / len(rows), 1),
        'retry_rate': round(100 * sum(r['retried'] for r in successes) / len(successes), 1) if successes else None,
        'correction_rate': round(100 * sum(r['had_correction'] for r in rows) / len(rows), 1),
        'grounding_rate': round(100 * grounded_total / item_total, 1) if item_total else None,
        'latency_p50_ms': round(_percentile(latencies, 0.5)) if latencies else None,
        'latency_p95_ms': round(_percentile(latencies, 0.95)) if latencies else None,
        'model_counts': model_counts,
        'top_corrections': top_corrections,
    })


def _meal_type_for_now():
    h = datetime.now().hour
    if 4 <= h < 11:
        return 'Breakfast'
    if 11 <= h < 16:
        return 'Lunch'
    if 16 <= h < 22:
        return 'Dinner'
    return 'Snack'


def _insert_meal(items, totals, summary='', thumbnail=None, meal_type=None):
    """Shared by the /api/meals route and the chat assistant's log tool"""
    if meal_type not in ('Breakfast', 'Lunch', 'Dinner', 'Snack'):
        meal_type = None
    created_at = datetime.now().isoformat(timespec='seconds')
    sql = ('INSERT INTO meals (created_at, summary, items, calories, protein_g, carbs_g, fat_g, thumbnail, meal_type) '
           'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
    params = (created_at, summary, json.dumps(items),
              totals.get('calories'), totals.get('protein_g'),
              totals.get('carbs_g'), totals.get('fat_g'),
              thumbnail, meal_type)
    with get_db() as db:
        if IS_POSTGRES:
            cur = db.execute(q(sql + ' RETURNING id'), params)
            meal_id = cur.fetchone()['id']
        else:
            cur = db.execute(sql, params)
            meal_id = cur.lastrowid
    return meal_id, created_at


@app.route('/api/meals', methods=['POST'])
def log_meal():
    """Save an analyzed meal to the log"""
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not items:
        return jsonify({'error': 'Nothing to log'}), 400

    meal_id, created_at = _insert_meal(
        items, data.get('totals') or {}, data.get('summary', ''),
        data.get('thumbnail'), data.get('meal_type'))
    return jsonify({'success': True, 'id': meal_id, 'created_at': created_at})


@app.route('/api/meals', methods=['GET'])
def list_meals():
    """Return logged meals, newest first"""
    with get_db() as db:
        rows = db.execute('SELECT * FROM meals ORDER BY created_at DESC, id DESC LIMIT 200').fetchall()
    meals = []
    for row in rows:
        meal = dict(row)
        meal['items'] = json.loads(meal['items'])
        meals.append(meal)
    return jsonify({'meals': meals})


@app.route('/api/meals/<int:meal_id>', methods=['DELETE'])
def delete_meal(meal_id):
    with get_db() as db:
        db.execute(q('DELETE FROM meals WHERE id = ?'), (meal_id,))
    return jsonify({'success': True})


@app.route('/api/meals/<int:meal_id>', methods=['PATCH'])
def update_meal(meal_id):
    """Update a logged meal (currently: its type)"""
    data = request.get_json(silent=True) or {}
    meal_type = data.get('meal_type')
    if meal_type not in ('Breakfast', 'Lunch', 'Dinner', 'Snack'):
        return jsonify({'error': 'Invalid meal_type'}), 400
    with get_db() as db:
        db.execute(q('UPDATE meals SET meal_type = ? WHERE id = ?'), (meal_type, meal_id))
    return jsonify({'success': True})


def _get_goals():
    with get_db() as db:
        rows = db.execute(q("SELECT key, value FROM settings WHERE key IN (?, ?)"),
                          ('calorie_goal', 'protein_goal')).fetchall()
    goals = {row['key']: row['value'] for row in rows}
    return {
        'calorie_goal': _to_number(goals.get('calorie_goal')),
        'protein_goal': _to_number(goals.get('protein_goal')),
    }


@app.route('/api/goals', methods=['GET'])
def get_goals():
    return jsonify(_get_goals())


def _set_goals(calorie_goal=None, protein_goal=None):
    """Shared by the /api/goals route and the chat assistant's set_goals tool"""
    upsert = ('INSERT INTO settings (key, value) VALUES (?, ?) '
              'ON CONFLICT (key) DO UPDATE SET value = excluded.value')
    with get_db() as db:
        for key, raw in (('calorie_goal', calorie_goal), ('protein_goal', protein_goal)):
            value = _to_number(raw)
            if value and value > 0:
                db.execute(q(upsert), (key, str(value)))
            else:
                db.execute(q('DELETE FROM settings WHERE key = ?'), (key,))
    return _get_goals()


@app.route('/api/goals', methods=['POST'])
def set_goals():
    data = request.get_json(silent=True) or {}
    return jsonify({'success': True, **_set_goals(data.get('calorie_goal'), data.get('protein_goal'))})


# ---------- Chat assistant ----------
# A real function-calling loop grounded in the app's own data, not a wrapper
# around a static prompt: the model can see actual meals/goals and log new
# ones, through the same DB helpers the REST routes use.

CHAT_TOOLS = [
    types.FunctionDeclaration(
        name='get_daily_totals',
        description="Get the user's daily nutrition totals (calories, protein, carbs, fat) for "
                    "each of the last N days, most recent first. Use for trend/average questions "
                    "or 'how much X have I eaten' over a period.",
        parameters={
            'type': 'object',
            'properties': {'days': {'type': 'integer', 'description': 'how many days back, 1-30'}},
            'required': ['days'],
        },
    ),
    types.FunctionDeclaration(
        name='get_meal_log',
        description="Get the user's individual logged meals for the last N days - what they ate, "
                    "when, and its nutrition. Use for questions about specific meals.",
        parameters={
            'type': 'object',
            'properties': {'days': {'type': 'integer', 'description': 'how many days back, 1-14'}},
            'required': ['days'],
        },
    ),
    types.FunctionDeclaration(
        name='get_goals',
        description="Get the user's current daily calorie and protein goals, if any are set.",
        parameters={'type': 'object', 'properties': {}},
    ),
    types.FunctionDeclaration(
        name='set_goals',
        description="Set the user's daily calorie and/or protein goal. Omit a field to leave it unchanged.",
        parameters={
            'type': 'object',
            'properties': {
                'calorie_goal': {'type': 'number', 'description': 'daily calorie goal in kcal'},
                'protein_goal': {'type': 'number', 'description': 'daily protein goal in grams'},
            },
        },
    ),
    types.FunctionDeclaration(
        name='log_meal_from_text',
        description="Analyze a meal the user describes in words (no photo) and log it to their "
                    "food diary. Use whenever the user describes something they ate and wants it tracked.",
        parameters={
            'type': 'object',
            'properties': {
                'description': {'type': 'string', 'description': 'what they ate, in their words'},
                'meal_type': {'type': 'string', 'enum': ['Breakfast', 'Lunch', 'Dinner', 'Snack'],
                              'description': 'omit to infer from the current time'},
            },
            'required': ['description'],
        },
    ),
]


def _tool_get_daily_totals(days=7):
    days = max(1, min(int(days or 7), 30))
    since = (datetime.now() - timedelta(days=days - 1)).strftime('%Y-%m-%d')
    with get_db() as db:
        rows = db.execute(q('SELECT * FROM meals WHERE created_at >= ? ORDER BY created_at'), (since,)).fetchall()
    by_day = {}
    for r in rows:
        day = r['created_at'][:10]
        totals = by_day.setdefault(day, {'calories': 0, 'protein_g': 0, 'carbs_g': 0, 'fat_g': 0, 'meals': 0})
        totals['calories'] += r['calories'] or 0
        totals['protein_g'] += r['protein_g'] or 0
        totals['carbs_g'] += r['carbs_g'] or 0
        totals['fat_g'] += r['fat_g'] or 0
        totals['meals'] += 1
    return {'daily_totals': [
        {'date': day, **{k: round(v, 1) for k, v in totals.items()}}
        for day, totals in sorted(by_day.items(), reverse=True)
    ]}


def _tool_get_meal_log(days=3):
    days = max(1, min(int(days or 3), 14))
    since = (datetime.now() - timedelta(days=days - 1)).strftime('%Y-%m-%d')
    with get_db() as db:
        rows = db.execute(
            q('SELECT * FROM meals WHERE created_at >= ? ORDER BY created_at DESC LIMIT 60'),
            (since,)).fetchall()
    meals = []
    for r in rows:
        items = json.loads(r['items'])
        name = items[0]['description'] if items else (r['summary'] or 'meal')
        meals.append({
            'date': r['created_at'][:10], 'time': r['created_at'][11:16],
            'meal_type': r['meal_type'], 'description': name,
            'calories': r['calories'], 'protein_g': r['protein_g'],
            'carbs_g': r['carbs_g'], 'fat_g': r['fat_g'],
        })
    return {'meals': meals}


def _tool_set_goals(calorie_goal=None, protein_goal=None):
    # The tool's contract is "omit = unchanged"; /api/goals's is "omit =
    # clear" (right for its form, which always sends both fields). Merge
    # with the current values here rather than change that route's semantics.
    current = _get_goals()
    return _set_goals(
        calorie_goal if calorie_goal is not None else current['calorie_goal'],
        protein_goal if protein_goal is not None else current['protein_goal'])


def _tool_log_meal_from_text(description, meal_type=None):
    if not description or not description.strip():
        return {'error': 'No description given'}
    result = analyze_text_food(description)
    if not result['items']:
        return {'error': 'Could not identify any food in that description.',
                'summary': result['full_description']}
    if meal_type not in ('Breakfast', 'Lunch', 'Dinner', 'Snack'):
        meal_type = _meal_type_for_now()
    meal_id, created_at = _insert_meal(result['items'], result['totals'], result['full_description'],
                                        meal_type=meal_type)
    return {'logged': True, 'meal_id': meal_id, 'meal_type': meal_type, 'totals': result['totals'],
            'items': [i['description'] for i in result['items']]}


CHAT_TOOL_IMPLS = {
    'get_daily_totals': lambda args: _tool_get_daily_totals(args.get('days')),
    'get_meal_log': lambda args: _tool_get_meal_log(args.get('days')),
    'get_goals': lambda args: _get_goals(),
    'set_goals': lambda args: _tool_set_goals(args.get('calorie_goal'), args.get('protein_goal')),
    'log_meal_from_text': lambda args: _tool_log_meal_from_text(args.get('description'), args.get('meal_type')),
}

CHAT_SYSTEM_PROMPT = """You are SnapTrack's in-app nutrition assistant. You can see the user's \
real meal log and goals through your tools, and you can log new meals they describe in words. \
Always call a tool to check real data before answering questions about what they've eaten or \
their goals - never guess or make up numbers. When you log a meal, confirm what you logged and \
its totals. Keep replies short (2-4 sentences) and conversational - this is a chat bubble, not a \
report. No markdown formatting. Today is {now}."""

MAX_CHAT_TOOL_CALLS = 5


@app.route('/api/chat', methods=['POST'])
def chat():
    """One turn of the nutrition assistant. The client holds conversation
    history (not persisted server-side) and resends it each call."""
    if client is None:
        return jsonify({'error': 'GEMINI_API_KEY is not configured on the server.'}), 503

    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'error': 'No message provided'}), 400

    contents = []
    for turn in (data.get('history') or [])[-20:]:
        role = 'model' if turn.get('role') == 'model' else 'user'
        text = (turn.get('text') or '').strip()
        if text:
            contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    contents.append(types.Content(role='user', parts=[types.Part(text=message)]))

    config = types.GenerateContentConfig(
        system_instruction=CHAT_SYSTEM_PROMPT.format(now=datetime.now().strftime('%A, %Y-%m-%d %H:%M')),
        tools=[types.Tool(function_declarations=CHAT_TOOLS)],
    )

    actions = []
    try:
        for _ in range(MAX_CHAT_TOOL_CALLS):
            response = None
            last_error = None
            for model_name in MODEL_CANDIDATES:
                if _model_cooldown_until.get(model_name, 0) > time.time():
                    continue
                try:
                    response = client.models.generate_content(model=model_name, contents=contents, config=config)
                    break
                except Exception as e:
                    last_error = e
                    if _is_rate_limit(e):
                        _model_cooldown_until[model_name] = time.time() + RATE_LIMIT_COOLDOWN_SECONDS
            if response is None:
                raise last_error or RuntimeError('No model available')

            parts = response.candidates[0].content.parts if response.candidates else []
            calls = [p.function_call for p in parts if p.function_call]

            if not calls:
                return jsonify({'message': response.text, 'actions': actions})

            contents.append(response.candidates[0].content)
            response_parts = []
            for call in calls:
                impl = CHAT_TOOL_IMPLS.get(call.name)
                try:
                    result = impl(dict(call.args)) if impl else {'error': f'Unknown tool {call.name}'}
                except Exception as e:
                    app.logger.warning(f"Chat tool {call.name} failed: {e}")
                    result = {'error': str(e)}
                actions.append({'tool': call.name, 'args': dict(call.args)})
                response_parts.append(types.Part(function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response=result)))
            contents.append(types.Content(role='user', parts=response_parts))

        return jsonify({
            'message': "That took more steps than expected - could you ask more directly?",
            'actions': actions,
        })
    except Exception as e:
        app.logger.error(f"Chat failed: {e}")
        return jsonify({'error': f'Chat failed: {e}'}), 502


if __name__ == '__main__':
    # Local default port 5001: macOS AirPlay Receiver occupies port 5000
    # host 0.0.0.0: reachable from phones on the same Wi-Fi for camera testing
    # In production, run under gunicorn instead: gunicorn -w 2 -b 0.0.0.0:$PORT app:app
    port = int(os.environ.get('PORT', 5001))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(debug=debug, host='0.0.0.0', port=port)
