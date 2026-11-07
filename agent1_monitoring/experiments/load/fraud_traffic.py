"""Steady background traffic for the fraud-detection API (standard library only).

Sends realistic transactions to POST /predict, with values in the ranges of the project's
hold-out data (Category, paymentMethod, account ages, ...).

Two real bugs of the API, found by Agent 1 on 2026-10-16 (HTTP 500), since fixed in main:
1. a payment method older than one day (`paymentMethodAgeDays` >= 2);
2. a missing `Category` (allowed by the API schema).
By default no transaction has these values; `--buggy-share 0.5` gives them to half of the
transactions. On a fixed API this is a regression check: every answer must stay HTTP 200 and
the agent must stay silent. To test error detection, use /chaos/errors of the test service.

Usage: python agent1_monitoring/experiments/load/fraud_traffic.py
           --url http://127.0.0.1:8000/predict --rps 2
"""

import argparse
import http.client
import json
import random
import threading
import time
import urllib.request

CATEGORIES = ["electronics", "food", "shopping"]
PAYMENT_METHODS = ["creditcard", "creditcard", "paypal", "storecredit"]


def transaction(rng: random.Random, buggy_share: float) -> dict[str, object]:
    bug = (
        rng.choice(["old_payment_method", "missing_category"]) if rng.random() < buggy_share else ""
    )
    return {
        "Category": None if bug == "missing_category" else rng.choice(CATEGORIES),
        "paymentMethod": rng.choice(PAYMENT_METHODS),
        "isWeekend": rng.choice([0, 1, None]),
        "numItems": rng.randint(1, 5),
        "localTime": round(rng.uniform(2.6, 5.04), 6),
        "paymentMethodAgeDays": (
            rng.randint(2, 1830) if bug == "old_payment_method" else rng.choice([0, 1])
        ),
        "accountAgeDays": rng.randint(1, 2000),
    }


def send(url: str, body: dict[str, object]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except (OSError, http.client.HTTPException):
        # Failures are expected during injected faults (HTTP 500, API stopped or restarting and
        # closing the connection); the API metrics record them.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/predict")
    parser.add_argument("--rps", type=float, default=2.0, help="requests per second")
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 runs until Ctrl+C")
    parser.add_argument(
        "--buggy-share",
        type=float,
        default=0.0,
        help="share of transactions using the values of the two bugs fixed in the API",
    )
    args = parser.parse_args()

    rng = random.Random()
    interval = 1 / args.rps
    deadline = time.monotonic() + args.duration if args.duration else float("inf")
    print(
        f"Sending {args.rps} transactions/s to {args.url}, "
        f"{args.buggy_share:.0%} using the values of the fixed API bugs (Ctrl+C to stop)"
    )
    try:
        while time.monotonic() < deadline:
            # One thread per request so a slow API does not lower the request rate.
            threading.Thread(
                target=send, args=(args.url, transaction(rng, args.buggy_share)), daemon=True
            ).start()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("Stopped")


if __name__ == "__main__":
    main()
