# chess-coach

A personal chess coach built from three small pieces:

- **Stockfish** tells the truth about every move.
- **Claude Code** turns that into coaching: plain language, three corrections per game,
  and a memory of your weaknesses that it keeps up to date.
- **`chess.html`** is an offline dashboard that shows both: stats and trends, and a
  move-by-move review of every game with the coach's comments pinned to the moves.

It's aimed at beginners. The coaching instructions ([`CLAUDE.md`](CLAUDE.md)) assume no
vocabulary, cap every review at three lessons, and teach the one habit that matters most
at low ratings: *what can I take, what can they take, any checks?*

## Why

Engine reviews tell you *what* was wrong. They don't know that it's the fourth game in a
row where you walked past a free piece, and they can't explain the rule in words you'll
remember at the board. A language model can — but left alone it also "sees" hanging pieces
that aren't there. This project makes the engine the source of truth: the coach may not
claim anything about a position that Stockfish's output doesn't back, and every comment it
saves is checked against the actual game before the dashboard shows it.

## What you get

- **Overview.** Record, rating, accuracy, blunders per game, and *"Is the coaching
  working?"* — how often each weakness showed up per block of five games, so you can see
  a habit disappear (or not).
- **Game view.** Board with the best move and the refutation drawn as arrows, a
  winning-chances graph with your mistakes marked, time spent per move, and the coach's
  comment on the moves that mattered. ← → to step, ↑ ↓ to jump between mistakes.
- **Puzzles.** Every moment from the reviews where you missed the engine's move, as a
  position to solve: click a piece, click a square, and it's graded on the spot (Stockfish
  scored every legal move at build time). Forced mates are played out move by move; the
  hint is the review's rule, and progress (solved / missed) is kept in your browser.
- **Drills.** King + queen and king + rook against a lone king, from random positions.
  Every position is solved exactly in the page (no engine, under a second), so the computer
  defends perfectly and the result says how many moves the fastest mate needed. Timed
  against the one-minute goal; stalemate and an unprotected piece end it as a draw.
- **Repertoire.** A six-move opening plan with board diagrams, linked to the moments in
  your own games where it applied.
- No server, no libraries, no network: it's one HTML file you open from Finder or
  Explorer. Light and dark mode; works on a phone screen.

## Setup

You need Python 3.11+, [Stockfish](https://stockfishchess.org/download/) and
[Claude Code](https://claude.com/claude-code).

```bash
git clone https://github.com/obuter/chess-coach.git && cd chess-coach
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml        # set `player` to your username
cp templates/* .                          # STUDENT, STATE, PROGRESS, REPERTOIRE, milestones
mkdir games
```

Fill in `STUDENT.md` with a few lines about yourself.

## Use

1. Open Claude Code in this folder and say *"analyse my latest game"*.
2. The coach runs `build.py --fetch` to pull your new games from chess.com, runs
   `analyze.py` on the newest one, gives you the review, and at the end of the session
   saves it to `reviews/` and rebuilds the dashboard.
3. Open `chess.html`.

To update the dashboard by hand: `.venv/bin/python build.py --fetch`.

- **Fetching** uses chess.com's public API — no login or key. Each month lands in
  `games/chesscom-YYYY-MM.pgn`; finished months are downloaded once, the current month is
  refreshed each run. The first run starts from your oldest game already in `games/`, or
  from the latest month if there are none; `--since 2025-01` reaches further back. Set
  `time_classes = ["rapid"]` in `config.toml` to skip blitz, bullet and daily games.
- **Other sources:** any PGN file dropped into `games/` (a lichess export, a game typed in
  by hand) is picked up too. The same game in two files is counted once.
- **Stockfish** only runs on games it hasn't seen before (results are cached in `cache/`),
  so once your games are analysed, rebuilds take well under a second. The first run on a
  big archive takes a while: about 1–2 minutes a game at the default depth 16 on a
  laptop (`--depth 12` is several times faster, if you want a quick first pass).

## How it measures

- **Severity is win probability lost, not centipawns** — going from +9 to +3 changes
  nothing, going from +2 to −1 flips the game. Centipawns are converted with
  lichess' formula.
- **Classes:** blunder ≥ 20% of your winning chances lost in one move, mistake 10–20%,
  inaccuracy 5–10%.
- **Accuracy** uses lichess' per-move accuracy formula, averaged over the game.
- Stockfish runs at depth 16 by default (`--depth` to change). Depth-limited evals vary
  slightly between runs; that's Stockfish, not a bug.

## Files

| File | What it is |
|---|---|
| `CLAUDE.md` | The coach: rules, review format, curriculum, session routine |
| `analyze.py` | Stockfish report for one PGN — what the coach reads |
| `build.py` | Builds `data.js` for the dashboard; validates saved reviews against the games |
| `chess.html` | The dashboard |
| `templates/` | Starter files for your personal notes |

Everything personal — your games, the coach's notes about you, the cache and the built
`data.js` — is gitignored, so it stays on your machine.

## License

[MIT](LICENSE)
