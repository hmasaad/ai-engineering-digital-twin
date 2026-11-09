# AI Engineering Digital Twin

Twin is a local model of a software project. It reads a repository on this machine, scores near-term engineering risk, and lets you try a change before anything is edited. Scores are decision-support indexes, not a calibrated chance of failure.

## Setup

Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
make test
make serve
```

Open http://127.0.0.1:8000. The first launch scans `examples/northwind`. Stop the server with Ctrl+C.

`make test` runs the suite. `make serve` starts the API and the console on `127.0.0.1:8000`.

## Use

The header **Scan** field takes a folder on this machine, not a GitHub URL. An absolute path is used as given. A relative path is resolved from this repository, then from the current working directory. **Rescan** reads the active folder again.

| View | What to do |
| --- | --- |
| Overview | Read health, the sharpest risks, and the first council steps. |
| System map | Select a module and see what it imports and what imports it. |
| Time | Step through the system as it was 20, 40, 60 days ago. Relations hold over an interval. |
| Sync | **Check repository** compares the virtual twin with the folder and does not change it. **Apply** folds the difference in as events. Incidents you record stay on the twin. |
| Risk radar | Filter bug, incident, delay, and performance indexes. |
| Forecast | Read the predicted failures, the p50–p90 count band, and the confidence interval on each call. Thin history and few recorded outcomes widen the band. |
| Change lab | Tick the files a proposal would touch. The lab walks the blast radius and names reviewers. |
| Scenarios | Run one hypothetical: tests, an extraction, a second owner, a circuit breaker, or a freeze. The repository is not edited. |
| What if | Compare the decision that happened with each alternative, scored on its own. The lowest combined movement is marked preferred. |
| Decide | Keep bug, incident, delay, and cycles separate. See which measurement would change the choice, and the plan if the failure lands or holds. |
| Council | Read findings from security, reliability, performance, quality, architecture, and delivery, in one sequence. |
| Learning | Mark whether a predicted bug happened. Record an incident and the next score includes it. |
| Loop | The closed loop. The policy ranks an action, you record what you did and whether it failed, and both the action value and the calibration update. |
| Run | Start from the council plan. Agents that share a file wait. A safe measurement is recorded by the twin. Security, high severity, and wide changes wait for approval. A failed step comes back as a measurement. The repository is not edited. |
| Fidelity | See whether imports bound, including unbound and dynamic ones. Deploys and incidents in history are production checks. A build or a test is kept and does not move the bug index. A production check updates the incident index only when its band does not cross 0.50. An action counts only when it sits between a failure and a later pass. A simulation is not evidence that a deploy will succeed. |

Beliefs, outcomes, and incidents are stored in `backend/data/twin.sqlite`. Delete that file to start over; the next launch scans Northwind again.

## Your own repository

Scan the project root. Twin reads source, imports, manifests, and Dockerfiles. Optional history goes in `<repo>/.twin/history.jsonl`, one JSON object per line. `age_days` counts back from the scan.

```json
{"age_days": 6, "type": "commit", "author": "Mina Chen", "summary": "Leave retries uncapped", "files": {"services/payments/retry.py": [160, 42]}}
{"age_days": 12, "type": "incident", "service": "checkout", "severity": "sev2", "summary": "Latency while reserving bundles"}
{"age_days": 11, "type": "deploy", "service": "checkout", "result": "rollback", "summary": "Rolled back"}
{"age_days": 7, "type": "review", "files": ["services/payments/retry.py"], "hours": 72, "summary": "Review sat for 72 hours"}
```

Git history is included only when the scanned directory is its own git root. A nested folder does not inherit the parent repository.
