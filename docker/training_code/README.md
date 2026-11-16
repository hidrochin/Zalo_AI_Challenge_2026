# Training code

**No training or fine-tuning was performed.** The model (Qwen3.5-9B, revision
`c202236235762e1c871ad0ccb60c8ee5ba337b9a`) runs exactly as released, with prompting only.

**One data asset was generated:** the external part of the question lexicon, `/code/assets/lexicon_external.json`
(section "Generated data" below).

## Contents

| path | content |
|---|---|
| `data/train/` | the organizers' train split as provided: 30 businesses, 3000 questions, `answers.jsonl`, `rulebook.md`, `calendar.csv` |
| `data_manifest.json` | every packaged data and weight file with its size and SHA-256, the dataset counts, the model's source, revision and licence, and the generated lexicon with its raw replies (`generated_data`) |
| `data_generation/manifest.py` | the script that writes `data_manifest.json` (`--add-generated` adds the `generated_data` part to an existing manifest) |
| `requirements.txt` | pinned dependencies (the same as inference) |

## How the train split is used

1. **Runtime examples.** At inference, the train questions and their gold answers are retrieved (TF-IDF) as
   examples for the model (`src/datamate/agent/retrieve.py`).
2. **Telex lexicon.** `/code/assets/vi_rhymes.txt` is built from train questions and the train rulebook only. To
   rebuild it, run this from `/code` with `data/train` linked to `training_code/data/train`:
   `uv run datamate build-lexicon`.
3. **Development checks.** The deterministic code (ingestion, SQL views and macros) was tested against train
   answers (`datamate sql-check`, `datamate facts-check`, `uv run pytest -q`), and the typed slots against train shops
   changed to show each slot's gap (`datamate slots --split train --mutate all`).

## Generated data

`/code/assets/lexicon_external.json` holds 85 word rewrites (abbreviations, teen-code, regional words → standard
Vietnamese) that the rule-based question normalization applies (`src/datamate/preprocess/lexicon.py`):

| source | entries | how |
|---|---|---|
| Gemini `gemini-3-flash-preview` (Google API, temperature 0, seed 0) | 67 | `src/datamate/preprocess/lexgen.py` |
| public web pages | 16 | the URL is stored with each entry |
| initials of the train phrase "đơn hàng" (`đh`, `dh`, as `hd` → `hoá đơn`) | 2 | written by hand, reason stored with each entry |

Generation (development only; inference makes no API call):
1. **Seeds**: standard words and two-word phrases of the **train** split only: question words used by at least 8 train
   shops and the term cells of the train rulebook (`lexgen.seeds`). No public- or private-test text was sent.
2. **Proposals**: Gemini lists, for each batch of 60 seeds, how people type each seed in chat (prompt `lexgen.PROMPT`).
   Raw replies: `/code/assets/lexicon_gemini_raw.jsonl` (model, seeds, reply).
3. **Filters**: each proposal must pass `lexicon.accept`: one to three words of letters, mapping to one of the seeds,
   not the seed itself with or without diacritics, and not ordinary train text (a single word may not be a train
   word; a phrase may not be made of train words only).
4. **Check**: a second Gemini call confirms meaning and lack of ambiguity of each remaining pair. Raw replies:
   `/code/assets/lexicon_gemini_verify.jsonl`.

Commands, from `/code` with `data/train` linked to `training_code/data/train`:
- rebuild from the kept replies, no API call: `uv run python -m datamate.preprocess.lexgen --refilter`. It writes
  the 67 Gemini and 16 web entries exactly as packaged; the 2 hand-written entries are then appended by hand;
- regenerate (needs the internet and `GEMINI_API_TOKEN`): `uv run python -m datamate.preprocess.lexgen`. A new call
  to the hosted model may answer differently; the kept replies are the ones the packaged lexicon was built from.

No public-test or private-test data was used for training or for building any packaged asset.

## Hand-written word lists

Besides the generated lexicon, the code holds word lists the team wrote by hand (abbreviations, dialect and filler
words, subjective adjectives, column-name and unit patterns). No model or data generated them, so there is nothing
to reproduce. The entries that occur nowhere in the train split are listed in `/code/README.md`, section Data.
