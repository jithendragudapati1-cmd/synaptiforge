import json
import os
import tempfile
from pathlib import Path

import joblib
import numpy as np
from workspace_storage import model_root, owner_id

MODEL_PATH = Path("models/custom_distributed_model.joblib")


ACTIVE_MODEL_PATH = Path("models/active_custom_model.json")


def saved_model_path(job_id, owner=None):
    if not isinstance(job_id, str) or len(job_id) != 8 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("Invalid job ID.")
    return model_root(owner) / "jobs" / f"{job_id}.joblib"


def active_job_id(owner=None):
    active_path = model_root(owner) / "active_custom_model.json"
    if not active_path.exists():
        return None
    job_id = json.loads(active_path.read_text())["job_id"]
    if not saved_model_path(job_id, owner).is_file():
        raise FileNotFoundError("The selected model is not available in your account.")
    return job_id


def activate_custom_model(job_id, owner=None):
    active_path = model_root(owner) / "active_custom_model.json"
    path = saved_model_path(job_id, owner)
    if not path.is_file():
        raise FileNotFoundError("This job has no separately saved model.")
    # Verify the package before switching the prediction service.
    package = joblib.load(path)
    for key in ("model", "scaler", "label_encoder", "feature_names", "target_column"):
        if key not in package:
            raise ValueError("Saved model package is incomplete.")
    active_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=active_path.parent, suffix=".json")
    os.close(descriptor)
    try:
        Path(temporary).write_text(json.dumps({"job_id": job_id}))
        os.replace(temporary, active_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_custom_model():
    job_id = active_job_id()
    from flask import has_request_context
    if has_request_context() and job_id is None:
        raise FileNotFoundError('Train or select one of your saved models first.')
    path = saved_model_path(job_id) if job_id else model_root() / "custom_distributed_model.joblib"
    if not path.exists():
        raise FileNotFoundError("Train a CSV dataset before running predictions.")
    stat = path.stat()
    package = joblib.load(path)
    package["job_id"] = job_id
    version = f"{job_id or 'legacy'}-{stat.st_mtime_ns}-{stat.st_size}"
    return package, version


def get_custom_model_info():
    package, version = load_custom_model()
    return {
        "success": True,
        "features": package["feature_names"],
        "feature_count": len(package["feature_names"]),
        "target": package["target_column"],
        "classes": [str(value) for value in package["label_encoder"].classes_],
        "model_version": version,
        "job_id": package.get("job_id"),
    }


def predict_custom(features, model_version=None):
    package, version = load_custom_model()
    if model_version is not None and model_version != version:
        raise RuntimeError("The trained model changed. Reload the input fields and try again.")
    feature_names = package["feature_names"]
    if not isinstance(features, list) or len(features) != len(feature_names):
        raise ValueError(f"Enter exactly {len(feature_names)} numeric feature values.")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in features):
        raise ValueError("All feature values must be numbers.")
    values = np.asarray(features, dtype=float).reshape(1, -1)
    if not np.isfinite(values).all():
        raise ValueError("All feature values must be finite numbers.")
    scaled = package["scaler"].transform(values)
    model = package["model"]
    prediction = int(model.predict(scaled)[0])
    probabilities = model.predict_proba(scaled)[0]
    class_index = list(model.classes_).index(prediction)
    label = str(package["label_encoder"].inverse_transform([prediction])[0])
    return {
        "success": True,
        "prediction": prediction,
        "label": label,
        "target": package["target_column"],
        "confidence": float(probabilities[class_index]),
        "probabilities": [float(value) for value in probabilities],
        "model_version": version,
        "job_id": package.get("job_id"),
    }


def predict_custom_batch(dataset, model_version=None):
    import pandas as pd
    package, version = load_custom_model()
    if model_version is not None and model_version != version:
        raise RuntimeError("The selected model changed. Refresh the page and try again.")
    names = package["feature_names"]
    if dataset.empty:
        raise ValueError("The uploaded CSV contains no data rows.")
    if len(dataset) > 10000:
        raise ValueError("Batch prediction supports up to 10,000 rows per upload.")
    missing = [name for name in names if name not in dataset.columns]
    if missing:
        raise ValueError("Missing feature columns: " + ", ".join(missing))
    try:
        values = dataset.loc[:, names].apply(pd.to_numeric, errors="raise").to_numpy(dtype=float)
    except (ValueError, TypeError):
        raise ValueError("All model feature columns must contain numeric values.") from None
    if not np.isfinite(values).all():
        raise ValueError("Model feature columns contain missing or non-finite values.")
    model = package["model"]
    scaled = package["scaler"].transform(values)
    predictions = model.predict(scaled)
    labels = package["label_encoder"].inverse_transform(predictions.astype(int))
    probabilities = model.predict_proba(scaled)
    class_indexes = {int(value): index for index, value in enumerate(model.classes_)}
    confidence = np.array([probabilities[row, class_indexes[int(value)]]
                           for row, value in enumerate(predictions)])
    output = dataset.copy()
    def unique_name(name):
        candidate = name
        suffix = 2
        while candidate in output.columns:
            candidate = f"{name}_{suffix}"
            suffix += 1
        return candidate
    output[unique_name("predicted_" + package["target_column"])] = labels
    output[unique_name("prediction_confidence_percent")] = np.round(confidence * 100, 2)
    output[unique_name("prediction_model_job")] = package.get("job_id") or "legacy"
    return output, package.get("job_id")
