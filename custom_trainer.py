import hashlib
import os
import tempfile
import multiprocessing as mp
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from sklearn.linear_model import SGDClassifier
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler


MODEL_PATH = Path("models/custom_distributed_model.joblib")


def train_worker(
    worker_id,
    X_part,
    y_part,
    classes,
    coef,
    intercept,
    result_queue,
    simulate_failure=False,
):
    """
    Train one worker on its own portion of the dataset.
    """

    try:
        if simulate_failure:
            print(
                f"Worker {worker_id}: SIMULATED CRASH"
            )
            raise RuntimeError(
                f"Simulated failure in Worker {worker_id}"
            )

        model = SGDClassifier(
            loss="log_loss",
            learning_rate="constant",
            eta0=0.01,
            random_state=42 + worker_id,
        )

        # Initialize the model.
        model.partial_fit(
            X_part[:1],
            y_part[:1],
            classes=classes,
        )

        # Start from the current global model.
        model.coef_ = coef.copy()
        model.intercept_ = intercept.copy()

        # Train on this worker's data.
        model.partial_fit(
            X_part,
            y_part,
        )

        result_queue.put({
            "success": True,
            "worker_id": worker_id,
            "samples": len(X_part),
            "coef": model.coef_,
            "intercept": model.intercept_,
        })

    except Exception as error:
        result_queue.put({
            "success": False,
            "worker_id": worker_id,
            "error": str(error),
        })


