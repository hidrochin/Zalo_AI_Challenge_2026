# Training code

**No training, fine-tuning or synthetic data generation was performed.** The model
(Qwen3.5-9B, revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`) runs exactly as released, with prompting only.

## Contents

| path | content |
|---|---|
| `data/train/` | the organizers' train split as provided: 30 businesses, 3000 questions, `answers.jsonl`, `rulebook.md`, `calendar.csv` |
| `data_manifest.json` | every packaged data and weight file with its size and SHA-256, the dataset counts, the model's source, revision and licence |
| `data_generation/manifest.py` | the script that writes `data_manifest.json` |
| `requirements.txt` | pinned dependencies (the same as inference) |

## How the train split is used

1. **Runtime examples.** At inference, the train questions and their gold answers are retrieved (TF-IDF) as
   examples for the model (`src/datamate/agent/retrieve.py`).
2. **Telex lexicon.** `/code/assets/vi_rhymes.txt` is built from train questions and the train rulebook only. To
   rebuild it, run this from `/code` with `data/train` linked to `training_code/data/train`:
   `uv run datamate build-lexicon`.
3. **Development checks.** The deterministic code (ingestion, SQL views and macros) was tested against train
   answers (`datamate sql-check`, `datamate facts-check`, `uv run pytest -q`).

No public-test or private-test data was used for training or for building any packaged asset.
