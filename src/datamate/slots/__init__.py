"""Shop conventions as typed slots (docs/PLAN_LAYERS.md section 9): the model fills a narrow JSON per convention, code
applies it.

`apply.py` holds the pure functions the loaders call (ingest and canon import it, so it imports neither); `base.py` runs
the slots of a shop (trigger, prompt, parse, apply, check) and saves the fills next to the ingest cache.
"""
