import json
import os
import sys
from pathlib import Path

from custom_trainer import train_custom_dataset
from workspace_storage import (jobs_directory, checkpoint_path as scoped_checkpoint_path,
                               upload_path, owner_id)
from custom_model_service import activate_custom_model, saved_model_path


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("target")
    parser.add_argument("workers", type=int)
    parser.add_argument("job_id")
    parser.add_argument("simulate_failure", nargs="?", default="false", choices=("true", "false"))
    parser.add_argument("--owner-id", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after-round", type=int)
    args = parser.parse_args()
    csv_path, target_column, workers, job_id = args.csv_path, args.target, args.workers, args.job_id
    if len(job_id) != 8 or any(c not in "0123456789abcdef" for c in job_id):
        parser.error("Job ID must contain exactly eight lowercase hexadecimal characters.")
    simulate_failure = args.simulate_failure == "true"
    owner = owner_id(args.owner_id)
    if owner is not None and Path(csv_path).resolve() != upload_path(job_id, owner).resolve():
        parser.error("Dataset path does not belong to this account and job.")
    checkpoint_path = scoped_checkpoint_path(job_id, owner)

    job_directory = jobs_directory(owner)
    job_directory.mkdir(
        parents=True,
        exist_ok=True
    )

    status_path = job_directory / f"{job_id}.json"

    def write_status(value):
        temporary = status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, indent=2))
        os.replace(temporary, status_path)

    previous = json.loads(status_path.read_text()) if status_path.exists() else {}
    # Tell the dashboard that training has started.
    status = {
        **previous,
        "success": True,
        "job_id": job_id,
        "owner_id": owner,
        "status": "training",
        "pid": os.getpid(),
        "target": target_column,
        "workers": workers,
        "rounds": 10,
        "csv_path": csv_path,
        "simulate_failure": simulate_failure,
        "checkpoint_path": str(checkpoint_path),
        "message": "Distributed training is running."
    }

    write_status(status)

    def update_progress(progress):
        status.update(progress)
        events = status.setdefault("events", [])
        events.append({"round": progress["current_round"], "message": progress["message"]})
        status["events"] = events[-40:]
        write_status(status)

    try:
        result = train_custom_dataset(
            csv_path=csv_path,
            target_column=target_column,
            worker_count=workers,
            rounds=10,
            simulate_failure=simulate_failure,
            checkpoint_path=checkpoint_path,
            resume=args.resume,
            stop_after_round=args.stop_after_round,
            progress_callback=update_progress,
            model_path=saved_model_path(job_id, owner),
        )

        if result.get("status") == "paused":
            status.update(result)
            status["message"] = f"Paused after Round {result['completed_round']}. Ready to resume."
            write_status(status)
            print(status["message"])
            return

        final_status = {
            **status,
            "success": True,
            "job_id": job_id,
            "status": "complete",
            "message": "Training completed successfully.",
            "rows": result["rows"],
            "features": result["features"],
            "target": result["target"],
            "workers": result["workers"],
            "rounds": result["rounds"],
            "classes": result["classes"],
            "metrics": result["metrics"],
            "model_path": result["model_path"],
            "simulate_failure": simulate_failure,
            "recovered_failures": result["recovered_failures"],
            "checkpoint_path": result["checkpoint_path"],
            "resumed_from_round": result["resumed_from_round"],
        }

        write_status(final_status)
        activate_custom_model(job_id, owner)

        print("")
        print("======================================")
        print(" TRAINING JOB COMPLETE")
        print("======================================")
        print("Job:", job_id)
        print(
            "Accuracy:",
            f'{result["metrics"]["accuracy"] * 100:.2f}%'
        )
        print(
            "Model:",
            result["model_path"]
        )
        print("")

    except Exception as error:
        failed_status = {
            **status,
            "success": False,
            "job_id": job_id,
            "status": "failed",
            "message": "Training failed.",
            "error": str(error),
        }

        write_status(failed_status)

        print("")
        print("TRAINING FAILED")
        print("Job:", job_id)
        print("Error:", error)
        print("")

        sys.exit(1)


if __name__ == "__main__":
    main()
