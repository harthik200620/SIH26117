"""Read-only compact status from the local strict evaluation database."""

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

db = sqlite3.connect("file:/home/hp/.local/share/blackbox/strict-test/yantra.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
for run in db.execute("SELECT id,status FROM runs ORDER BY created_at DESC LIMIT 1"):
    print(dict(run))
    if "--cancel" in sys.argv and run["status"] in {"running", "planning", "intake"}:
        token = Path("/home/hp/.local/share/blackbox/strict-test/admin.key").read_text().strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            "http://127.0.0.1:7343/api/workbench/runs/" + run["id"] + "/cancel",
            data=b"{}",
            headers={"x-yantra-token": token, "content-type": "application/json"},
        )
        with opener.open(request, timeout=30) as response:
            print(response.status, "cancellation requested")
    for task in db.execute(
        "SELECT title,role,status,outputs,failure_summary FROM tasks WHERE run_id=?", (run["id"],)
    ):
        print(dict(task))
    for call in db.execute(
        "SELECT tool,status,error,args FROM tool_calls WHERE run_id=? ORDER BY started_at",
        (run["id"],),
    ):
        row = dict(call)
        row["args"] = str(row["args"])[:800]
        print(json.dumps(row))
    for span in db.execute(
        "SELECT name,attrs FROM spans WHERE run_id=? AND (name LIKE '%error%' OR name LIKE '%chat%') ORDER BY rowid DESC LIMIT 4",
        (run["id"],),
    ):
        print(str(dict(span))[:1600])
    for verification in db.execute(
        "SELECT attempt,verdict,checks,reviewer FROM verifications WHERE run_id=? ORDER BY created_at DESC LIMIT 2",
        (run["id"],),
    ):
        print("Verification:", str(dict(verification))[:6000])
    for observed in db.execute(
        "SELECT observation FROM steps WHERE run_id=? ORDER BY created_at DESC LIMIT 3",
        (run["id"],),
    ):
        print("Latest observation:", str(observed["observation"])[:2600])
db.close()
