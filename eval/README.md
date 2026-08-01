# Accuracy eval

Measures how close SnapTrack's calorie/macro estimates get to ground truth,
using the exact `analyze_food()` pipeline the app runs in production.

## Setup

1. Put meal photos in `eval/photos/`. The best ground truth is food with
   **published nutrition facts**: packaged food, chain restaurant items
   (McDonald's, Chipotle, Subway all publish numbers), or single whole foods
   weighed on a scale and looked up in USDA FoodData Central.
2. Copy `manifest.example.json` to `manifest.json` and add one entry per photo:
   `photo` (path relative to `eval/`) and `calories` are required;
   `protein_g` / `carbs_g` / `fat_g` / `label` / `source` are recommended.
3. Aim for ~50 photos across categories: packaged, restaurant, home-cooked,
   single items vs. full plates, good vs. bad lighting.

## Run

```bash
./venv/bin/python eval/run_eval.py eval/manifest.json
```

Predictions are cached in `eval/results/` so re-runs are free — only new
photos hit the API (`--fresh` forces re-analysis). Free-tier friendly:
one photo every few seconds.

## Output

- `eval/results/manifest-report.md` — summary metrics plus a per-photo table
  sorted worst-first, so the biggest misses are easy to inspect.
- Key metrics: **calorie MAPE** (mean absolute % error), median APE
  (robust to one bad miss), MAE in kcal, and the share of photos within
  20% / 30% of truth. Macro MAPEs when ground truth includes them.

## Results so far (Aug 2026, 50 Nutrition5k dishes, gemini-3.1-flash-lite)

| | Baseline prompt | + mass-first estimation |
|---|---|---|
| Calorie MAPE | 35.6% | 33.4% |
| Bias on dishes <200 kcal | +25.7% | **−1.5%** |
| Bias on dishes ≥400 kcal | −10.1% | −12.1% |
| Fat MAPE | 177% | **47%** |

Error analysis showed the baseline regressed toward "typical meal" calories
(over-predicting light plates, under-predicting dense ones). Rewriting the
prompt to estimate grams first, multiply by energy density, and explicitly
permit extreme values eliminated the light-dish bias and fixed fat estimation;
remaining calorie error is mostly per-dish scatter. Ground truth via
`fetch_nutrition5k.py` (Nutrition5k: real dishes, per-ingredient scale weights).

For context: nutrition-estimation literature generally considers ±20%
good for photo-based estimation — even human dietitians often miss by that
much on restaurant meals.
