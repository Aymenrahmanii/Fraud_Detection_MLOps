"""Regression test for the paymentMethodAgeDays int-vs-double bug.

Runs against the REAL, unmocked model in a subprocess -- deliberately
isolated from the rest of the API test suite, whose conftest.py replaces
`mlflow` in sys.modules for the whole pytest process. A subprocess is the
only way to exercise the genuine model here without either contaminating
the mocked suite or being contaminated by it.

This is exactly the class of bug the mocked tests in test_main.py cannot
catch: MLflow's schema enforcement only allows an implicit int64->float64
conversion when every value in a column is 0 or 1 (treated as
boolean-like); any other value is correctly rejected. API/schemas.py
declared paymentMethodAgeDays and isWeekend as `int`, which silently
"worked" for 0/1-valued requests and 500'd on anything else -- found by
actually running the server and sending it a request with
paymentMethodAgeDays=700, not by unit tests.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _predict_in_subprocess(payload: dict) -> subprocess.CompletedProcess:
    code = (
        "from API.services import predict_fraud\n"
        f"print(predict_fraud({payload!r}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


def test_real_model_accepts_non_boolean_payment_method_age_days():
    payload = {
        "Category": "electronics",
        "paymentMethod": "storecredit",
        "isWeekend": 1,
        "numItems": 1,
        "localTime": 22.5,
        "paymentMethodAgeDays": 700.0,
        "accountAgeDays": 900,
    }

    result = _predict_in_subprocess(payload)

    assert result.returncode == 0, (
        f"real model prediction failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip().splitlines()[-1] in ("0", "1")


def test_real_model_accepts_zero_payment_method_age_days():
    # The value that always "worked" even with the buggy int type --
    # included so a future regression in the other direction also fails.
    payload = {
        "Category": "shopping",
        "paymentMethod": "paypal",
        "isWeekend": 0,
        "numItems": 4,
        "localTime": 4.742303,
        "paymentMethodAgeDays": 0.0,
        "accountAgeDays": 1,
    }

    result = _predict_in_subprocess(payload)

    assert result.returncode == 0, (
        f"real model prediction failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip().splitlines()[-1] in ("0", "1")
