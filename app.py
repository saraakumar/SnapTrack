import io
import json
import os

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from google import genai
from google.genai import types
from PIL import Image

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
if client:
    print(f"[STARTUP] Gemini configured with key: {GEMINI_API_KEY[:12]}...")
else:
    print("[STARTUP] GEMINI_API_KEY not set - uploads will fail until it is "
          "(add it to .env; get a key at https://aistudio.google.com/apikey)")

# gemini-2.5-* models are closed to new accounts; this key has access to
# gemini-3.5-flash (5 req/min free tier) and gemini-3.1-flash-lite (separate quota)
MODEL_CANDIDATES = ['gemini-3.5-flash', 'gemini-3.1-flash-lite']

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


def analyze_food(image):
    """Send the image to Gemini and return detected items with nutrition estimates"""
    response = None
    last_error = None
    for model_name in MODEL_CANDIDATES:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[ANALYSIS_PROMPT, image],
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',
                    response_schema=RESPONSE_SCHEMA,
                ),
            )
            break
        except Exception as e:
            app.logger.warning(f"{model_name} failed: {e}")
            last_error = e
    if response is None:
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

    try:
        result = analyze_food(image)
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


if __name__ == '__main__':
    # Port 5001: macOS AirPlay Receiver occupies port 5000
    app.run(debug=True, port=5001)
