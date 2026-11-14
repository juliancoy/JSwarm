# Review and validation — 2026-11-13 (America/New_York)

- `git diff --check`: PASS
- Host pytest release/publication/Git-source tests: 16 passed. CPU tests: 10 passed, 3 subtests passed. Node resource-history/resource-fetch tests passed. Exact commands, environment fingerprints and output paths remain in adjacent `*.test-log.jsonl` logs.

- Changed-file SHA-256 fingerprints:

```json
{
  "scripts/release-secrets-allowlist.json": "f2525ec071ac6e2d149450b15b0770edd2933a3f75e18e2932df5dde462ef2d8",
  "scripts/release_secrets.py": "c799450880cda873c24e12dafaac72c4b74761049af3a6503d0bdb9d3094989d",
  "swarm-control/server/tests/resource-fetch.test.js": "6aab078f697e3312c731a27a492d8f3d5f93fc99e8a78b3930cef857d825304a",
  "swarm-control/server/view/resource-fetch.js": "62f9d6935f21a60022fd53cf1cc8fccfbf6af741c416a2966d2a5f6f3a0413ec",
  "tests/test_deploy_publish.py": "643b5441657c2c86e1a89cd7cccf6a0fb3f4d30eb898e6b8fa6511c349ff602b",
  "tests/test_release_secrets.py": "6f6da1583a9e4f8536f433135754f00a584c5b5db1f98d534bac7de46e515921",
  "README.md": "98ecc8e1b78d07d27fb65935df8f6a34f2d424d34f7cec966104f9cff02fc67d",
  "docs/filesystem-qualification-20261113.md": "ec1e10c00f493e7653d659ab83b09e4a2b6f0be03ec74bb6a6530cd9116c691c",
  "scripts/deploy_swarm_inputs.py": "46e1c4f5763e9a5a4bff8755f1246931e215dc736c0762dba1f925601d5860af",
  "swarm-control/server/cpu.py": "782d07d354c02e86eb46eafb2e5d76ae7adf4f7bc06f6478184b7b6ca803b6af",
  "swarm-control/server/tests/resource-history.test.js": "099d1e1f2a095d76cb70e35b83ba89274d1b2989a15cef81f214d06aa4164267",
  "swarm-control/server/tests/test_cpu.py": "cba797bf3112f19625281ffa1f7db4b378ff034a97a5e0ec08a57bf0edb7a6a3",
  "swarm-control/server/view/levels-model.js": "06395e465dcbdc18c9de21f86f4eaa620b6f0edd90f6392d7c70b91fe0b46bff",
  "swarm-control/server/view/levels.js": "291cd7ba53e14023ba9ec077747e8323ee2d1c6c155d897ee1e6e6bd95b0c044",
  "swarm-control/server/view/resource-history.js": "dc7f0860cf06275eb4bff92042652fc30824d721de9c455e1b842b03aab616b8",
  "tests/deployment-test-runs.jsonl": "772ed5ce3934aba06adba79e63990dac8c029d7154de6f4978fa278f0b906c4d",
  "tests/filesystem-test-runs.jsonl": "e654ae99cd73830657dd012c1923d4bb87e33016d14f4c5377d88431cc0bc29d",
  "tests/mdns-test-runs.jsonl": "8830c460a23f29cfc12ba932db124e4661b2d854728d204b4dd4f1715ac4a6e9"
}
```
