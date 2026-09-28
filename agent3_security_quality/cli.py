"""CLI entrypoint: python -m agent3_security_quality.cli assess [options]

Writes release_assessment.json and exits non-zero when the decision is
BLOCK, so it can gate a CI job on its own exit code.
"""

import argparse
import sys
from pathlib import Path

from agent3_security_quality.core.assess import run_assessment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent3")
    subparsers = parser.add_subparsers(dest="command", required=True)

    assess_parser = subparsers.add_parser("assess", help="run all scanners and produce a release assessment")
    assess_parser.add_argument("--image", default="", help="container image ref for Trivy (skipped if omitted)")
    assess_parser.add_argument("--requirements", default="requirements.txt")
    assess_parser.add_argument("--test-target", default="agent3_security_quality/tests")
    assess_parser.add_argument("--cov-source", default="agent3_security_quality")
    assess_parser.add_argument("--out", default="release_assessment.json")
    assess_parser.add_argument(
        "--strict",
        action="store_true",
        help="treat a missing/failed scanner as BLOCK instead of a warning (use in CI)",
    )

    args = parser.parse_args(argv)

    if args.command == "assess":
        assessment = run_assessment(
            test_target=args.test_target,
            cov_source=args.cov_source,
            requirements_path=args.requirements,
            image_ref=args.image,
            strict=args.strict,
        )
        Path(args.out).write_text(assessment.model_dump_json(indent=2))
        print(assessment.model_dump_json(indent=2))
        print(f"\nDecision: {assessment.decision}", file=sys.stderr)
        return 1 if assessment.decision == "BLOCK" else 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
