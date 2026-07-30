import io
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

import secrets

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, render_template_string
from google import genai
from google.genai import types
from PIL import Image

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
                    "calories": {"type": "number"},
                    "protein_g": {"type": "number"},
                    "carbs_g": {"type": "number"},
                    "fat_g": {"type": "number"},
                    "confidence": {"type": "number", "description": "0-100"},
                },
                "required": ["name", "portion", "calories", "protein_g", "carbs_g", "fat_g", "confidence"],
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


def _run_model(image, prompt):
    """Run one Gemini analysis pass and return the parsed result dict"""
    response = None
    last_error = None
    for model_name in MODEL_CANDIDATES:
        if _model_cooldown_until.get(model_name, 0) > time.time():
            app.logger.info(f"Skipping {model_name} (rate-limit cooldown)")
            continue
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[prompt, image],
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
            'calories': _to_number(item.get('calories')),
            'protein_g': _to_number(item.get('protein_g')),
            'carbs_g': _to_number(item.get('carbs_g')),
            'fat_g': _to_number(item.get('fat_g')),
            'confidence': _to_number(item.get('confidence')) or 90.0,
            'type': 'gemini',
        })

    totals = {}
    for field in ('calories', 'protein_g', 'carbs_g', 'fat_g'):
        values = [i[field] for i in detected_items if i.get(field) is not None]
        totals[field] = round(sum(values), 1) if values else None

    return {
        'items': detected_items,
        'full_description': parsed.get('summary', ''),
        'totals': totals,
        'source': 'gemini',
    }


def _min_confidence(result):
    scores = [i['confidence'] for i in result['items'] if i.get('confidence') is not None]
    return min(scores) if scores else 0


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

    result = _run_model(image, prompt)

    if not correction and result['items'] and _min_confidence(result) < 90:
        app.logger.info("Low confidence result, re-running analysis once")
        try:
            second = _run_model(image, prompt)
            if _min_confidence(second) > _min_confidence(result):
                result = second
        except Exception as e:
            app.logger.warning(f"Confidence retry failed, keeping first result: {e}")

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
    """Analyze an uploaded food photo"""
    if client is None:
        return jsonify({'error': 'GEMINI_API_KEY is not configured on the server. '
                                 'Add it to .env and restart.'}), 503

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

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

    correction = (request.form.get('correction') or '').strip() or None

    try:
        result = analyze_food(image, correction=correction)
        return jsonify({
            'success': True,
            'items': result['items'],
            'full_description': result['full_description'],
            'totals': result['totals'],
            'count': len(result['items']),
            'source': result['source'],
        })
    except Exception as e:
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


@app.route('/api/meals', methods=['POST'])
def log_meal():
    """Save an analyzed meal to the log"""
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not items:
        return jsonify({'error': 'Nothing to log'}), 400

    totals = data.get('totals') or {}
    meal_type = data.get('meal_type')
    if meal_type not in ('Breakfast', 'Lunch', 'Dinner', 'Snack'):
        meal_type = None
    created_at = datetime.now().isoformat(timespec='seconds')
    sql = ('INSERT INTO meals (created_at, summary, items, calories, protein_g, carbs_g, fat_g, thumbnail, meal_type) '
           'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
    params = (created_at, data.get('summary', ''), json.dumps(items),
              totals.get('calories'), totals.get('protein_g'),
              totals.get('carbs_g'), totals.get('fat_g'),
              data.get('thumbnail'), meal_type)
    with get_db() as db:
        if IS_POSTGRES:
            cur = db.execute(q(sql + ' RETURNING id'), params)
            meal_id = cur.fetchone()['id']
        else:
            cur = db.execute(sql, params)
            meal_id = cur.lastrowid
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


@app.route('/api/goals', methods=['POST'])
def set_goals():
    data = request.get_json(silent=True) or {}
    upsert = ('INSERT INTO settings (key, value) VALUES (?, ?) '
              'ON CONFLICT (key) DO UPDATE SET value = excluded.value')
    with get_db() as db:
        for key in ('calorie_goal', 'protein_goal'):
            value = _to_number(data.get(key))
            if value and value > 0:
                db.execute(q(upsert), (key, str(value)))
            else:
                db.execute(q('DELETE FROM settings WHERE key = ?'), (key,))
    return jsonify({'success': True, **_get_goals()})


if __name__ == '__main__':
    # Local default port 5001: macOS AirPlay Receiver occupies port 5000
    # host 0.0.0.0: reachable from phones on the same Wi-Fi for camera testing
    # In production, run under gunicorn instead: gunicorn -w 2 -b 0.0.0.0:$PORT app:app
    port = int(os.environ.get('PORT', 5001))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(debug=debug, host='0.0.0.0', port=port)
