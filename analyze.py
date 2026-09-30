#!/usr/bin/env python3
"""Replay a PGN through Stockfish and report the moves that actually cost the game.

Usage:
    .venv/bin/python analyze.py games/FILE.txt              # top moments per game
    .venv/bin/python analyze.py games/FILE.txt --all        # every flagged move
    .venv/bin/python analyze.py games/FILE.txt --player NAME --depth 18

Severity is measured in WIN PROBABILITY lost, not centipawns. Going from +9 to +3
is a big centipawn drop but changes nothing — you were winning, you are still
winning. Going from +2 to -1 flips the game. Only the second one is a lesson.

This script reports facts: evals, FENs, the engine's move, the refutation it saw.
It gives no advice. The coach reads this and does the teaching.
"""

import argparse
import math
import shutil
import sys
import tomllib
from pathlib import Path

import chess
import chess.engine
import chess.pgn


def load_config():
    """config.toml next to this file (gitignored; copy config.example.toml)."""
    path = Path(__file__).resolve().parent / "config.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


CONFIG = load_config()
DEFAULT_PLAYER = CONFIG.get("player", "")
ENGINE_PATH = CONFIG.get("stockfish") or shutil.which("stockfish") or "stockfish"
PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}

# Win-probability points lost by a single move.
BLUNDER = 20.0
MISTAKE = 10.0


def win_pct(cp):
    """Centipawns -> chance of winning, 0..100. Lichess' conversion."""
    if cp is None:
        return 50.0
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * max(-1500, min(1500, cp)))) - 1)


def score_cp(info, pov):
    return info["score"].pov(pov).score(mate_score=10000)


def material(board, color):
    return sum(PIECE_VALUES[pt] * len(board.pieces(pt, color)) for pt in PIECE_VALUES)


def phase_of(move_no):
    if move_no <= 12:
        return "opening"
    if move_no <= 30:
        return "middlegame"
    return "endgame"


def fmt_eval(cp):
    if cp is None:
        return "?"
    if abs(cp) >= 9000:
        # score(mate_score=10000) is 10000 minus the distance in MOVES, not plies.
        return f"mate in {10000 - abs(cp)}" + ("" if cp > 0 else " against you")
    return f"{cp/100:+.1f}"


def label_of(board, move):
    dots = "." if board.turn == chess.WHITE else "..."
    return f"{board.fullmove_number}{dots}{board.san(move)}"


def evaluate_game(game, engine, limit):
    """One engine call per position, for every move of both sides.

    Scores are centipawns from White's side (mate = ±10000 minus the distance).
    The analysis of the position before a move gives that move's best move; the
    analysis after it gives the eval and the opponent's best reply. build.py
    caches this dict as JSON, so keep it plain data.
    """
    board = game.board()
    start_fen = board.fen()
    info = engine.analyse(board, limit)
    start_cp = score_cp(info, chess.WHITE)
    plies = []
    for node in game.mainline():
        move = node.move
        pv = info.get("pv", [])
        rec = {
            "ply": len(plies) + 1, "move_no": board.fullmove_number,
            "san": board.san(move), "uci": move.uci(),
            "best_uci": pv[0].uci() if pv else None,
            "best_san": board.san(pv[0]) if pv else None,
            "best_line": board.variation_san(pv[:5]) if pv else None,
        }
        board.push(move)
        info = engine.analyse(board, limit)
        reply = info.get("pv", [])
        rec.update(fen=board.fen(), cp=score_cp(info, chess.WHITE),
                   reply_line=board.variation_san(reply[:4]) if reply else None,
                   clock=node.clock())
        plies.append(rec)
    return {"start_fen": start_fen, "start_cp": start_cp, "plies": plies}


