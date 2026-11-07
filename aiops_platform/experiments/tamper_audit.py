"""Demo of tamper evidence: alter one audit record behind the platform's back.

    python -m aiops_platform.experiments.tamper_audit demo-state
    python -m aiops_platform.experiments.tamper_audit demo-state --incident INC-...

`demo-state` is the --workdir of `python -m aiops_platform.demo serve`. The script
rewrites who approved a workflow directly in the database, bypassing the Context
Service; the dashboard then reports that workflow's hash chain as broken. Use it
only on a throwaway demo state directory.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workdir", type=Path, help="the --workdir given to `demo serve`")
    parser.add_argument("--incident", help="workflow ID (default: the most recently approved one)")
    parser.add_argument("--approver", default="mallory@attacker", help="the forged approver")
    args = parser.parse_args(argv)

    db_path = args.workdir / "context.db"
    if not db_path.exists():
        print(f"{db_path} not found: start `demo serve --workdir {args.workdir}` and approve a workflow first")
        return 1
    db = sqlite3.connect(db_path)
    try:
        query = "SELECT seq, incident_id, detail_json FROM audit_log WHERE action = 'APPROVAL_APPROVED'"
        params: tuple[str, ...] = ()
        if args.incident:
            query += " AND incident_id = ?"
            params = (args.incident,)
        row = db.execute(query + " ORDER BY seq DESC LIMIT 1", params).fetchone()
        if row is None:
            print("No approved workflow yet: approve one in the dashboard first.")
            return 1
        seq, incident, detail = row
        record = json.loads(detail)
        before = record.get("approver_id")
        record["approver_id"] = args.approver
        db.execute("UPDATE audit_log SET detail_json = ? WHERE seq = ?", (json.dumps(record), seq))
        db.commit()
    finally:
        db.close()
    print(f"{incident}: audit record {seq} now says approved by {args.approver} (was {before}).")
    print("Reload that workflow in the dashboard: its audit trail reports the hash chain as broken.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
