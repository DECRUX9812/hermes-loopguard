# Goal: Fix the flaky token-refresh test

## Requirement
The test suite passes reliably again: tests/test_auth.py::test_refresh_expiry no longer fails
intermittently on CI. The root cause (a race between the token cache and the refresh timer) is
fixed in the code; the test is not merely skipped, weakened, or marked xfail. The behaviour of
the refresh endpoint itself is unchanged.

## Checks
- run: `python3 -m pytest -q tests/test_auth.py` exits 0
- file: CHANGELOG.md exists
- file: CHANGELOG.md contains "token refresh"
- file: test-output.log not contains "FAILED"

## Limits
- max_continuations: 3
- timeout: 600