def train_custom_dataset(
    csv_path,
    target_column,
    worker_count=4,
    rounds=10,
    simulate_failure=False,
    checkpoint_path=None,
    resume=False,
    stop_after_round=None,
    progress_callback=None,
    model_path=None,
):
    """
    Train a distributed binary classifier from a CSV file.
    """

    model_path = Path(model_path or MODEL_PATH)
    worker_count = int(worker_count)

    if worker_count not in [1, 2, 4]:
        raise ValueError(
            "Worker count must be 1, 2, or 4."
        )

    rounds = int(rounds)
    if rounds < 1:
        raise ValueError("Training rounds must be positive.")
    if stop_after_round is not None and not 1 <= int(stop_after_round) < rounds:
        raise ValueError("The pause round must be between 1 and rounds - 1.")
    checkpoint_path = Path(checkpoint_path or "models/checkpoints/custom_latest.joblib")
    configuration = {
        "dataset_sha256": hashlib.sha256(Path(csv_path).read_bytes()).hexdigest(),
        "target": str(target_column),
        "workers": worker_count,
        "rounds": rounds,
        "simulate_failure": bool(simulate_failure),
    }

    dataset = pd.read_csv(csv_path)

    if dataset.empty:
        raise ValueError(
            "Dataset is empty."
        )

    if target_column not in dataset.columns:
        raise ValueError(
            f"Target column '{target_column}' was not found."
        )


    # -----------------------------
    # Separate features and target
    # -----------------------------

    X = dataset.drop(
        columns=[target_column]
    )

    y = dataset[target_column]


    if X.shape[1] == 0:
        raise ValueError(
            "Dataset must contain at least one feature column."
        )


    # Current V1 supports numeric features.
    non_numeric = X.select_dtypes(
        exclude="number"
    ).columns.tolist()

    if non_numeric:
        raise ValueError(
            "V1 currently supports numeric feature columns only. "
            "Non-numeric columns: "
            + ", ".join(map(str, non_numeric))
        )


    # -----------------------------
    # Missing values
    # -----------------------------

    if X.isnull().any().any():
        raise ValueError(
            "Feature columns contain missing values."
        )

    if y.isnull().any():
        raise ValueError(
            "Target column contains missing values."
        )


    # -----------------------------
    # Encode target
    # -----------------------------

    label_encoder = LabelEncoder()

    y_encoded = label_encoder.fit_transform(
        y
    )


    if len(label_encoder.classes_) != 2:
        raise ValueError(
            "V1 currently supports binary classification only. "
            f"Found {len(label_encoder.classes_)} target classes."
        )


    # -----------------------------
    # Need enough data
    # -----------------------------

    if len(dataset) < 10:
        raise ValueError(
            "Dataset must contain at least 10 rows "
            "for training and evaluation."
        )


    class_counts = np.bincount(
        y_encoded
    )

    if np.min(class_counts) < 2:
        raise ValueError(
            "Each target class needs at least 2 samples."
        )


    # -----------------------------
    # Train / test split
    # -----------------------------

    X_train, X_test, y_train, y_test = (
        train_test_split(
            X.to_numpy(dtype=float),
            y_encoded,
            test_size=0.20,
            random_state=42,
            stratify=y_encoded,
        )
    )


    if len(X_train) < worker_count:
        raise ValueError(
            "Training dataset is too small "
            "for the selected worker count."
        )


    # -----------------------------
    # Scale features
    # -----------------------------

    scaler = StandardScaler()

    X_train = scaler.fit_transform(
        X_train
    )

    X_test = scaler.transform(
        X_test
    )


    classes = np.array(
        [0, 1]
    )


    # -----------------------------
    # Create initial global model
    # -----------------------------

    global_model = SGDClassifier(
        loss="log_loss",
        learning_rate="constant",
        eta0=0.01,
        random_state=42,
    )

    global_model.partial_fit(
        X_train[:1],
        y_train[:1],
        classes=classes,
    )


    # -----------------------------
    # Distributed training rounds
    # -----------------------------

    recovered_failures = 0
    completed_round = 0
    if resume:
        checkpoint = joblib.load(checkpoint_path)
        if checkpoint.get("configuration") != configuration:
            raise ValueError("Checkpoint does not match this dataset or training settings.")
        completed_round = checkpoint["completed_round"]
        if not isinstance(completed_round, int) or not 1 <= completed_round <= rounds:
            raise ValueError("Checkpoint contains an invalid completed round.")
        global_model = checkpoint["model"]
        scaler = checkpoint["scaler"]
        label_encoder = checkpoint["label_encoder"]
        recovered_failures = checkpoint["recovered_failures"]
        print(f"Resuming after Round {completed_round}; next round is {completed_round + 1}.")

    worker_states = ["waiting"] * worker_count
    def report(round_number, completed, message):
        if progress_callback is not None:
            progress_callback({
                "current_round": round_number,
                "completed_round": completed,
                "recovered_failures": recovered_failures,
                "worker_states": [
                    {"worker_id": index + 1, "state": state}
                    for index, state in enumerate(worker_states)
                ],
                "message": message,
            })

    report(completed_round, completed_round, "Resuming saved training." if resume else "Preparing workers.")

    for round_number in range(
        completed_round + 1,
        rounds + 1
    ):

        print("")
        print(
            f"Round {round_number}/{rounds}"
        )

        worker_states[:] = ["training"] * worker_count
        report(round_number, round_number - 1, f"Training Round {round_number}/{rounds}.")

        feature_parts = np.array_split(
            X_train,
            worker_count
        )

        target_parts = np.array_split(
            y_train,
            worker_count
        )

        result_queue = mp.Queue()

        processes = []


        for worker_index in range(
            worker_count
        ):

            process = mp.Process(
                target=train_worker,
                args=(
                    worker_index + 1,
                    feature_parts[worker_index],
                    target_parts[worker_index],
                    classes,
                    global_model.coef_,
                    global_model.intercept_,
                    result_queue,
                    (
                        simulate_failure
                        and round_number == 4
                        and worker_index == 2
                    ),
                ),
            )

            processes.append(
                process
            )

            process.start()


        results = []
        recovered_workers = []


        for worker_index, process in enumerate(processes):

            process.join(timeout=30)

            if process.is_alive():

                process.terminate()
                process.join()

                print(
                    f"Worker {worker_index + 1} timed out."
                )

                recovered_workers.append(
                    worker_index
                )

            elif process.exitcode != 0:

                print(
                    f"Worker {worker_index + 1} crashed."
                )

                recovered_workers.append(
                    worker_index
                )


        # Collect results produced by healthy workers.
        while not result_queue.empty():

            result = result_queue.get()

            if result["success"]:

                results.append(
                    result
                )

            else:

                failed_index = (
                    result["worker_id"] - 1
                )

                if failed_index not in recovered_workers:

                    recovered_workers.append(
                        failed_index
                    )


        # Find workers that exited without returning a result.
        completed_ids = {
            result["worker_id"]
            for result in results
        }


        for worker_index in range(worker_count):

            worker_id = worker_index + 1

            if (
                worker_id not in completed_ids
                and worker_index not in recovered_workers
            ):

                recovered_workers.append(
                    worker_index
                )


        for result in results:
            worker_states[result["worker_id"] - 1] = "complete"
        for worker_index in recovered_workers:
            worker_states[worker_index] = "failed"
        report(round_number, round_number - 1,
               "Worker failure detected." if recovered_workers else "Aggregating worker results.")

        # Restart any failed workers.
        for worker_index in recovered_workers:

            worker_id = worker_index + 1

            print(
                f"Recovering Worker {worker_id}..."
            )

            worker_states[worker_index] = "recovering"
            report(round_number, round_number - 1, f"Restarting Worker {worker_id}.")

            retry_queue = mp.Queue()

            retry_process = mp.Process(
                target=train_worker,
                args=(
                    worker_id,
                    feature_parts[worker_index],
                    target_parts[worker_index],
                    classes,
                    global_model.coef_,
                    global_model.intercept_,
                    retry_queue,
                    False,
                ),
            )

            retry_process.start()

            retry_process.join(
                timeout=30
            )


            if retry_process.is_alive():

                retry_process.terminate()
                retry_process.join()

                raise RuntimeError(
                    f"Worker {worker_id} failed recovery."
                )


            if retry_process.exitcode != 0:

                raise RuntimeError(
                    f"Worker {worker_id} crashed during recovery."
                )


            if retry_queue.empty():

                raise RuntimeError(
                    f"Worker {worker_id} returned no recovery result."
                )


            retry_result = (
                retry_queue.get()
            )


            if not retry_result["success"]:

                raise RuntimeError(
                    f"Worker {worker_id} recovery failed: "
                    f'{retry_result["error"]}'
                )


            results.append(
                retry_result
            )
            recovered_failures += 1
            worker_states[worker_index] = "recovered"
            report(round_number, round_number - 1, f"Worker {worker_id} recovered successfully.")


            print(
                f"Worker {worker_id} recovered successfully."
            )


        if len(results) != worker_count:

            raise RuntimeError(
                "Not all workers completed the training round."
            )


        # -------------------------
        # Weighted aggregation
        # -------------------------

        total_samples = sum(
            result["samples"]
            for result in results
        )


        new_coef = np.zeros_like(
            global_model.coef_
        )

        new_intercept = np.zeros_like(
            global_model.intercept_
        )


        for result in results:

            weight = (
                result["samples"]
                / total_samples
            )

            new_coef += (
                result["coef"]
                * weight
            )

            new_intercept += (
                result["intercept"]
                * weight
            )


        global_model.coef_ = new_coef

        global_model.intercept_ = (
            new_intercept
        )


        print(
            f"{worker_count} workers aggregated."
        )
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = {
            "configuration": configuration,
            "completed_round": round_number,
            "model": global_model,
            "scaler": scaler,
            "label_encoder": label_encoder,
            "recovered_failures": recovered_failures,
        }
        # Replace only after the full checkpoint is written.
        descriptor, temporary = tempfile.mkstemp(dir=checkpoint_path.parent, suffix=".joblib")
        os.close(descriptor)
        try:
            joblib.dump(checkpoint, temporary)
            os.replace(temporary, checkpoint_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(f"Checkpoint saved after Round {round_number}.")
        report(round_number, round_number, f"Round {round_number}/{rounds} complete; checkpoint saved.")
        if stop_after_round is not None and round_number == int(stop_after_round):
            return {
                "success": True,
                "status": "paused",
                "completed_round": round_number,
                "checkpoint_path": str(checkpoint_path),
                "recovered_failures": recovered_failures,
            }



    # -----------------------------
    # Evaluate global model
    # -----------------------------

    predictions = global_model.predict(
        X_test
    )


    accuracy = accuracy_score(
        y_test,
        predictions
    )

    precision = precision_score(
        y_test,
        predictions,
        zero_division=0,
    )

    recall = recall_score(
        y_test,
        predictions,
        zero_division=0,
    )

    f1 = f1_score(
        y_test,
        predictions,
        zero_division=0,
    )


    metrics = {
        "accuracy": float(accuracy),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
    }


    # -----------------------------
    # Save model package
    # -----------------------------

    model_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )


    package = {
        "model": global_model,
        "scaler": scaler,
        "label_encoder": label_encoder,
        "feature_names": [
            str(column)
            for column in X.columns
        ],
        "target_column": str(
            target_column
        ),
        "workers": worker_count,
        "rounds": rounds,
        "recovered_failures": recovered_failures,
        "metrics": metrics,
    }


    descriptor, temporary = tempfile.mkstemp(dir=model_path.parent, suffix=".joblib")
    os.close(descriptor)
    try:
        joblib.dump(package, temporary)
        os.replace(temporary, model_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


    return {
        "success": True,
        "rows": int(
            dataset.shape[0]
        ),
        "features": int(
            X.shape[1]
        ),
        "target": str(
            target_column
        ),
        "workers": worker_count,
        "rounds": rounds,
        "recovered_failures": recovered_failures,
        "classes": [
            str(value)
            for value
            in label_encoder.classes_
        ],
        "metrics": metrics,
        "checkpoint_path": str(checkpoint_path),
        "resumed_from_round": completed_round,
        "model_path": str(
            model_path
        ),
    }


if __name__ == "__main__":

    print("")
    print("======================================")
    print(" SYNAPTIFORGE CUSTOM TRAINING TEST")
    print("======================================")

    result = train_custom_dataset(
        csv_path="data/test_upload.csv",
        target_column="passed",
        worker_count=4,
        rounds=10,
    )

    print("")
    print("Training complete.")
    print(result)
