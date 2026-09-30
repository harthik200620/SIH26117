#!/usr/bin/env bash
# Build the offline bundle tar (SPEC §21) on a CONNECTED Linux machine with docker.
#
#   bash scripts/bundle.sh <profile>            # lite | standard | refinery
#   YANTRA_SKIP_IMAGES=1 bash scripts/bundle.sh lite   # dry bundle without docker saves
#
# Produces bundle/yantra-<version>-<profile>.tar containing: docker images, python
# wheelhouse, dashboard build, assets, models for the profile (fetched beforehand with
# scripts/fetch_models.py), install.sh, MANIFEST.sha256, SBOM.json. Prints the size.
set -euo pipefail

PROFILE="${1:-lite}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(python -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")"
STAGE="bundle/stage-$PROFILE"
OUT="bundle/yantra-$VERSION-$PROFILE.tar"

echo "== yantra bundle $VERSION ($PROFILE) =="
rm -rf "$STAGE"
mkdir -p "$STAGE"

# 1. Python wheelhouse (server + serving extras + their transitive closure).
echo "-- wheelhouse"
python -m pip wheel --wheel-dir "$STAGE/wheelhouse" ".[knowledge,vision,render,analysis,ocr]" >/dev/null

# 2. Docker images: server (built here) + the third-party images this profile needs.
if [ "${YANTRA_SKIP_IMAGES:-0}" != "1" ]; then
  echo "-- docker images"
  docker build -t "yantra-server:$VERSION" -t yantra-server:latest .
  IMAGES=("yantra-server:$VERSION" "qdrant/qdrant:v1.12.4")
  case "$PROFILE" in
    lite)     IMAGES+=("vllm/vllm-openai:v0.11.0" "ghcr.io/ggml-org/llama.cpp:server") ;;
    standard) IMAGES+=("vllm/vllm-openai:v0.11.0") ;;
    refinery) IMAGES+=("vllm/vllm-openai:v0.11.0" "postgres:17-alpine") ;;
  esac
  for img in "${IMAGES[@]}"; do docker pull "$img" >/dev/null 2>&1 || true; done
  docker save "${IMAGES[@]}" -o "$STAGE/images.tar"
fi

# 3. Assets + code the host needs outside the image (compose file, profiles, scripts, docs).
echo "-- assets"
mkdir -p "$STAGE/app"
cp -r docker-compose.yml models agents knowledge templates skills tools corpus docs \
      scripts/install.sh scripts/seal_nftables.sh scripts/fetch_models.py "$STAGE/app/"
mv "$STAGE/app/install.sh" "$STAGE/install.sh"

# 4. Models for the profile (must have been fetched already on this machine).
MODELS_SRC="${YANTRA_MODELS_DIR:-models/weights}"
echo "-- models from $MODELS_SRC"
mkdir -p "$STAGE/models-weights"
python - "$PROFILE" "$MODELS_SRC" "$STAGE/models-weights" <<'PY'
import shutil, sys, yaml
from pathlib import Path
profile, src, dst = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
reg = {m["id"]: m["path"] for m in yaml.safe_load(Path("models/registry.yaml").read_text())
       if m.get("path") and m["path"] != "-"}
prof = yaml.safe_load((Path("models/profiles") / f"{profile}.yaml").read_text())
wanted = []
for e in prof.get("engines", []):
    wanted += [m for m in [e.get("model"), *e.get("models", [])] if m]
missing = []
for mid in dict.fromkeys(wanted):
    p = src / reg.get(mid, "?")
    if p.exists():
        dest = dst / p.name
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, dest)
        print(f"   + {mid}")
    else:
        missing.append(mid)
if (src / "MODELS.sha256").is_file():
    shutil.copy2(src / "MODELS.sha256", dst / "MODELS.sha256")
if missing:
    print("   ! missing (fetch first with scripts/fetch_models.py):", ", ".join(missing))
PY

# 5. SBOM: python wheels + dashboard npm packages, CycloneDX-shaped.
echo "-- SBOM"
python - "$STAGE" "$VERSION" <<'PY'
import json, re, subprocess, sys
from pathlib import Path
stage, version = Path(sys.argv[1]), sys.argv[2]
components = []
for whl in sorted((stage / "wheelhouse").glob("*.whl")):
    name, ver = whl.name.split("-")[0], whl.name.split("-")[1]
    components.append({"type": "library", "name": name, "version": ver, "purl": f"pkg:pypi/{name}@{ver}"})
try:
    npm = json.loads(subprocess.run(["npm", "ls", "--prefix", "web", "--all", "--json"],
                                    capture_output=True, text=True).stdout or "{}")
    def walk(deps):
        for name, info in (deps or {}).items():
            if v := info.get("version"):
                components.append({"type": "library", "name": name, "version": v,
                                   "purl": f"pkg:npm/{name}@{v}"})
            walk(info.get("dependencies"))
    walk(npm.get("dependencies"))
except (OSError, json.JSONDecodeError):
    pass
sbom = {"bomFormat": "CycloneDX", "specVersion": "1.5",
        "metadata": {"component": {"type": "application", "name": "yantra", "version": version}},
        "components": components}
(stage / "SBOM.json").write_text(json.dumps(sbom, indent=2))
print(f"   {len(components)} components")
PY

# 6. Manifest of every file in the stage, then tar.
echo "-- MANIFEST.sha256"
( cd "$STAGE" && find . -type f ! -name MANIFEST.sha256 -print0 \
  | sort -z | xargs -0 sha256sum > MANIFEST.sha256 )
echo "-- tar"
tar -cf "$OUT" -C "$STAGE" .
SIZE=$(du -h "$OUT" | cut -f1)
echo "== bundle ready: $OUT ($SIZE) =="
echo "   install on the air-gapped host:  tar -xf $(basename "$OUT") && sudo bash install.sh"