def analyse_game(game, engine, player, limit, show_all, top_n):
    h = game.headers
    white, black = h.get("White", "?"), h.get("Black", "?")
    if player.lower() == white.lower():
        pov, opp, opp_elo, my_elo = chess.WHITE, black, h.get("BlackElo", "?"), h.get("WhiteElo", "?")
    elif player.lower() == black.lower():
        pov, opp, opp_elo, my_elo = chess.BLACK, white, h.get("WhiteElo", "?"), h.get("BlackElo", "?")
    else:
        return None

    side = "White" if pov == chess.WHITE else "Black"
    opening = h.get("ECOUrl", "").rsplit("/", 1)[-1].replace("-", " ")
    print("=" * 78)
    print(f"{h.get('Date','?')}   {player} ({my_elo}) as {side}   vs   {opp} ({opp_elo})")
    print(f"Result {h.get('Result','?')} — {h.get('Termination','?')}")
    print(f"Opening {h.get('ECO','?')}: {opening}   |   time control {h.get('TimeControl','?')}s")
    print("=" * 78)

    ev = evaluate_game(game, engine, limit)
    sign = 1 if pov == chess.WHITE else -1
    board = game.board()
    prev_cp = sign * ev["start_cp"]
    findings, curve = [], []

    for p, node in zip(ev["plies"], game.mainline()):
        move = node.move
        mine = board.turn == pov
        move_no = board.fullmove_number
        lbl = label_of(board, move)
        fen_before = board.fen()

        board.push(move)
        cp = sign * p["cp"]

        if mine:
            lost = win_pct(prev_cp) - win_pct(cp)
            if lost >= MISTAKE:
                findings.append({
                    "label": lbl, "lost": lost, "before": prev_cp, "after": cp,
                    "fen": fen_before, "best": p["best_san"] or "?", "line": p["best_line"] or "?",
                    "refutation": p["reply_line"] or "?", "phase": phase_of(move_no),
                    "mat_me": material(board, pov), "mat_opp": material(board, not pov),
                    "move_no": move_no,
                })
            if move_no % 10 == 0:
                curve.append((move_no, cp, material(board, pov) - material(board, not pov)))
        prev_cp = cp

    findings.sort(key=lambda f: -f["lost"])
    shown = findings if show_all else findings[:top_n]

    if not findings:
        print("\nNothing you played cost 10% or more of the win. Clean game.")
    for i, f in enumerate(shown, 1):
        sev = "BLUNDER" if f["lost"] >= BLUNDER else "mistake"
        print(f"\n--- {i}. {sev}   {f['label']}   [{f['phase']}]   cost {f['lost']:.0f}% of the win")
        print(f"    eval {fmt_eval(f['before'])} -> {fmt_eval(f['after'])}"
              f"   (material after: you {f['mat_me']} vs them {f['mat_opp']})")
        print(f"    position BEFORE your move: {f['fen']}")
        print(f"    engine move: {f['best']}   —   {f['line']}")
        print(f"    what it allowed: {f['refutation']}")

    if findings and not show_all and len(findings) > top_n:
        rest = ", ".join(f["label"] for f in findings[top_n:])
        print(f"\n    (also flagged, smaller: {rest})")

    print("\nEVAL CURVE (from your side, + = you better):")
    print("    " + "  ".join(f"m{n}:{fmt_eval(c)}({d:+d})" for n, c, d in curve) or "    n/a")

    blunders = sum(1 for f in findings if f["lost"] >= BLUNDER)
    phases = {}
    for f in findings:
        phases[f["phase"]] = phases.get(f["phase"], 0) + 1
    spread = ", ".join(f"{v} in {k}" for k, v in phases.items()) or "none"
    print(f"\nSUMMARY: {blunders} blunder(s), {len(findings)-blunders} mistake(s)  —  {spread}")

    term = h.get("Termination", "")
    if "on time" in term:
        who = "YOU" if player.lower() in term.lower() else "your opponent"
        print(f"CLOCK: {who} won on time — the board result was not what decided this game.")
    print()
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pgn", help="PGN or .txt file; may hold several games")
    ap.add_argument("--player", default=DEFAULT_PLAYER)
    ap.add_argument("--depth", type=int, default=16)
    ap.add_argument("--all", action="store_true", help="show every flagged move")
    ap.add_argument("--top", type=int, default=5, help="moments to show per game")
    args = ap.parse_args()
    if not args.player:
        sys.exit("No player set. Copy config.example.toml to config.toml and set `player`, or pass --player.")

    limit = chess.engine.Limit(depth=args.depth)
    try:
        engine = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH)
    except FileNotFoundError:
        sys.exit(f"Stockfish not found at {ENGINE_PATH}. Install it (brew/apt install stockfish) or set `stockfish` in config.toml.")

    total = 0
    try:
        with open(args.pgn) as fh:
            while (game := chess.pgn.read_game(fh)) is not None:
                if analyse_game(game, engine, args.player, limit, args.all, args.top) is not None:
                    total += 1
    finally:
        engine.quit()

    if total == 0:
        print(f"No games found for player '{args.player}'. Check the --player name.")
    else:
        print(f"Analysed {total} game(s) for {args.player}.")


if __name__ == "__main__":
    main()
