"""List the default versioned metric capabilities without executing a model."""

import json

from agent6_mlops_lifecycle.evaluation.builtin_metrics import default_metric_registry


def main() -> None:
    print(
        json.dumps(
            [
                definition.model_dump(mode="json")
                for definition in default_metric_registry().definitions()
            ],
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
