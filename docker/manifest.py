"""data_manifest.json of the submission image (guideline section 8): every file under training_code/data and models/
with its size and SHA-256, the dataset counts, the model's public source, and the generated data (the lexicon's
external entries with the raw replies they were built from).

    python docker/manifest.py <context dir> > <context dir>/training_code/data_manifest.json
    python manifest.py --add-generated /code /code/training_code/data_manifest.json   # in an image: generated_data only
"""

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

MODEL = {"name": "Qwen3.5-9B", "source": "https://huggingface.co/Qwen/Qwen3.5-9B",
         "revision": "c202236235762e1c871ad0ccb60c8ee5ba337b9a", "license": "apache-2.0", "parameters": 9653104368,
         "released": "2026-02-27", "local_path": "/code/models/Qwen3.5-9B"}
GENERATED = ("lexicon_external.json", "lexicon_gemini_raw.jsonl", "lexicon_gemini_verify.jsonl")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def files(root: Path, base: Path) -> list[dict]:
    return [{"path": f"/code/{p.relative_to(base)}", "bytes": p.stat().st_size, "sha256": sha256(p)}
            for p in sorted(root.rglob("*")) if p.is_file()]


def generated(assets: Path) -> dict:
    """The one generated asset, used by the question normalization, and the raw model replies it was built from."""
    entries = json.loads((assets / "lexicon_external.json").read_text(encoding="utf-8"))
    kind = lambda source: "gemini" if source.startswith("gemini:") else "web page" if source.startswith("http") else "by hand"
    return {"lexicon_external": {
        "generator": "src/datamate/preprocess/lexgen.py: Gemini gemini-3-flash-preview through the Google API, temperature 0, "
                     "seed 0, during development only (inference makes no API call); prompt in lexgen.PROMPT",
        "seeds": "train split only: question words of at least 8 train shops and the term cells of the train rulebook",
        "entries": len(entries), "by_source": dict(Counter(kind(e["source"]) for e in entries)),
        "files": [{"path": f"/code/assets/{n}", "bytes": (assets / n).stat().st_size, "sha256": sha256(assets / n)}
                  for n in GENERATED]}}


def main(ctx: Path) -> None:
    train = ctx / "training_code" / "data" / "train"
    count = lambda name: sum(1 for line in (train / name).read_text(encoding="utf-8").splitlines() if line.strip())
    manifest = {
        "training": "none: no model was trained or fine-tuned; the base model is used as released",
        "data": {"source": "Zalo AI Challenge 2026 DataMate train split (as provided by the organizers)",
                 "businesses": sum(1 for p in (train / "data").iterdir() if p.is_dir()),
                 "questions": count("questions.jsonl"), "answers": count("answers.jsonl"),
                 "files": files(ctx / "training_code" / "data", ctx)},
        "model": {**MODEL, "files": files(ctx / "models", ctx)},
        "generated_data": generated(ctx / "code" / "assets"),
    }
    json.dump(manifest, sys.stdout, ensure_ascii=False, indent=1)
    print()


def add_generated(code: Path, path: Path) -> None:
    """Add generated_data to the manifest an earlier image holds, without hashing the weights and train data again."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["generated_data"] = generated(code / "assets")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] == "--add-generated":
        add_generated(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        main(Path(sys.argv[1]))
