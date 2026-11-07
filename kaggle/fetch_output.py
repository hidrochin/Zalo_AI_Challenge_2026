"""Download every output file of a Kaggle kernel: list all pages first, then fetch in parallel, summaries before logs.

`kaggle kernels output --file-pattern` only filters the first page of files, so it misses per-question logs.
Usage: uvx --with six --with requests --from kaggle python -I kaggle/fetch_output.py <owner/kernel> <dest_dir>
For the second account, set KAGGLE_API_TOKEN for this command only (see README).
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.kernels.types.kernels_api_service import ApiListKernelSessionOutputRequest

kernel, dest = sys.argv[1], sys.argv[2]
owner, slug = kernel.split("/")
api = KaggleApi()
api.authenticate()
root = os.path.realpath(dest)

items, token = [], None
with api.build_kaggle_client() as kaggle:
    while True:
        req = ApiListKernelSessionOutputRequest()
        req.user_name, req.kernel_slug, req.page_size = owner, slug, 200
        if token:
            req.page_token = token
        resp = kaggle.kernels.kernels_api_client.list_kernel_session_output(req)
        items += [(f.file_name, f.url) for f in resp.files or []]
        token = resp.next_page_token
        if not token:
            break
print("listed", len(items), flush=True)
items.sort(key=lambda it: ("/logs/" in it[0], it[0]))


def fetch(it):
    name, url = it
    out = os.path.realpath(os.path.join(dest, name))
    if not out.startswith(root + os.sep):  # names come from the notebook's output: never write outside dest
        return f"skip {name}"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    err = None
    for _ in range(3):
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            with open(out, "wb") as fh:
                fh.write(r.content)
            return None
        except Exception as e:  # noqa: BLE001
            err = f"fail {name}: {e}"
    return err


with ThreadPoolExecutor(16) as ex:
    errs = [e for e in ex.map(fetch, items) if e]
print("done", len(items), "errors", len(errs), flush=True)
for e in errs[:20]:
    print(e)
