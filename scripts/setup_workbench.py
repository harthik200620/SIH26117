"""Explicit connected provisioning. Never imported by the running workbench.

Downloads public model weights only, pins the resolved revision, verifies the upstream
LFS SHA256, then atomically promotes the file. It never reads workspace documents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def fetch(url: str, target: Path, expected_sha: str, size: int | None = None):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        with target.open("rb") as existing:
            if hashlib.file_digest(existing, "sha256").hexdigest() == expected_sha:
                print(f"Already verified: {target.name}")
                return
        raise RuntimeError(
            f"Existing file differs from upstream: {target}. Move it aside before retrying."
        )
    if size and shutil.disk_usage(target.parent).free < size + 2 * 1024**3:
        raise RuntimeError("Insufficient free disk space (requires download size plus 2 GB)")
    partial = target.with_suffix(target.suffix + ".partial")
    digest = hashlib.sha256()
    count = 0
    with urllib.request.urlopen(url, timeout=90) as response, partial.open("wb") as output:
        while chunk := response.read(4 * 1024**2):
            output.write(chunk)
            digest.update(chunk)
            count += len(chunk)
            print(f"\r{target.name}: {count / 1024**2:.0f} MB downloaded", end="", flush=True)
    print()
    if digest.hexdigest() != expected_sha or (size and count != size):
        raise RuntimeError(f"Integrity check failed. Quarantined download: {partial}")
    partial.replace(target)
    print(f"SHA256 verified: {expected_sha}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download", help="Catalog model id. Explicitly enables this one download."
    )
    parser.add_argument(
        "--runtime", action="store_true", help="Download a pinned native llama.cpp runtime"
    )
    parser.add_argument("--backend", choices=["cpu", "vulkan", "cuda", "metal"], default="cpu")
    parser.add_argument("--models-dir", type=Path, default=ROOT / "models/weights")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / ".yantra/runtime")
    args = parser.parse_args()
    catalog = json.loads((ROOT / "models/catalog.json").read_text(encoding="utf-8"))
    if not args.download and not args.runtime:
        print("Offline workbench setup. These are optional connected provisioning actions:")
        print("  python scripts/setup_workbench.py --runtime")
        for item in catalog:
            print(
                f"  {item['id']:24} ~{item['runtime_gb']} GB runtime estimate | https://huggingface.co/{item['repo']}"
            )
        return
    # The separate provisioner is the only component allowed to unseal itself,
    # and only after an explicit download flag. It never imports application state.
    try:
        from yantra_server.seal.socket_guard import uninstall

        uninstall()
    except ImportError:
        pass
    if args.runtime:
        system, machine = platform.system(), platform.machine().lower()
        arch = (
            "arm64"
            if machine in {"arm64", "aarch64"}
            else "x64"
            if machine in {"amd64", "x86_64"}
            else None
        )
        if arch is None:
            raise RuntimeError(
                "No bundled runtime for this architecture. Build llama.cpp from the linked upstream source."
            )
        if system == "Darwin":
            suffix = f"macos-{arch}.tar.gz"
        elif system == "Windows":
            backend = (
                "cuda-12.4"
                if args.backend == "cuda" and arch == "x64"
                else "cuda-13.4"
                if args.backend == "cuda"
                else args.backend
            )
            suffix = f"win-{backend}-{arch}.zip"
        elif system == "Linux":
            backend = (
                "cuda-12.8-"
                if args.backend == "cuda" and arch == "x64"
                else "cuda-13.3-"
                if args.backend == "cuda"
                else "vulkan-"
                if args.backend == "vulkan"
                else ""
            )
            suffix = f"ubuntu-{backend}{arch}.tar.gz"
        else:
            raise RuntimeError(
                "Unsupported prebuilt runtime platform; supply --llama-server at startup"
            )
        release = get_json("https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b11048")
        names = ["llama-b11048-bin-" + suffix]
        if args.backend == "cuda":
            names.append(
                ("cudart-llama-bin-" if system == "Windows" else "cudart-llama-b11048-bin-")
                + suffix
            )
        dest = args.runtime_dir
        for name in names:
            asset = next((a for a in release["assets"] if a["name"] == name), None)
            if not asset or not str(asset.get("digest", "")).startswith("sha256:"):
                raise RuntimeError(
                    f"No verifiable runtime archive: {name}. Use the upstream releases page."
                )
            archive = dest / name
            fetch(
                asset["browser_download_url"],
                archive,
                asset["digest"].split(":", 1)[1],
                asset["size"],
            )
            destination = dest / "llama"
            destination.mkdir(parents=True, exist_ok=True)
            if name.endswith(".zip"):
                with zipfile.ZipFile(archive) as bundle:
                    for member in bundle.infolist():
                        if (
                            not (destination / member.filename)
                            .resolve()
                            .is_relative_to(destination.resolve())
                        ):
                            raise RuntimeError("Unsafe archive path")
                    bundle.extractall(destination)
            else:
                with tarfile.open(archive) as bundle:
                    bundle.extractall(destination, filter="data")
            (dest / (name + ".provenance.json")).write_text(
                json.dumps(
                    {
                        "release": "b11048",
                        "asset": name,
                        "sha256": asset["digest"],
                        "bytes": asset["size"],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        binaries = list(
            (dest / "llama").rglob("llama-server.exe" if os.name == "nt" else "llama-server")
        )
        if binaries:
            print(f"Runtime installed: {binaries[0]} (supply --llama-server if needed)")
    if args.download:
        item = next((m for m in catalog if m["id"] == args.download), None)
        if not item or not item["file"]:
            raise RuntimeError(
                "Unknown model or multi-file vision model. Use its model card for manual import."
            )
        info = get_json(f"https://huggingface.co/api/models/{item['repo']}?blobs=true")
        sibling = next((f for f in info["siblings"] if f["rfilename"] == item["file"]), None)
        if not sibling or not sibling.get("lfs", {}).get("sha256"):
            raise RuntimeError("Upstream file/checksum unavailable; download refused")
        sha, size = sibling["lfs"]["sha256"], sibling["lfs"]["size"]
        url = f"https://huggingface.co/{item['repo']}/resolve/{info['sha']}/{item['file']}"
        target = args.models_dir / item["file"]
        fetch(url, target, sha, size)
        target.with_suffix(".provenance.json").write_text(
            json.dumps(
                {
                    "repo": item["repo"],
                    "revision": info["sha"],
                    "file": item["file"],
                    "sha256": sha,
                    "bytes": size,
                    "license": item["license"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    print(
        "Provisioning finished. Disconnect the network, then run python scripts/run_workbench.py. The workbench does not download dependencies or models."
    )


if __name__ == "__main__":
    main()
