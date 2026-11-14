"""Write the submission image as a `docker save` archive without a Docker daemon (for a build box that has no Docker,
such as a vast.ai container). The result is what `docker build` from docker/Dockerfile followed by `docker save` gives:
the base image's layers, then one layer per COPY/RUN step, with the Dockerfile's ENTRYPOINT, ENV, WORKDIR and CMD.

    python3 assemble.py --base <dir with manifest.json, config.json, blobs/> --tag zaic2026_datamate_<team>:final \
        --layer <src dir>=<path in image>[:deref] ... | pigz > zaic2026_datamate_<team>.tar.gz

The archive holds both layouts `docker save` writes (Docker >= 25): manifest.json for `docker load`, and an OCI layout
(oci-layout, index.json, blobs/sha256/...). Layers are uncompressed tars named by their SHA-256. Every digest and size
is computed in a first pass, so the metadata comes first and the archive streams to stdout in a second pass.
`--layer` sources are walked in sorted order with fixed owner and mtime, so both passes write the same bytes. `:deref`
stores the files that symlinks point to (model weights linked from the Hugging Face cache); otherwise symlinks stay
links (the venv's bin/python -> /usr/bin/python3.12).

`--verify <archive.tar>` (or `-` for stdin) re-reads an archive: every blob's SHA-256 against its name, the manifest
against the config, and the total size in bytes (the guideline's "uncompressed .tar size").
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

MTIME = 1763078400  # 2025-11-14 00:00 UTC: fixed, so two passes (and two builds) write the same bytes
CHUNK = 1 << 20
ENV = ["HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1", "VLLM_NO_USAGE_STATS=1", "DO_NOT_TRACK=1", "PIP_NO_CACHE_DIR=1",
       "NCCL_P2P_DISABLE=1", "PYTHONHASHSEED=0", "UV_PROJECT_ENVIRONMENT=/code/.venv", "UV_LINK_MODE=copy",
       "UV_PYTHON_DOWNLOADS=never", "UV_NO_CACHE=1"]


class Sink:
    """Counts and hashes what is written; passes it on to `out` when given."""

    def __init__(self, out=None):
        self.out, self.n, self.h = out, 0, hashlib.sha256()

    def write(self, b) -> int:
        self.n += len(b)
        self.h.update(b)
        if self.out is not None:
            self.out.write(b)
        return len(b)


def _info(name: str, st: os.stat_result, kind: bytes, size: int = 0, link: str = "") -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type, ti.size, ti.linkname = kind, size, link
    ti.mode = st.st_mode & 0o7777 if kind != tarfile.SYMTYPE else 0o777
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mtime = MTIME
    return ti


def write_layer(out, src: Path, dest: str, deref: bool) -> None:
    """One layer: the parents of `dest`, then `src` placed at `dest` (a path in the image, without the leading /)."""
    tar = tarfile.open(fileobj=out, mode="w|", format=tarfile.GNU_FORMAT)
    parts = dest.strip("/").split("/")
    dirst = os.stat(src)
    for i in range(1, len(parts)):
        tar.addfile(_info("/".join(parts[:i]) + "/", dirst, tarfile.DIRTYPE))
    for root, dirs, files in os.walk(src):
        dirs.sort()
        rel = os.path.relpath(root, src)
        base = dest.strip("/") if rel == "." else f"{dest.strip('/')}/{rel}"
        tar.addfile(_info(base + "/", os.stat(root), tarfile.DIRTYPE))
        for d in list(dirs):  # a symlink to a directory is a link entry, not walked
            p = os.path.join(root, d)
            if os.path.islink(p):
                dirs.remove(d)
                tar.addfile(_info(f"{base}/{d}", os.lstat(p), tarfile.SYMTYPE, link=os.readlink(p)))
        for f in sorted(files):
            p = os.path.join(root, f)
            if os.path.islink(p) and not deref:
                tar.addfile(_info(f"{base}/{f}", os.lstat(p), tarfile.SYMTYPE, link=os.readlink(p)))
                continue
            st = os.stat(p)
            with open(p, "rb") as fh:
                tar.addfile(_info(f"{base}/{f}", st, tarfile.REGTYPE, size=st.st_size), fh)
    tar.close()


def _gunzip(path: Path):
    """A decompressing reader of a base layer blob (pigz when present: several times faster than gzip)."""
    try:
        p = subprocess.Popen(["pigz", "-dc", str(path)], stdout=subprocess.PIPE, bufsize=CHUNK)
        return p.stdout, p
    except FileNotFoundError:
        return gzip.open(path, "rb"), None


def base_layer_digest(path: Path) -> tuple[str, int]:
    sink = Sink()
    r, p = _gunzip(path)
    for b in iter(lambda: r.read(CHUNK), b""):
        sink.write(b)
    if p is not None and p.wait() != 0:
        raise RuntimeError(f"pigz failed on {path}")
    return "sha256:" + sink.h.hexdigest(), sink.n


def _member(out, name: str, size: int, kind: bytes = tarfile.REGTYPE) -> None:
    ti = tarfile.TarInfo(name)
    ti.type, ti.size, ti.mode, ti.mtime = kind, size, 0o755 if kind == tarfile.DIRTYPE else 0o644, MTIME
    out.write(ti.tobuf(format=tarfile.GNU_FORMAT))


def _pad(out, size: int) -> None:
    if size % 512:
        out.write(b"\0" * (512 - size % 512))


def _bytes_member(out, name: str, data: bytes) -> None:
    _member(out, name, len(data))
    out.write(data)
    _pad(out, len(data))


def assemble(base: Path, tag: str, layers: list[tuple[Path, str, bool]], out) -> None:
    manifest = json.loads((base / "manifest.json").read_text())
    config = json.loads((base / "config.json").read_text())
    blobs = [base / "blobs" / l["digest"].split(":", 1)[1] for l in manifest["layers"]]
    if len(blobs) != len(config["rootfs"]["diff_ids"]):
        raise SystemExit("base manifest and config disagree on the layer count")

    # pass 1: digests and sizes
    uniq = sorted(set(blobs))
    with ProcessPoolExecutor(max_workers=min(32, os.cpu_count() or 4)) as ex:
        got = dict(zip(uniq, ex.map(base_layer_digest, uniq)))
    for i, (b, want) in enumerate(zip(blobs, config["rootfs"]["diff_ids"])):
        if got[b][0] != want:
            raise SystemExit(f"base layer {i} ({b.name}) does not match its diff_id")
    ours = []
    for src, dest, deref in layers:
        sink = Sink()
        write_layer(sink, src, dest, deref)
        ours.append(("sha256:" + sink.h.hexdigest(), sink.n))
        print(f"layer {dest}: {sink.n} bytes {ours[-1][0]}", file=sys.stderr, flush=True)

    # image config: the Dockerfile's instructions over the base config
    c = config["config"]
    env = [e for e in c.get("Env") or [] if e.split("=", 1)[0] not in {x.split("=", 1)[0] for x in ENV}]
    c.update(Env=env + ENV, Entrypoint=None, Cmd=["bash", "/code/predict.sh"], WorkingDir="/code")
    config["rootfs"]["diff_ids"] += [d for d, _ in ours]
    created = "2026-11-14T00:00:00Z"
    config["created"] = created
    config["history"] = (config.get("history") or []) + [
        {"created": created, "created_by": "ENTRYPOINT []", "empty_layer": True},
        {"created": created, "created_by": "ENV " + " ".join(ENV), "empty_layer": True},
        {"created": created, "created_by": "WORKDIR /code", "empty_layer": True},
    ] + [{"created": created, "created_by": f"COPY {dest} (docker/assemble.py)"} for _, dest, _ in layers] + [
        {"created": created, "created_by": 'CMD ["bash", "/code/predict.sh"]', "empty_layer": True}]
    cfg = json.dumps(config, separators=(",", ":")).encode()
    cfg_d = "sha256:" + hashlib.sha256(cfg).hexdigest()
    all_layers = [got[b] for b in blobs] + ours
    oci = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                      "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": cfg_d, "size": len(cfg)},
                      "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": d, "size": n}
                                 for d, n in all_layers]}, separators=(",", ":")).encode()
    oci_d = "sha256:" + hashlib.sha256(oci).hexdigest()
    name, _, ref = tag.rpartition(":")
    index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                        "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": oci_d,
                                       "size": len(oci), "annotations": {
                                           "io.containerd.image.name": f"docker.io/library/{name}:{ref}",
                                           "org.opencontainers.image.ref.name": ref}}]}, separators=(",", ":")).encode()
    path = lambda d: "blobs/sha256/" + d.split(":", 1)[1]
    docker = json.dumps([{"Config": path(cfg_d), "RepoTags": [tag], "Layers": [path(d) for d, _ in all_layers]}],
                        separators=(",", ":")).encode()

    # pass 2: the archive, metadata first
    _member(out, "blobs/", 0, tarfile.DIRTYPE)
    _member(out, "blobs/sha256/", 0, tarfile.DIRTYPE)
    _bytes_member(out, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
    _bytes_member(out, "index.json", index)
    _bytes_member(out, "manifest.json", docker)
    _bytes_member(out, path(cfg_d), cfg)
    _bytes_member(out, path(oci_d), oci)
    written = set()
    for b in blobs:
        d, n = got[b]
        if d in written:
            continue
        written.add(d)
        _member(out, path(d), n)
        sink = Sink(out)
        r, p = _gunzip(b)
        for chunk in iter(lambda: r.read(CHUNK), b""):
            sink.write(chunk)
        if p is not None and p.wait() != 0:
            raise RuntimeError(f"pigz failed on {b}")
        if ("sha256:" + sink.h.hexdigest(), sink.n) != (d, n):
            raise SystemExit(f"base layer {b.name} changed between passes")
        _pad(out, n)
    for (src, dest, deref), (d, n) in zip(layers, ours):
        if d in written:
            continue
        written.add(d)
        _member(out, path(d), n)
        sink = Sink(out)
        write_layer(sink, src, dest, deref)
        if ("sha256:" + sink.h.hexdigest(), sink.n) != (d, n):
            raise SystemExit(f"layer {dest} changed between passes")
        _pad(out, n)
    out.write(b"\0" * 1024)
    out.flush()
    print(f"image {tag}: config {cfg_d}, manifest {oci_d}, {len(all_layers)} layers", file=sys.stderr, flush=True)


def verify(f) -> None:
    """Every blob's digest against its name, the manifests against each other, and the archive size."""
    sink = Sink()

    class Reader(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, b):
            data = f.read(len(b))
            sink.write(data)
            b[:len(data)] = data
            return len(data)

    seen, meta = {}, {}
    with tarfile.open(fileobj=io.BufferedReader(Reader(), CHUNK), mode="r|") as tar:
        for m in tar:
            if not m.isfile():
                continue
            h, n = hashlib.sha256(), 0
            r = tar.extractfile(m)
            data = b""
            for b in iter(lambda: r.read(CHUNK), b""):
                h.update(b)
                n += len(b)
                if m.size < 1 << 20:
                    data += b
            if m.name.startswith("blobs/sha256/"):
                ok = h.hexdigest() == m.name.rsplit("/", 1)[1]
                seen[m.name] = n
                if not ok:
                    raise SystemExit(f"digest mismatch: {m.name}")
            if m.size < 1 << 20:
                meta[m.name] = data
    for b in iter(lambda: f.read(CHUNK), b""):  # the end-of-archive blocks
        sink.write(b)
    man = json.loads(meta["manifest.json"])[0]
    cfg = json.loads(meta[man["Config"]])
    missing = [l for l in man["Layers"] if l not in seen]
    if missing or len(man["Layers"]) != len(cfg["rootfs"]["diff_ids"]):
        raise SystemExit(f"manifest/config mismatch, missing {missing}")
    if [l.rsplit("/", 1)[1] for l in man["Layers"]] != [d.split(":")[1] for d in cfg["rootfs"]["diff_ids"]]:
        raise SystemExit("layer order differs from the config's diff_ids")
    c = cfg["config"]
    print(json.dumps({"tag": man["RepoTags"], "layers": len(man["Layers"]), "blobs": len(seen),
                      "os/arch": f"{cfg['os']}/{cfg['architecture']}", "entrypoint": c.get("Entrypoint"),
                      "cmd": c.get("Cmd"), "workdir": c.get("WorkingDir"), "tar_bytes": sink.n}, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path)
    ap.add_argument("--tag")
    ap.add_argument("--layer", action="append", default=[], help="<src dir>=<path in image>[:deref]")
    ap.add_argument("--verify", help="an archive to check, or - for stdin")
    a = ap.parse_args()
    if a.verify:
        with (sys.stdin.buffer if a.verify == "-" else open(a.verify, "rb")) as f:
            verify(f)
        return
    layers = []
    for spec in a.layer:
        src, dest = spec.split("=", 1)
        deref = dest.endswith(":deref")
        layers.append((Path(src), dest.removesuffix(":deref"), deref))
    out = sys.stdout.buffer
    assemble(a.base, a.tag, layers, out)


if __name__ == "__main__":
    main()
