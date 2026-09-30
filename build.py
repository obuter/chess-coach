#!/usr/bin/env python3
"""Build data.js for chess.html: engine data for every game + Claude's reviews.

Usage:
    .venv/bin/python build.py            # analyse new games, write data.js
    .venv/bin/python build.py --list     # print game keys (to name reviews/<key>.json)
    .venv/bin/python build.py --depth 18 # re-analyse everything at a new depth

Stockfish results are cached in cache/<key>.json, so only new games cost engine
time. Reviews are read from reviews/<key>.json and checked against the PGN: a
comment pinned to a ply whose SAN does not match is reported, never shown.
"""

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from analyze import BLUNDER, DEFAULT_PLAYER, ENGINE_PATH, MISTAKE, evaluate_game, phase_of, win_pct

ROOT = Path(__file__).resolve().parent
GAMES, CACHE, REVIEWS = ROOT / "games", ROOT / "cache", ROOT / "reviews"
INACCURACY = 5.0
REVIEW_FIELDS = ("snapshot", "right", "pattern", "drill", "avoid")


def game_key(h):
    """2026-09-30_0646_alice — date, end time, opponent. Stable across re-exports."""
    date = h.get("Date", "????.??.??").replace(".", "-")
    t = re.match(r"\s*(\d{1,2}):(\d{2})", h.get("EndTime", "") or h.get("UTCTime", ""))
    hhmm = f"{int(t.group(1)):02d}{t.group(2)}" if t else "0000"
    opp = h["Black"] if h.get("White", "").lower() == PLAYER.lower() else h.get("White", "?")
    return f"{date}_{hhmm}_{re.sub(r'[^A-Za-z0-9_-]', '', opp)}"


def load_games():
    games = {}
    for path in sorted(GAMES.glob("*")):
        if path.suffix not in (".pgn", ".txt"):
            continue
        with open(path) as fh:
            while (g := chess.pgn.read_game(fh)) is not None:
                h = g.headers
                if PLAYER.lower() not in (h.get("White", "").lower(), h.get("Black", "").lower()):
                    continue
                games.setdefault(game_key(h), (g, path.name))
    return dict(sorted(games.items()))


def cached_eval(key, game, depth, engine_box):
    path = CACHE / f"{key}.json"
    if path.exists():
        data = json.loads(path.read_text())
        if data.get("depth") == depth:
            return data
    if engine_box[0] is None:
        try:
            engine_box[0] = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH)
        except FileNotFoundError:
            sys.exit(f"Stockfish not found at {ENGINE_PATH}. Install it (brew/apt install stockfish) or set `stockfish` in config.toml.")
    print(f"  analysing {key} at depth {depth} ...", flush=True)
    data = evaluate_game(game, engine_box[0], chess.engine.Limit(depth=depth))
    data["depth"] = depth
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def move_accuracy(lost):
    """Lichess' per-move accuracy from win% lost."""
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * lost) - 3.1669))


def classify(ply, lost):
    if ply["uci"] == ply["best_uci"]:
        return "best"
    if lost >= BLUNDER:
        return "blunder"
    if lost >= MISTAKE:
        return "mistake"
    if lost >= INACCURACY:
        return "inaccuracy"
    return "good"


def tag_vocabulary():
    path = ROOT / "PROGRESS.md"
    text = (path if path.exists() else ROOT / "templates" / "PROGRESS.md").read_text()
    block = text.split("**Tag vocabulary**", 1)[-1].split("Format:", 1)[0]
    return set(re.findall(r"`([a-z-]+)`", block))


def check_review(key, review, plies, vocab, warnings):
    """Keep only comments whose ply and SAN match the game; report the rest."""
    def ok(ply_no, san, where):
        if not isinstance(ply_no, int) or not 1 <= ply_no <= len(plies):
            warnings.append(f"{key}: {where} ply {ply_no} is not in the game ({len(plies)} plies)")
            return False
        actual = plies[ply_no - 1]["san"]
        if san is not None and san != actual:
            warnings.append(f"{key}: {where} ply {ply_no} says {san!r} but the move played was {actual!r}")
            return False
        return True

    moments = [m for i, m in enumerate(review.get("moments", []))
               if ok(m.get("ply"), m.get("san"), f"moment {i + 1}")]
    notes = {}
    for k, v in review.get("notes", {}).items():
        ply_no = int(k) if str(k).isdigit() else k
        text, san = (v.get("text"), v.get("san")) if isinstance(v, dict) else (v, None)
        if ok(ply_no, san, "note"):
            notes[str(ply_no)] = text
    bad_tags = [t for t in review.get("tags", []) if t not in vocab]
    if bad_tags:
        warnings.append(f"{key}: tags not in PROGRESS.md vocabulary: {', '.join(bad_tags)}")
    out = {f: review.get(f, "") for f in REVIEW_FIELDS}
    out.update(moments=moments, notes=notes, tags=[t for t in review.get("tags", []) if t in vocab])
    return out


def repertoire_boards(text):
    """Diagram for each **a · b · c** setup line in REPERTOIRE.md: your six moves, their pieces at home.

    The side comes from the nearest heading above ("As White" / "As Black"); the
    other side passes (null moves), so the moves only need to be legal on their own.
    """
    boards, colour = {}, chess.WHITE
    for line in text.splitlines():
        if line.startswith("#"):
            colour = chess.BLACK if "Black" in line else chess.WHITE
        m = re.fullmatch(r"\*\*([^*]+·[^*]+)\*\*", line.strip())
        if not m:
            continue
        board, squares = chess.Board(), []
        try:
            for san in (s.strip() for s in m.group(1).split("·")):
                if board.turn != colour:
                    board.push(chess.Move.null())
                move = board.push_san(san)
                squares.append(chess.square_name(move.to_square))
        except ValueError as e:
            sys.exit(f"REPERTOIRE.md: can't play {m.group(1)!r}: {e}")
        boards[m.group(1)] = {"fen": board.fen(), "colour": "white" if colour else "black", "squares": squares}
    return boards


