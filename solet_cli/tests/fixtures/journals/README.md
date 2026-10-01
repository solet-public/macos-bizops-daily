# Journal migration fixtures

`bizopsb15_v1_pre_r12.json` is the real captured manager journal from
`/Users/example/solet_test_corpus/bizopsb15-manager-transaction-journal-corrupt-install-launchagent-20260901T012541Z.json`.
Its SHA-256 is `6deadb024fa7721de08b54fe4f8648c5696af721013e5b7709a43ba76d205e53`.
It is a v1 journal with 24 exact pre-r12 (12-key) operation attempts.

The migration smoke derives its v1 16-key control from this frozen capture by
adding only the four diagnostics introduced in r12. That fixture is synthetic
and deliberately labeled as such in the smoke because no independent captured
16-key v1 journal was available in the preserved corpus.
