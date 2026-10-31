# lanes

Free-model lanes: router chains, the `lane` client and quality gate, ledger, and the `lane serve` shim.
Docs: this README and [`docs/agents-and-lanes.md`](../../../docs/agents-and-lanes.md).

The quality gate ships one checker that works on its own: `json_fields` with no params (valid JSON object).
The code, SQL, regex, calendar and label checkers load validators from a separate lane-evals checkout
(`lib/` under `LANE_EVALS`, default `~/AI_Projects/_tools/lane-evals`), which is not part of this kit; without it
those checkers stop with a clear error. Write your own `lib/` there, or keep classes on `json_fields`.