def summarise(key, game, source, ev, review):
    h = game.headers
    pov = chess.WHITE if h["White"].lower() == PLAYER.lower() else chess.BLACK
    colour = "white" if pov == chess.WHITE else "black"
    opp = h["Black"] if pov == chess.WHITE else h["White"]

    plies, prev = [], ev["start_cp"]
    acc = {"me": [], "opp": []}
    counts = {c: 0 for c in ("best", "good", "inaccuracy", "mistake", "blunder")}
    phases = {}
    for p in ev["plies"]:
        mover_white = p["ply"] % 2 == 1
        sign = 1 if mover_white else -1
        lost = max(0.0, win_pct(sign * prev) - win_pct(sign * p["cp"]))
        cls = classify(p, lost)
        mine = mover_white == (pov == chess.WHITE)
        acc["me" if mine else "opp"].append(move_accuracy(lost))
        if mine:
            counts[cls] += 1
            if cls in ("mistake", "blunder"):
                ph = phase_of(p["move_no"])
                phases[ph] = phases.get(ph, 0) + 1
        plies.append({**p, "lost": round(lost, 1), "cls": cls,
                      "win": round(win_pct(p["cp"] if pov == chess.WHITE else -p["cp"]), 1)})
        prev = p["cp"]

    res = h.get("Result", "*")
    outcome = "draw" if res == "1/2-1/2" else ("win" if res == ("1-0" if pov == chess.WHITE else "0-1") else "loss")
    elo = lambda s: int(h[s]) if h.get(s, "").isdigit() else None  # noqa: E731
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    return {
        "key": key, "source": source,
        "date": h.get("Date", "").replace(".", "-"), "end": key.split("_")[1],
        "colour": colour, "opponent": opp,
        "my_elo": elo("WhiteElo" if pov == chess.WHITE else "BlackElo"),
        "opp_elo": elo("BlackElo" if pov == chess.WHITE else "WhiteElo"),
        "result": res, "outcome": outcome, "termination": h.get("Termination", ""),
        "time_control": h.get("TimeControl", ""),
        "opening": h.get("ECOUrl", "").rsplit("/", 1)[-1].replace("-", " "),
        "link": h.get("Link", ""),
        "accuracy": mean(acc["me"]), "opp_accuracy": mean(acc["opp"]),
        "counts": counts, "phases": phases,
        "start_fen": ev["start_fen"], "start_cp": ev["start_cp"], "depth": ev["depth"],
        "plies": plies, "review": review,
    }


def main():
    global PLAYER
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", default=DEFAULT_PLAYER)
    ap.add_argument("--depth", type=int, default=16)
    ap.add_argument("--list", action="store_true", help="print game keys and exit")
    args = ap.parse_args()
    PLAYER = args.player
    if not PLAYER:
        sys.exit("No player set. Copy config.example.toml to config.toml and set `player`, or pass --player.")

    games = load_games()
    if args.list:
        for key, (g, _) in games.items():
            h = g.headers
            has = "reviewed" if (REVIEWS / f"{key}.json").exists() else "-"
            print(f"{key:40s} {'W' if h['White'].lower() == PLAYER.lower() else 'B'}  {h.get('Result','?'):8s} {has}")
        return

    vocab, warnings, out = tag_vocabulary(), [], []
    engine_box = [None]
    try:
        for key, (game, source) in games.items():
            ev = cached_eval(key, game, args.depth, engine_box)
            review = None
            rpath = REVIEWS / f"{key}.json"
            if rpath.exists():
                try:
                    review = check_review(key, json.loads(rpath.read_text()), ev["plies"], vocab, warnings)
                except json.JSONDecodeError as e:
                    warnings.append(f"{key}: reviews/{rpath.name} is not valid JSON: {e}")
            out.append(summarise(key, game, source, ev, review))
    finally:
        if engine_box[0] is not None:
            engine_box[0].quit()

    for r in REVIEWS.glob("*.json") if REVIEWS.exists() else []:
        if r.stem not in games:
            warnings.append(f"reviews/{r.name} matches no game — check the key with --list")

    milestones_path = ROOT / "milestones.json"
    milestones = json.loads(milestones_path.read_text()) if milestones_path.exists() else []
    repertoire_md = (ROOT / "REPERTOIRE.md").read_text() if (ROOT / "REPERTOIRE.md").exists() else ""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "player": PLAYER, "games": out, "milestones": milestones,
        "state_md": (ROOT / "STATE.md").read_text() if (ROOT / "STATE.md").exists() else "",
        "repertoire_md": repertoire_md, "repertoire_boards": repertoire_boards(repertoire_md),
    }
    (ROOT / "data.js").write_text("window.CHESS = " + json.dumps(payload, separators=(",", ":")) + ";\n")

    reviewed = sum(1 for g in out if g["review"])
    print(f"Wrote data.js — {len(out)} games, {reviewed} with Claude reviews.")
    for w in warnings:
        print(f"WARNING {w}")
    if warnings:
        sys.exit(1)


PLAYER = DEFAULT_PLAYER

if __name__ == "__main__":
    main()
