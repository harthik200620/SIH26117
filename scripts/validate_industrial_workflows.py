"""Run source-only industrial fixtures through the real local workbench API.

Run in Linux beside the strict deployment. Answers stay in this evaluator,
outside the workload's mounted workspace. No mock engine or prewritten outputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "90_evaluation/live_workflows"

PROMPTS = {
    "scan": (
        "Read page 1 of inspection.pdf using read_pages and page 1 of rules.pdf. "
        "Create inspection-note.docx with render_document, schema_id report. "
        "Use calculate or Python to compute every indicated-minus-reference error and compare absolute errors with the controlled tolerance. "
        "Include all five readings and errors. "
        "State as-found disposition, missing closure evidence and cite each source filename and page. "
        "This is synthetic; do not claim adjustment, approval or independent review. "
        "Report JSON has title, executive_summary, sections [{heading,paragraphs,tables:[{columns,rows}]}], "
        "appendix {citations,assumptions,unverified_claims}. Table rows contain strings. Pass data_json as an object, not an escaped string."
    ),
    "tender": (
        "First call read_file with path offers.csv. Then call read_pages with path rules.pdf and pages 1. "
        "Then call render_document directly to create tender-comparison.xlsx. Do not use Python or write a helper script for this spreadsheet. Use "
        "schema_id csv_table with source_path offers.csv. Compare all offers by technical eligibility, landed INR total and delivery. "
        "Use spreadsheet formulas referencing each offer's basic, freight and tax cells for totals. "
        "Select the eligible preferred offer with the controlled tie-breaker; explain excluded offers and pending award approval. "
        "Cite source file and row or page. Label synthetic. CSV_table JSON has title, source_path, numeric_columns (exact source headers), computed_columns [{name,expression}], summary and appendix. "
        "The renderer loads every source row automatically. Provide named-column expressions for calculations, not calculated numbers. Pass data_json as an object."
    ),
    "code": (
        "Use the python tool to create and execute pump_calculation.py and write result.json. "
        "First call read_file with path duty.csv to inspect its actual rows and columns. "
        "Calculate hydraulic_kw, shaft_kw, electrical_kw and shaft_margin_kw against motor nameplate, converting m3/h to m3/s. "
        "Save assertions testing unit conversion, energy balance and rejection of zero efficiency in the Python file and execute them. "
        "JSON must include synthetic:true, sources:[duty.csv], and an explicit statement that duty-point margin is not operational approval. "
        "Use supplied gravity and efficiencies; report actual execution."
    ),
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=list(PROMPTS))
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--model", default="qwen3-4b-instruct-awq-gguf")
    parser.add_argument("--model-file", type=Path, help="Record the local weight file hash")
    parser.add_argument("--prompt-style", choices=["guided", "plain"], default="guided")
    parser.add_argument("--variant", choices=["original", "reordered"], default="original")
    args = parser.parse_args()
    if args.case != "tender" and (args.prompt_style != "guided" or args.variant != "original"):
        parser.error("Plain prompts and reordered sources currently apply to tender only")
    prompt = PROMPTS[args.case]
    if args.prompt_style == "plain":
        prompt = (
            "Read offers.csv and rules.pdf. Create tender-comparison.xlsx comparing all offers. "
            "Keep original source inputs and use spreadsheet formulas referencing those inputs for "
            "landed INR totals and eligibility. Apply the controlled rule and tie-breaker to recommend "
            "the preferred eligible offer. Explain exclusions and pending award approval. "
            "Cite the source file and row or page, and label the comparison synthetic."
        )
    private = Path("/home/hp/.local/share/blackbox/strict-test")
    token = (private / "admin.key").read_text().strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(path, body=None):
        req = urllib.request.Request(
            "http://127.0.0.1:7343" + path,
            headers={"x-yantra-token": token, "content-type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with opener.open(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode()}") from exc

    status = api("/api/workbench/status")
    print(json.dumps({k: status.get(k) for k in ("profile", "models")}), flush=True)
    if status.get("profile") == "mock" or not status.get("security", {}).get(
        "os_isolation_verified"
    ):
        raise RuntimeError("A real model and verified strict namespace are required")
    workspace = Path("/home/hp/.local/share/blackbox/validation-workspace") / (
        "industrial-" + args.case + "-" + uuid.uuid4().hex[:10]
    )
    workspace.mkdir()
    sources = {
        "scan": {
            "inspection.pdf": "17_controlled_challenges/scans/MRPL-CASE-04-image-only.pdf",
            "rules.pdf": "14_connected_MRPL_cases/MRPL-CASE-04/controlled_rules_R02.pdf",
        },
        "tender": {
            "offers.csv": "14_connected_MRPL_cases/MRPL-CASE-02/observations.csv",
            "rules.pdf": "14_connected_MRPL_cases/MRPL-CASE-02/controlled_rules_R02.pdf",
        },
        "code": {"duty.csv": "14_connected_MRPL_cases/MRPL-CASE-01/observations.csv"},
    }[args.case]
    for name, path in sources.items():
        shutil.copy2(DATA / path, workspace / name)
    if args.variant == "reordered":
        path = workspace / "offers.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = list(reversed(reader.fieldnames or []))
            rows = list(reader)
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(list(reversed(rows)))
    started = time.monotonic()
    run_id = api(
        "/api/workbench/runs",
        {
            "workspace": str(workspace),
            "mode": "auto",
            "model": args.model,
            "goal": prompt,
        },
    )["run_id"]
    OUT.mkdir(parents=True, exist_ok=True)
    folder = OUT / run_id
    folder.mkdir()
    report = {
        "case": args.case,
        "requested_model": args.model,
        "run_id": run_id,
        "workspace": str(workspace),
        "prompt": prompt,
        "prompt_style": args.prompt_style,
        "variant": args.variant,
        "sources": sources,
        "input_sha256": {n: digest(workspace / n) for n in sources},
        "deployment_before": status,
        "independent_review": "pending",
        "runtime_packages": {
            distribution.metadata["Name"]: distribution.version
            for distribution in importlib.metadata.distributions()
        },
    }
    report["implementation_sha256"] = {
        str(p.relative_to(ROOT)): digest(p)
        for p in [
            ROOT / "server/yantra_server/conductor/executor.py",
            ROOT / "server/yantra_server/conductor/planner.py",
            ROOT / "server/yantra_server/conductor/verifier.py",
            ROOT / "server/yantra_server/gateway/structured.py",
            ROOT / "server/yantra_server/knowledge/ingest/parse.py",
            ROOT / "server/yantra_server/knowledge/ingest/ocr.py",
            ROOT / "server/yantra_server/knowledge/index/lexical.py",
            ROOT / "server/yantra_server/knowledge/index/sqlite_lexical.py",
            ROOT / "server/yantra_server/tools/builtin/knowledge.py",
            ROOT / "server/yantra_server/tools/builtin/fs.py",
            ROOT / "server/yantra_server/tools/builtin/render.py",
            ROOT / "server/yantra_server/render/schemas.py",
            ROOT / "server/yantra_server/render/service.py",
            ROOT / "server/yantra_server/render/workbook_checks.py",
            ROOT / "server/yantra_server/render/column_formulas.py",
            ROOT / "server/yantra_server/render/csv_binding.py",
            ROOT / "server/yantra_server/render/expression_syntax.py",
            ROOT / "server/yantra_server/render/expression_types.py",
        ]
    }
    if args.model_file:
        report["model_weight_evidence"] = {
            "path": str(args.model_file.resolve()),
            "bytes": args.model_file.stat().st_size,
            "sha256": digest(args.model_file),
        }
    (folder / "run.json").write_text(json.dumps(report, indent=2))
    previous = None
    while True:
        row = api("/api/workbench/runs/" + run_id)
        if row["status"] != previous:
            print(run_id, row["status"], flush=True)
            previous = row["status"]
        if row["status"] in {
            "done",
            "done_with_gaps",
            "failed",
            "cancelled",
            "interrupted",
            "planned",
        }:
            break
        if time.monotonic() - started > args.timeout:
            api(f"/api/workbench/runs/{run_id}/cancel", {})
            report["timed_out"] = True
            # Wait for cancellation to settle before copying mutable outputs.
            for _ in range(20):
                row = api("/api/workbench/runs/" + run_id)
                if row["status"] not in {"intake", "planning", "running", "verifying"}:
                    break
                time.sleep(1)
            report["snapshot_after_terminal_status"] = row["status"] in {
                "done",
                "done_with_gaps",
                "failed",
                "cancelled",
                "interrupted",
                "planned",
            }
            break
        time.sleep(3)
    report.update(
        run=row,
        elapsed_seconds=round(time.monotonic() - started, 2),
        execution_evidence=api(f"/api/workbench/runs/{run_id}/execution-evidence"),
        deployment_after=api("/api/workbench/status"),
    )
    report["source_bytes_unchanged"] = all(
        digest(workspace / n) == h for n, h in report["input_sha256"].items()
    )
    report["implementation_unchanged"] = all(
        digest(ROOT / name) == value for name, value in report["implementation_sha256"].items()
    )
    with sqlite3.connect(f"file:{private / 'yantra.db'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        report["tool_calls"] = [
            dict(row)
            for row in db.execute(
                "SELECT tool,status,args,error,sandbox,started_at,finished_at FROM tool_calls WHERE run_id=? ORDER BY started_at",
                (run_id,),
            )
        ]
        report["observations"] = [
            dict(row)
            for row in db.execute(
                "SELECT task_id,n,observation,status FROM steps WHERE run_id=? ORDER BY created_at",
                (run_id,),
            )
        ]
    shutil.copytree(workspace, folder / "workspace")
    report["output_sha256"] = {
        str(p.relative_to(workspace)): digest(p) for p in workspace.rglob("*") if p.is_file()
    }
    report["scope"] = (
        "Known synthetic development fixtures; prompt style and source variant are recorded. "
        "Reordering is a development perturbation, not independent unseen accuracy."
    )
    (folder / "run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(folder / "run.json"),
                "status": row["status"],
                "seconds": report["elapsed_seconds"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
