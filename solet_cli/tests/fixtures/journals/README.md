# Journal migration fixtures

`bizopsb15_v1_pre_r12.json` is the real captured manager journal from
`/Users/example/solet_test_corpus/bizopsb15-manager-transaction-journal-corrupt-install-launchagent-20260901T012541Z.json`.
Its SHA-256 is `6deadb024fa7721de08b54fe4f8648c5696af721013e5b7709a43ba76d205e53`.
It is a v1 journal with 24 exact pre-r12 (12-key) operation attempts.

The migration smoke derives its v1 16-key control from this frozen capture by
adding only the four diagnostics introduced in r12. That fixture is synthetic
and deliberately labeled as such in the smoke because no independent captured
16-key v1 journal was available in the preserved corpus.

`pre_r65_update_runtime_plan_codec.py.txt` is `solet_cli/src/solet_manager/update_runtime_plan_codec.py` exactly as it stood at
`7ddbdfe0b` (r64; SHA-256 `8f417545712f4481911255382cbc577627b5e6e05b05f1ca40e6a2ba7b4a5148`, Git blob `85f80c2989e3333c26f03eac1031f0cdb4e1cb0b`).
`update_plist_adopt_smoke.py` loads it inside the `solet_manager` package and journals a real approved plan with it, so the plan has the shape every
Manager before r65 wrote (no `current_sha256` or `adopt_diff` on an artifact row), then resumes it with the current Manager. The `.txt` suffix keeps it
out of every Python gate; do not edit it.
