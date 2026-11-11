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
| Time | Step through the system as it was 20, 40, 60 days ago. Imports, packages, plugins, pull requests, and test links hold over an interval. A dependency upgrade closes the previous version. A build failure is tied to the package the history names. |
| Sync | **Check repository** compares the virtual twin with the folder and does not change it. **Apply** folds the difference in as events. Incidents you record stay on the twin. |
| Risk radar | Filter bug, incident, delay, and performance indexes. |
| Dependencies | See which packages carry risk from a build failure, a recent upgrade, a native plugin, or a missing manifest entry. A trace then follows a vulnerability, a failed or changed dependency, or a structural weak point through importers, with a severity, a likelihood index, the import path, and a consequence read from that path. Ten algorithms then follow that trace in order: directed hops, a weight that multiplies the origin, a conditional index, a noisy-or belief update, a fault tree, common causes, a replay from 20 days ago, one containment cut, an uncertainty band, and a calibration check. A shared package or import cycle counts once. Direct importers keep the package index. Each further hop keeps a smaller share. This does not move the other indexes. |
| Forecast | Read the predicted failures, the p50–p90 count band, and the confidence interval on each call. Thin history and few recorded outcomes widen the band. |
| Failures | Read the engineering failure a dependency points to. A recorded build failure, upgrade, or native binding forecasts a build failure. An undeclared import on an untested file forecasts a test failure. The same page then answers seven questions from recorded history: bugs, pull requests, builds, unstable dependencies, releases, incidents, and debt. Twelve algorithms read those answers in order: features, the commit sequence, the existing bug index, the pull request, the build forecast, one pass over the import graph, the days since a recorded incident, a posterior that stays the prior when no outcome is recorded, a band, an explanation, a drift check that does not retrain, and an evaluation. A recorded build failure is evidence, not confirmation. This is not a calibrated probability. |
| Change lab | Tick the files a proposal would touch. Eight algorithms run in order: the import graph, transitive pressure, co-changes within a commit or 14 days, a reading of the file, an impact index with a band, a greedy test cover, an uncertainty band on the blast-radius index, and an explanation that cites the edge, co-change, test, or index it used. |
| Scenarios | Run one hypothetical: tests, an extraction, a second owner, a circuit breaker, or a freeze. The repository is not edited. |
| What if | Compare the decision that happened with each alternative, scored on its own. The lowest combined movement is marked preferred. |
| Counterfactual | Withhold one recorded upgrade or build failure and see the dependency index move. Withholding an upgrade also removes a build failure that came after it. The same page then simulates each engineering decision on its own: withhold that fact, cover one untested file, or take the containment cut. Ten algorithms then separate a scenario from an intervention. A changed variable is a cause only where a structural edge says so, and that edge stays an assumption until a recorded outcome supports it. Tests move only that file's likelihood index. The bug index is not resimulated. |
| Decide | Keep bug, incident, delay, and cycles separate. See which measurement would change the choice, and the plan if the failure lands or holds. |
| Engine | One engineering action from the failure forecast and the counterfactual. Accuracy can block it. The repository is not edited. |
| Council | Read findings from security, reliability, performance, quality, architecture, and delivery, in one sequence. |
| Learning | Mark whether a predicted bug happened. Record an incident and the next score includes it. |
| Loop | The closed loop. The policy ranks an action, you record what you did and whether it failed, and both the action value and the calibration update. |
| Run | Start from the council plan. Agents that share a file wait. A safe measurement is recorded by the twin. Security, high severity, and wide changes wait for approval. A failed step comes back as a measurement. The repository is not edited. |
| Fidelity | See whether imports bound, including unbound and dynamic ones. Six comparisons then read that scan against the twin: architecture, dependencies, simulations, predicted failures, incidents, and CI results. A simulation is not evidence that a deploy will succeed. A failure or incident that fed an index is not scored as a hit. Deploys and incidents in history are production checks. A build or a test is kept and does not move the bug index. A production check updates the incident index only when its band does not cross 0.50. An action counts only when it sits between a failure and a later pass. The accuracy trust is read and left unchanged. |
| Accuracy | Compare predicted indexes with recorded outcomes and with production checks. A missed check or a thin history holds the decision engine. A forecast built from a recorded failure is not counted as a hit. |

Beliefs, outcomes, and incidents are stored in `backend/data/twin.sqlite`. Delete that file to start over; the next launch scans Northwind again.

## Your own repository

Scan the project root. Twin reads source, imports, manifests, and Dockerfiles. Optional history goes in `<repo>/.twin/history.jsonl`, one JSON object per line. `age_days` counts back from the scan.

```json
{"age_days": 6, "type": "commit", "author": "Mina Chen", "summary": "Leave retries uncapped", "files": {"services/payments/retry.py": [160, 42]}}
{"age_days": 12, "type": "incident", "service": "checkout", "severity": "sev2", "summary": "Latency while reserving bundles"}
{"age_days": 11, "type": "deploy", "service": "checkout", "result": "rollback", "summary": "Rolled back"}
{"age_days": 7, "type": "review", "files": ["services/payments/retry.py"], "hours": 72, "summary": "Review sat for 72 hours"}
{"age_days": 21, "type": "dependency", "manifest": "requirements.txt", "package": "psycopg", "version": "3.2.1", "prior": "3.1.0", "summary": "Upgrade psycopg"}
{"age_days": 19, "type": "pull_request", "id": "84", "status": "merged", "merged_age": 18, "files": ["services/payments/retry.py"], "tests": ["tests/test_payments.py"], "summary": "Change retry behavior after the driver upgrade"}
{"age_days": 17, "type": "build", "id": "ci-441", "result": "failure", "package": "psycopg", "tests": ["tests/test_payments.py"], "summary": "CI failed after the psycopg upgrade"}
{"age_days": 30, "type": "plugin", "package": "psycopg", "plugin": "libpq", "summary": "psycopg binds the libpq native driver"}
```

`pubspec.yaml` dependencies become package nodes. An `android` `package:` under `flutter.plugin.platforms` becomes a native plugin of the pubspec package. A stored scan that predates package records shows one sync notice until the next accepted scan.

Git history is included only when the scanned directory is its own git root. A nested folder does not inherit the parent repository.
