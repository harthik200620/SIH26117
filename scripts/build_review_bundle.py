"""Package an allowlisted development evidence snapshot without secrets or model weights."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    "READINESS_2026-09-29.md",
    "REVIEW_BUNDLE.md",
    "UPGRADE_REPORT_2026-09-28.md",
    "SIH26117_REQUIREMENTS.md",
    "ROUND2_SUBMISSION_BRIEF.md",
    "PITCH_CURRENT.md",
    "DELIVERABLE_VALIDATION.md",
    "STRICT_DEPLOYMENT.md",
]
RUN_FILES = [
    "numeric-checks.json",
    "evaluation.json",
    "post_fix_check.json",
    "artifact_inspection.json",
    "validation-record.json",
    "recalculation/recalculated-values.json",
    "recalculation/engine-record.json",
    "recalculation/formulas.json",
    "recalculation/workbook.png",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    files = [ROOT / "docs" / name for name in DOCUMENTS]
    files += [
        ROOT / "data/LIVE_WORKFLOW_REPORT.md",
        ROOT / "data/00_catalog/live_recovery_summary.json",
    ]
    validation = ROOT / "docs/validation"
    files += [
        validation / name
        for name in [
            "source-binding-2026-09-29-final-tests.txt",
            "source-binding-2026-09-29-conditional-tests.txt",
            "source-binding-2026-09-29-review-tests.txt",
            "source-binding-2026-09-29-final-types-tests.txt",
            "source-binding-2026-09-29-types.txt",
            "qwen3-4b-instruct-2507-provenance.json",
            "upgrade-2026-09-28-final-tests.txt",
            "formula-review-2026-09-28-tests.txt",
            "access-screen-2026-09-28.md",
            "render-snapshots-2026-09-28-tests.txt",
            "revision-export-2026-09-29-tests.txt",
            "upgrade-2026-09-29-tests.txt",
            "retry-sources-2026-09-29-tests.txt",
            "network-2026-09-28/namespace-headers-20260928T134140Z.json",
            "network-2026-09-28/namespace-headers-20260928T134140Z.pcap",
            "network-2026-09-28/namespace-processes.json",
            "computed-columns-2026-09-28/expected-values.json",
            "computed-columns-2026-09-28/recalculation/recalculated-values.json",
            "computed-columns-2026-09-28/recalculation/formulas.json",
            "computed-columns-2026-09-28/recalculation/workbook.png",
        ]
    ]
    for folder in sorted((ROOT / "data/90_evaluation/live_workflows").iterdir()):
        if folder.is_dir():
            files += [folder / name for name in RUN_FILES if (folder / name).is_file()]
    entries = []
    payloads = []
    for path in sorted(set(files)):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(ROOT):
            raise ValueError(f"Bundle input escaped workspace: {path.name}")
        if resolved.stat().st_size > 16_000_000:
            raise ValueError(f"Unexpectedly large evidence file: {path.name}")
        content = resolved.read_bytes()
        name = path.relative_to(ROOT).as_posix()
        payloads.append((name, content))
        entries.append(
            {"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        )
    manifest = {
        "created_utc": datetime.now(UTC).isoformat(),
        "status": "Development review; official submission compliance unverified",
        "independent_review": "pending",
        "files": entries,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("MANIFEST.json", json.dumps(manifest, indent=2))
        for name, content in payloads:
            archive.writestr(name, content)
    with zipfile.ZipFile(args.out) as archive:
        assert archive.testzip() is None
        for entry in entries:
            assert hashlib.sha256(archive.read(entry["path"])).hexdigest() == entry["sha256"]
    print(
        json.dumps(
            {
                "archive": str(args.out.resolve()),
                "files": len(entries),
                "sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
