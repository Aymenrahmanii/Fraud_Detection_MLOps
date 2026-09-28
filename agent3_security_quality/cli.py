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
    assess_parser.add_argument(
        "--test-target",
        action="append",
        dest="test_targets",
        help="pytest target; repeatable (default: agent3_security_quality/tests and API/tests)",
    )
    assess_parser.add_argument(
        "--cov-source",
        action="append",
        dest="cov_sources",
        help="coverage source; repeatable (default: agent3_security_quality and API)",
    )
    assess_parser.add_argument("--out", default="release_assessment.json")
    assess_parser.add_argument(
        "--strict",
        action="store_true",
        help="treat a missing/failed scanner as BLOCK instead of a warning (use in CI)",
    )

    args = parser.parse_args(argv)

    if args.command == "assess":
        kwargs = {}
        if args.test_targets:
            kwargs["test_targets"] = tuple(args.test_targets)
        if args.cov_sources:
            kwargs["cov_sources"] = tuple(args.cov_sources)

        assessment = run_assessment(
            requirements_path=args.requirements,
            image_ref=args.image,
            strict=args.strict,
            **kwargs,
        )
        Path(args.out).write_text(assessment.model_dump_json(indent=2))
        print(assessment.model_dump_json(indent=2))
        print(f"\nDecision: {assessment.decision}", file=sys.stderr)
        return 1 if assessment.decision == "BLOCK" else 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
