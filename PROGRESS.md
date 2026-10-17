# whittle: progress notes

> **Last updated:** 2026-10-17
> **State:** changes below are committed in `48aa66a` (WIP). Nothing pushed.

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

### Committed in `48aa66a` (WIP)

```
 M .whittle/whittle.py      bits=8 -> bits=12
 M DESIGN.md                §0: --bits 12, note on why
 A .whittle/simulate.py     new
 A PROGRESS.md              this file
```

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

## gittins CLI

`gittins` v2.0.0 is on PATH at `~/.local/bin/gittins`. The simulation now runs its CLI checks instead of skipping them. Earlier runs used a source build of `gittins-cli` v1.0.9:

```sh
cargo install --git https://github.com/maxpagels/gittins --rev 5a7f2413d0ae166d95b3a759bc4990eb0c0f2689 gittins-cli
```

## Next steps (recommended order)

1. **Squash/reword the WIP commit** if you want a clean history.
2. **Collision guard.** `bits=12` fixed this arm set partly by luck; adding arms or context values can collide again silently. Add a check that hashes every arm's features for every context value and fails if two arms share a slot. Put it in `simulate.py`, and/or warn at `SessionStart`.
3. **Simulate realistic usage (§16.5).** Use 10–30 sessions a day, sparse ratings, and 5–10 point gaps between arms. Questions to answer:
   - How many weeks until it learns?
   - Should `hour` be grouped or dropped? At 24 separate values it splits the data; in a standalone test it cut the best arm's late share from 89% to 68%.
   - What does training unrated sessions as 0.5 cost? It shrinks the gaps between arms.
   - Which ε? Use `gittins sweep` on the simulated logs.
4. **Version the arms (§10.3).** Name each arm by a hash of its text, so editing an arm doesn't mix new data into the old arm's history.
5. **Rewards beyond `+`/`-` (§7).** Only once real use shows that ratings are too sparse.

Defer plugin packaging and more sites until one site has visibly learned something (DESIGN §0).
