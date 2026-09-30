"""Save a run's validation record from a local workbench without exposing its key."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

from yantra_server.security import local_endpoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--url", default="http://127.0.0.1:7343")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.run_id.isalnum() or len(args.run_id) > 64:
        parser.error("run_id must be an alphanumeric run identifier")
    url = local_endpoint(args.url).rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    request = urllib.request.Request(
        f"{url}/api/workbench/runs/{args.run_id}/validation-record",
        headers={"x-yantra-token": args.key_file.read_text().strip()},
    )
    with opener.open(request, timeout=30) as response:
        payload = response.read(16_000_001)
    if len(payload) > 16_000_000:
        raise ValueError("Validation record exceeds export limit")
    record = json.loads(payload)
    if record.get("run", {}).get("run_id") != args.run_id:
        raise ValueError("Server returned the wrong run")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("xb") as output:
        output.write(payload)
    print(
        json.dumps(
            {
                "path": str(args.out),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "status": record["run"]["status"],
                "verification_attempts": len(record["verifications"]),
                "model_routes": len(record["model_routes"]),
            }
        )
    )


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    main()
