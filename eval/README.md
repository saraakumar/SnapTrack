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

| Calorie MAPE | Result | What changed |
|---|---|---|
| Baseline prompt | 35.6% | — |
| + mass-first estimation | 33.4% | light-dish bias +25.7%→−1.5%, fat MAPE 177%→47% |
| + median-of-3 ensemble | 32.2% | not worth 3× cost — error is systematic, not sampling noise |
| + USDA grounding, naive | 40.0% | REGRESSION: composite dishes priced as their densest ingredient ("caesar salad" × pure-dressing density) |
| + USDA grounding, composite-guarded | 33.4% | fixes extreme dense-food misses (792-kcal chia bowl: 80%→25% err) but net-neutral overall |

Takeaways: the model's dominant error was central-tendency bias (fixed via
prompt); remaining error is per-dish portion/identity scatter that neither
ensembling nor database grounding removes on net. Grounding (Gemini does
perception — food identity + grams; local USDA SR Legacy SQLite supplies
per-100g facts when a confident single-food match exists) is kept for its
tail behavior and provenance ("USDA ✓" items), with guardrails: match-overlap
threshold, wrong-preparation and wrong-identity penalties, composite-dish
exclusion. Ground truth via `fetch_nutrition5k.py` (real dishes,
per-ingredient scale weights).

For context: nutrition-estimation literature generally considers ±20%
good for photo-based estimation — even human dietitians often miss by that
much on restaurant meals.
