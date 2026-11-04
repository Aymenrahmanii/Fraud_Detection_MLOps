# Synthetic versioned metric-policy examples

Run python -m agent6_mlops_lifecycle.cli.metric_policy_demo from the repository root.

The support-routing fixture passes precision, ROC AUC and Brier but fails required
recall, so its overall result is FAIL. The energy-demand fixture passes MAE and R2.
Both include a declared coverage denominator and are saved/evaluated through SQLite
references. The fixtures and thresholds are illustrative, not calibrated production data.

See agent6_mlops_lifecycle/docs/architecture.md for contracts, existing-policy
compatibility, plugin responsibilities and task families that remain unsupported.
