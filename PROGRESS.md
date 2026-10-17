# whittle: progress notes

> **Last updated:** 2026-10-17
> **State:** everything below is committed. The rewritten history is local only; `origin/main` still has the old WIP commits.

## Done this session

### Simulation harness: `.whittle/simulate.py`

End-to-end simulation of v0. It copies `whittle.py` and `arms/` into a temp dir, imports the real hook functions, and drives them with a fake user on a fake clock. The live `.whittle/` state is never touched.

```sh
uv run --script .whittle/simulate.py [--sessions 600] [--seed 1] [--keep DIR]
```

- Simulated user: P(`+`) is 0.25 for `baseline`, 0.5 for `plan-first` and 0.75 for `tests-first`. 70% of sessions are rated; the rest expire.
- Sessions are 2h apart plus a random 1–60 min until the rating.
- Checks:
  - **Hooks:** a known arm is injected; resumes re-inject the same arm; ordinary prompts pass through; a second rating is refused.
  - **Logging:** one decision per session; one resolution per rating; no decision resolved twice; unrated sessions expire as 0.5.
  - **Learning:** the best arm gets more than 60% of picks in the last sixth of sessions.
  - **gittins CLI:** `verify` passes; `replay` rebuilds a state with the same policy. Byte equality doesn't hold because replay drops decisions that are still open.
- Exits 1 on any failure. If `gittins` isn't on PATH, the CLI checks are skipped.

### Bug found and fixed: `bits=8` cannot learn

- **Symptom:** with `bits=8`, whittle settled on `plan-first` (~80% of late picks) on all 5 seeds. The best arm, `tests-first`, stayed at ~6%. Running 3000 sessions didn't help.
- **Cause:** hashed-feature collisions in 256 slots. The arms' constant features overlap: `baseline` shares slot 58 with `plan-first` and slot 27 with `tests-first`, so the model can't tell the arms apart.
- **Ruled out:** delayed ratings, expiry, and serialize/deserialize round-trips. Plain gittins learns fine with each of them.
- **Fix:** `bits=12` in `.whittle/whittle.py`. `DESIGN.md` §0 is updated to match.
- **Result:** the full simulation passes on seeds 1–5. `tests-first` reaches 67–91% of late picks.

### Live state reset

The 8-bit state can't be reused with `bits=12`, and the new state restarts decision ids at `whittle:0`, which would collide with old session files. I moved `state`, `log.jsonl` and `sessions/` to `.whittle/archive-bits8/` (gitignored). It only holds 3 sessions; delete it whenever you like.

### Simulation run, 2026-10-17 (seed 1, 600 sessions): PASS

All checks passed, including the `gittins` CLI checks (`verify`, `replay`). `tests-first` went from 80% to 87% of picks between the first and last sixth of sessions. It was already at 80% early on, because the gaps between arms are large (25/50/75%). `gittins eval`: average reward 0.635 over 598 resolved decisions.

The output, log, state and sessions are saved in `.whittle/sim-runs/2026-10-17-seed1/`. `sim-runs/` is gitignored.

### bits 12 -> 18

- `bits=18` in `.whittle/whittle.py`. `DESIGN.md` §0 is updated to match.
- `simulate.py` now has a `BITS` constant: it checks that decisions are logged with that value and passes it to `gittins replay`/`eval`. Keep it in sync with `whittle.py`.
- Simulation (seed 1) passes: `tests-first` 84% -> 91% of picks.
- **State size:** with gittins 1.x the state was dense (~8.4 MB at 18 bits). gittins 2.0.0 stores only nonzero slots, so it's ~5 KB after 600 sessions. Both scripts now require `gittins>=2`, and `simulate.py` fails if the state reaches 64 KB. gittins 2 can't read 1.x states, so the archived states are only readable with 1.x.
- The live 12-bit state (1 decision, this session) moved to `.whittle/archive-bits12/`; its files are ignored by the same `.gitignore` patterns as `archive-bits8/`.
- The run in `sim-runs/2026-10-17-seed1/` was made at `bits=12`; replay or eval it with `--bits 12`.

### Automatic rewards: retention and friction

