# Synaptiforge Walkthrough

1. Run `python run_local.py` and open the app. Log in to a verified account.
2. In **Train**, download the generic sample CSV and upload it. Select `outcome` as the target, choose workers, give the job a name, and start training.
3. Watch the real round counter and worker states. Normal training uses the queue and saves checkpoints automatically.
4. In **My Models**, rename the completed job and use its model for predictions.
5. In **Predict**, download a prediction template. Add numeric rows using those columns, upload the file, and download results. There are no individual input boxes.
6. For a reliability demonstration, return to Train, open **Demo Mode**, enable it, choose four workers, and enable the Round 4 worker failure. Train and show the recovery event.
7. For resume, enable the pause option. Refresh after it pauses and resume from My Models.
8. To show job management, cancel a running job. Its saved checkpoint remains available. Deletion requires confirmation and is permanent.
9. In **Account**, show private storage, verified email/Google status, and password recovery.

Describe this honestly as parallel worker processes on one server, with recovery and checkpointing. It is a student demo or small beta; the sample is not a real financial or medical dataset. Accuracy measures held-out test rows; confidence is a score for an individual prediction.
