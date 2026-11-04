"""Existing Streamlit inference is captured without routing through the API."""

import importlib.util
import sqlite3
import sys
import types
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock

from API.telemetry.contracts import PredictionEvent


class Session(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


def run_page(tmp_path, monkeypatch, fail=False):
    root = Path(__file__).resolve().parents[2]
    page_path = root / "Pages/home.py"
    artifact = tmp_path / "Notebooks/artifacts/fraud_pipeline.pkl"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fixture-model-bytes")
    (tmp_path / "Pages").mkdir()
    database = tmp_path / "serving.sqlite3"
    monkeypatch.setenv("AGENT6_PREDICTION_DATABASE", str(database))
    st = MagicMock()
    st.session_state = Session()
    st.columns.return_value = (MagicMock(), MagicMock())
    st.button.side_effect = lambda label: label == "Predict Fraud"
    st.selectbox.side_effect = lambda label, values, index=0: values[index]
    st.number_input.side_effect = lambda label, **kwargs: kwargs["value"]
    model = MagicMock()
    model.best_threshold = 0.5
    model.predict_proba.return_value = [0.9]
    fake_joblib = types.ModuleType("joblib")
    fake_joblib.load = lambda path: model
    fake_src = types.ModuleType("Src")
    fake_src.__path__ = []
    fake_model = types.ModuleType("Src.model")
    for name in (
        "FraudPipeline",
        "FeatureEngineering",
        "Preprocessing",
        "LogTransformer",
    ):
        setattr(fake_model, name, object)
    monkeypatch.setitem(sys.modules, "streamlit", st)
    monkeypatch.setitem(sys.modules, "joblib", fake_joblib)
    monkeypatch.setitem(sys.modules, "Src", fake_src)
    monkeypatch.setitem(sys.modules, "Src.model", fake_model)
    spec = importlib.util.spec_from_file_location(
        "streamlit_page_under_test", page_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(tmp_path / "Pages/home.py")
    if fail:
        from API.telemetry.store import PredictionLogError

        def failure(*args, **kwargs):
            raise PredictionLogError("unavailable")

        monkeypatch.setattr(module, "SQLitePredictionLog", failure)
    module.home_page()
    return st, database, artifact


def test_streamlit_prediction_uses_real_shared_logger_and_exact_pickle_identity(
    tmp_path, monkeypatch
):
    st, database, artifact = run_page(tmp_path, monkeypatch)
    with sqlite3.connect(database) as connection:
        raw = connection.execute("SELECT payload FROM predictions").fetchone()[0]
    event = PredictionEvent.model_validate_json(raw)
    assert event.predicted == 1
    assert (
        event.model_version == "artifact-" + sha256(artifact.read_bytes()).hexdigest()
    )
    assert event.source_ref == "sha256:" + sha256(artifact.read_bytes()).hexdigest()
    assert any(event.observation_id in str(call) for call in st.caption.call_args_list)
    assert event.recorded_at >= event.observed_at


def test_streamlit_does_not_present_an_unrecorded_prediction_as_success(
    tmp_path, monkeypatch
):
    st, database, _ = run_page(tmp_path, monkeypatch, fail=True)
    assert not database.exists()
    assert "Prediction recording is unavailable" in str(st.error.call_args)
    st.success.assert_not_called()
    st.caption.assert_not_called()
