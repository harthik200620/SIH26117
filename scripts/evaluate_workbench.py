"""Evaluate real local work against independent fixture checks; never uses mock output."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from yantra_server.security import local_endpoint  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Evaluation API redirects are disabled")


def check_output(case: str, path: Path) -> dict[str, bool]:
    if not path.is_file():
        return {"file_exists": False}
    text = path.read_text(encoding="utf-8")
    if case == "exact":
        return {"file_exists": True, "exact_bytes": text == "Hello from YANTRA"}
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return {"valid_json": False}
        if case == "code":
            value = data.get("sum_of_squares")
            return {
                "valid_json": True,
                "sum_of_squares": type(value) is int and value == sum(n * n for n in range(1, 11)),
            }
        sources = data.get("sources")
        assumptions = data.get("assumptions")
        checks = {
            "valid_json": True,
            "hydraulic_power": type(data.get("hydraulic_kw")) in (int, float)
            and math.isclose(data["hydraulic_kw"], 2.1582, rel_tol=0, abs_tol=0.0001),
            "shaft_power": type(data.get("shaft_kw")) in (int, float)
            and math.isclose(data["shaft_kw"], 3.083142857142857, rel_tol=0, abs_tol=0.0001),
            "synthetic_label": data.get("synthetic") is True,
            "source_files": isinstance(sources, list)
            and all(name in sources for name in ("inspection-P101.md", "maintenance-procedure.md")),
            "oem_limit_unknown": data.get("oem_status")
            == "OEM limits required; compliance not determined",
            "assumptions_present": isinstance(assumptions, list)
            and bool(assumptions)
            and all(isinstance(item, str) and bool(item.strip()) for item in assumptions),
        }
        return checks
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"structured_output": False}


def check_execution(evidence: dict) -> bool:
    return any(
        call.get("tool") == "python"
        and call.get("status") == "done"
        and call.get("result_available") is True
        and call.get("ok") is True
        and call.get("exit_code") == 0
        and call.get("timed_out") is False
        and call.get("backend") in {"bwrap", "docker"}
        for call in evidence.get("calls", [])
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7331")
    parser.add_argument("--workspace", type=Path, default=ROOT / "workspace")
    parser.add_argument("--model", help="Actual model ID from /api/workbench/status")
    parser.add_argument("--case", choices=["exact", "pump", "code"], default="exact")
    parser.add_argument(
        "--token-file",
        type=Path,
        help="Read a local API access key without putting it in process arguments",
    )
    parser.add_argument("--run-id", help="Inspect an existing run instead of starting a new one")
    parser.add_argument("--timeout", type=int, default=1250)
    parser.add_argument("--output", type=Path, default=ROOT / ".yantra/evaluation.json")
    args = parser.parse_args()
    base = local_endpoint(args.url).rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    headers = {"Content-Type": "application/json"}
    if token := os.environ.get("YANTRA_SERVER__ADMIN_TOKEN"):
        headers["x-yantra-token"] = token
    if args.token_file:
        headers["x-yantra-token"] = args.token_file.read_text(encoding="utf-8").strip()

    def api(path: str, body=None):
        request = urllib.request.Request(
            base + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    deployment = api("/api/workbench/status")
    if deployment.get("profile") == "mock":
        raise RuntimeError("Real-model evaluation refuses the mock deployment profile")
    started = time.monotonic()
    run_id = args.run_id
    if not run_id:
        # A fresh subdirectory prevents a previous result from passing the next run.
        workspace = args.workspace.resolve() / ("evaluation-" + uuid.uuid4().hex[:10])
        workspace.mkdir(parents=True)
        if args.case == "pump":
            for name in ("inspection-P101.md", "maintenance-procedure.md"):
                (workspace / name).write_bytes((ROOT / "workspace" / name).read_bytes())
            goal = (
                "Read inspection-P101.md and maintenance-procedure.md. Use calculate_quantity with original source values and units to compute hydraulic and shaft power. "
                "Write pump-result.json with keys hydraulic_kw, shaft_kw (numbers), sources (source filenames), assumptions (list), synthetic (true), "
                "and oem_status (exactly 'OEM limits required; compliance not determined'). "
                "Keep the JSON concise. Do not invent acceptance limits."
            )
        elif args.case == "code":
            goal = (
                "Use the python tool to calculate the sum of squares of integers 1 through 10 inclusive and write result.json as a JSON object with key sum_of_squares. "
                "The Python code must actually execute. Report the result and whether execution succeeded. This is a synthetic sandbox validation."
            )
        else:
            goal = "Write verification.txt containing exactly Hello from YANTRA"
        run_id = api(
            "/api/workbench/runs",
            {"workspace": str(workspace), "goal": goal, "model": args.model, "mode": "auto"},
        )["run_id"]
    last_status = None
    while True:
        row = api("/api/workbench/runs/" + urllib.parse.quote(run_id, safe=""))
        if row["status"] != last_status:
            print(f"{run_id}: {row['status']}", flush=True)
            last_status = row["status"]
        if row["status"] in {"done", "done_with_gaps", "failed", "cancelled", "planned"}:
            break
        if time.monotonic() - started > args.timeout:
            if not args.run_id:
                api(f"/api/workbench/runs/{run_id}/cancel", {})
            raise TimeoutError("Evaluation timed out; no pass recorded")
        time.sleep(2)
    workspace = Path(row["workspace"])
    artifact = (
        workspace
        / {"exact": "verification.txt", "pump": "pump-result.json", "code": "result.json"}[
            args.case
        ]
    )
    checks = check_output(args.case, artifact)
    execution = None
    if args.case == "code":
        execution = api(
            f"/api/workbench/runs/{urllib.parse.quote(run_id, safe='')}/execution-evidence"
        )
        checks["isolated_python_execution"] = check_execution(execution)
    report = {
        "case": args.case,
        "run_id": run_id,
        "status": row["status"],
        "passed": row["status"] == "done" and all(checks.values()),
        "checks": checks,
        "execution_evidence": execution,
        "deployment": {
            "profile": deployment.get("profile"),
            "os_isolation_verified": deployment.get("security", {}).get(
                "os_isolation_verified", False
            ),
        },
        "usage": row.get("budget_used", {}),
        "artifact": str(artifact),
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()
        if artifact.is_file()
        else None,
        "final": row.get("final"),
        "scope": "Synthetic fixture only. No OS egress or general industrial accuracy attestation.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
        print(f"Evaluation failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
