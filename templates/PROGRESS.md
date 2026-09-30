# Progress log

Append-only. One block per game, 60 words max. Read only during a `STATE.md`
refresh or a "am I improving?" question — see `CLAUDE.md`.

**Rollup (archived games):** none yet.

**Tag vocabulary** — use only these, so counts can be taken with `grep -c`:
`missed-capture` `hung-piece` `defender-miscount` `missed-mate-against`
`missed-mate-for` `clock` `conversion-fail` `endgame-technique` `king-safety`
`queen-wander` `piece-shuffle` `opening-fine` `tactic-found` `clean-game`

Format:

```
DATE · vs OPPONENT (elo) · COLOUR · RESULT
Blunders: move (what it allowed)
Tags: tag, tag
Right: what they did well
```

---