- `whittle.py` adds a `PostToolUse` hook that records Claude's added lines per session. Session files are now JSON; the old `id arm` format is still read and never settled.
- The first `SessionStart` at least 8h after a session started settles it: retention minus friction, learned unless the user rated. Every settled session is logged to `.whittle/signals.jsonl` (gitignored). See DESIGN §0.
- Transcript markers were checked against real transcripts. An interrupt is a user text block `[Request interrupted by user]`. A denial is a `tool_result` with `is_error` and "The user doesn't want to proceed…". Denying at the prompt also writes `[… for tool use]`, which isn't counted again. On one real axiom session, all 4 flagged corrections were genuine.
- Tests: `.whittle/test_whittle.py`, 10 passing (`uv run --with pytest --with 'gittins>=2' pytest -q .whittle/test_whittle.py`). `simulate.py` still passes; its sessions have no signals, so they expire as before.
- `.claude/settings.json` runs `whittle.py` on `PostToolUse` for `Edit|Write|MultiEdit`.
- Known limits: settling only happens when a new session starts between 8h and 24h after the old one, otherwise the session expires. The horizon is baked into the state, so lengthening it means a state reset. Retention ≈ 1 for most kept sessions, so most of the difference between arms comes from friction and the occasional revert.

### `/whittle` status command

- `whittle.py status`, run by `.claude/commands/whittle.md`. See DESIGN §0. Covered by 2 more tests (12 in total). On a simulated run it takes about 0.2s.
- The command runs `.whittle/whittle.py` by a relative path, so it assumes Claude Code was started from the repo root.
- **50 scored sessions per arm is slow at ε = 0.2.** Each losing arm gets about 7% of traffic, so it takes roughly 750 sessions. In the 600-session simulation, baseline reached 33.
- **The `gittins eval` section isn't very informative yet.** It estimates the logging configuration on its own log, so IPS equals the logged mean, with weights of 1. The useful number is DESIGN §11.2's baseline comparison: an IPS estimate of "always baseline" (mean of 𝟙[arm = baseline]·r/p) against the learned policy. It's cheap to compute from the log in `summary()`.

### `/whittle` shows this session's arm (branch `whittle-show-session-arm`)

- `whittle.py status [SESSION_ID]` prints `this session: <arm> (arms/<arm>.md)` first, or `not tracked by whittle`. `.claude/commands/whittle.md` passes `${CLAUDE_SESSION_ID}` and no longer tells Claude to keep the arm hidden. Covered by 1 more test (15 in total).
- **This contradicts the design.** DESIGN §0 says the current session's arm isn't shown, and the status text and `summary()` still say open sessions' arms stay hidden. Knowing the arm before rating `+`/`-` biases the rating. Either drop the change or make it an explicit debug flag, and update DESIGN and the status text to match.
- Added a pirate arm, `arms/arr.md` ("Answer questions like a pirate."). **It breaks `simulate.py`:** the simulated user only has a P(`+`) for the three original arms, so the run fails with `KeyError: 'arr'` once `arr` is picked.

## gittins CLI

`gittins` v2.0.0 is on PATH at `~/.local/bin/gittins`. The simulation now runs its CLI checks instead of skipping them. Earlier runs used a source build of `gittins-cli` v1.0.9:

```sh
cargo install --git https://github.com/maxpagels/gittins --rev 5a7f2413d0ae166d95b3a759bc4990eb0c0f2689 gittins-cli
```

## Next steps (recommended order)

0. **Fix the simulation and decide on the arm reveal.** Copy only the arms in `P_PLUS` into the sim's scratch dir (or give unknown arms a default P(`+`)); then resolve the design conflict above.
1. **Collision guard.** Raising `bits` (now 18) fixed this arm set partly by luck; adding arms or context values can collide again silently. Add a check that hashes every arm's features for every context value and fails if two arms share a slot. Put it in `simulate.py`, and/or warn at `SessionStart`.
2. **Simulate realistic usage (§16.5).** Use 10–30 sessions a day, sparse ratings, and 5–10 point gaps between arms. Questions to answer:
   - How many weeks until it learns?
   - Should `hour` be grouped or dropped? At 24 separate values it splits the data; in a standalone test it cut the best arm's late share from 89% to 68%.
   - What does training unrated sessions as 0.5 cost? It shrinks the gaps between arms.
   - Which ε? Use `gittins sweep` on the simulated logs.
3. **Version the arms (§10.3).** Name each arm by a hash of its text, so editing an arm doesn't mix new data into the old arm's history.
4. **Rewards beyond `+`/`-` (§7).** Only once real use shows that ratings are too sparse.
5. **Session file hygiene.** `post_tool_use` reads, changes and writes the session file without a lock, so parallel edits in one session can drop line counts; lock it per session. Settled and expired session files are never deleted; settling could remove them, since `signals.jsonl` keeps the outcome.

Defer plugin packaging and more sites until one site has visibly learned something (DESIGN §0).
