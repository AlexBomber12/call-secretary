# Call Secretary contributor instructions

This repository uses a PR-based, test-first workflow. It does not require a Pipeline Orchestrator, coding daemon, task queue, or external merge daemon.

## PLANNED PR

1. Treat the complete, self-contained Codex prompt as the task. Do not search for `tasks/QUEUE.md` or `tasks/PR-*.md`.
2. Verify the repository, requested base and target branches, and a clean working tree. Create one task-specific branch from `origin/main`.
3. Implement only the approved coherent outcome. Keep production changes minimal and independently testable.
4. Write meaningful tests, then run the complete local gate with `scripts/ci.sh`. Do not reduce coverage or disable a failing test.
5. Commit and push the task branch. Open a ready GitHub pull request with real validation evidence, and verify that the remote PR HEAD matches the local HEAD.
6. Complete the approved task through the `FIX FEEDBACK` loop, then merge the validated PR and continue the next approved outcome. Do not ask for separate merge confirmation. Stop before merging only when the user explicitly requests that boundary.

## FIX FEEDBACK

1. Work on the existing PR branch. Obtain the latest relevant GitHub Actions results and Codex Review comments for its current HEAD; stale results and reviews never qualify.
2. Ignore feedback that applies only to obsolete commits and has already been addressed. Fix every applicable P1/P2 finding unless the human operator explicitly waives it.
3. Make only necessary, coherent regression fixes and avoid unrelated refactors. Add or update tests that demonstrate each correction.
4. Run `scripts/ci.sh` again, commit, and push to the same PR branch.
5. Verify the new remote HEAD and its GitHub Actions results. If a Codex Review is not already running for that HEAD, request one; never send a duplicate request for a running current-HEAD review.
6. Repeat without an artificial iteration limit until current-HEAD CI passes and the latest review has no unresolved actionable P1/P2 findings, or report a genuine blocker with a defensible rationale. Every pushed correction requires fresh current-HEAD validation. Do not weaken CI or tests. Merge once the eligibility conditions below are met.

The normal authorized sequence is: implement, run local CI, commit, push, open the PR, wait for GitHub Actions, request and evaluate Codex Review, fix actionable feedback with regression tests, rerun local CI, commit and push, request a current-HEAD review when needed, and repeat until eligible. Merge the exact validated HEAD, verify the resulting main branch, then continue the next approved outcome or report a genuine blocker.

## Merge eligibility

A PR may be merged only when all of the following are true:

- Every applicable GitHub Actions check succeeded on the current PR HEAD.
- The latest Codex Review for the current HEAD completed without unresolved actionable P1/P2 findings. An old review from before the latest push never qualifies.
- When a Codex bot `+1` reaction is available, it is non-stale positive review evidence. A generic `Completed` summary alone is not approval.
- The PR stays within the approved task scope and the user has not explicitly requested a stop before merge. Routine merging is already authorized; no additional approval is required.

## Security, testing, and repository rules

- Never commit secrets, access tokens, private keys, real participant data, or such values in fixtures, logs, or PR descriptions. Ordinary tests must not use production API credentials.
- Never commit directly to `main`, force-push `main`, bypass branch protection, weaken CI to obtain green status, or fabricate tests or reviewer approval.
- Do not modify deployment services without an explicitly authorized task. Report genuine blockers instead of looping indefinitely.
- All first-party production Python code must reach 100% line and branch coverage. Coverage does not replace security review, integration testing, behavioral evaluation, or production validation.

## Project context

Read `docs/spec.md`, `docs/plan.md` and `docs/progress.md` before substantial changes. Python 3.12; Ubuntu x86_64 target; SQLite; configurable Realtime voice; private Telegram reports. The existing SMS gateway and its modem are separate. This workflow is adapted from [Almost Reasonable](https://github.com/AlexBomber12/almost-reasonable/blob/main/AGENTS.md) (source blob `7f7ce3aa9f69bf992166ee1db2ed965d4442a85c`) and [Infrastructure Inspector](https://github.com/AlexBomber12/infrastructure-inspector/blob/main/AGENTS.md).

Run `bash scripts/ci.sh` on Linux/Git Bash, or `python scripts/ci.py` on Windows. Both invoke the same complete gate. CI runs on Windows and Linux with synthetic data and fake transports. It must not use paid APIs, a real microphone, Telegram delivery, or a modem. On this Windows host, asyncio requires a local socketpair: if sandbox isolation prevents it, obtain the execution permission for the same offline tests; never skip the tests.

Measure every production Python module under `src/call_secretary`, including entry points and platform-specific code. Do not add coverage exclusions, disable branches or narrow the source list to obtain green status. Exercise platform behavior with isolated fakes as appropriate; coverage is not live hardware evidence.

## Code Review Rules

- A caller number is not the original called recipient. Automatic routing needs validated, unambiguous ingress metadata; otherwise ask for the recipient. Reports must keep their fixed recipient/destination through retries and rebinding. Flag cross-family disclosure.
- Never persist call audio, raw audio payloads, credentials or real family data in Git, fixtures, logs or PR descriptions. Bound audio buffers, cancellation and cleanup. Simulation must use isolated state and fake delivery.
- Paid work needs a persisted budget reservation. Unknown usage must remain reserved across crashes/retries. Code must enforce the ten-minute limit independently of model cooperation; billing estimates are not exact provider invoices.
- Never take over the SMS modem, disable ModemManager globally, reset unrelated USB devices or deploy to SERVER as a side effect. Production modem work follows H1/H2 hardware gates. A green test suite does not prove phone audio, forwarded-number metadata, voice quality or live Telegram delivery; keep those unverified until observed.
