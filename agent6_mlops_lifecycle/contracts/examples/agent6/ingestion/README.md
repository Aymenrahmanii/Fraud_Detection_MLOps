# Synthetic observation ingestion fixtures

Two model policies are reused from ../projects/. Each JSONL source contains only
one tenant/project/model/version. Predictions contain an exact duplicate retry;
outcomes are deliberately out of order. Early and late configurations evaluate
the same prediction window with different availability cutoffs.

Run python -m agent6_mlops_lifecycle.cli.ingestion_demo from the repository root.
See agent6_mlops_lifecycle/docs/architecture.md for the contracts and expected output.
These five-prediction examples are pipeline fixtures, not production quality evidence.
