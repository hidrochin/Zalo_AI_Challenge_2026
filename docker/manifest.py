"""data_manifest.json of the submission image (guideline section 8): every file under training_code/data and models/
with its size and SHA-256, the dataset counts and the model's public source.

    python docker/manifest.py <context dir> > <context dir>/training_code/data_manifest.json
"""

import hashlib
import json
import sys
from pathlib import Path

MODEL = {"name": "Qwen3.5-9B", "source": "https://huggingface.co/Qwen/Qwen3.5-9B",
         "revision": "c202236235762e1c871ad0ccb60c8ee5ba337b9a", "license": "apache-2.0", "parameters": 9653104368,
         "released": "2026-02-27", "local_path": "/code/models/Qwen3.5-9B"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def files(root: Path, base: Path) -> list[dict]:
    return [{"path": f"/code/{p.relative_to(base)}", "bytes": p.stat().st_size, "sha256": sha256(p)}
            for p in sorted(root.rglob("*")) if p.is_file()]


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
    }
    json.dump(manifest, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
