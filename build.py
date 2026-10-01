#!/usr/bin/env python3
"""Build data.js for chess.html: engine data for every game + Claude's reviews.

Usage:
    .venv/bin/python build.py            # analyse new games, write data.js
    .venv/bin/python build.py --list     # print game keys (to name reviews/<key>.json)
    .venv/bin/python build.py --depth 18 # re-analyse everything at a new depth
    .venv/bin/python build.py --fetch    # download new games from chess.com first
    .venv/bin/python build.py --fetch --since 2025-01   # ...including older months

Stockfish results are cached in cache/<key>.json, so only new games cost engine
time. Reviews are read from reviews/<key>.json and checked against the PGN: a
comment pinned to a ply whose SAN does not match is reported, never shown.
"""

import argparse
import json
import math
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from analyze import BLUNDER, CONFIG, DEFAULT_PLAYER, MISTAKE, evaluate_game, open_engine, phase_of, win_pct

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


CHESSCOM_API = "https://api.chess.com/pub/player"
USER_AGENT = "chess-coach (+https://github.com/obuter/chess-coach)"


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def earliest_month_on_disk():
    """'YYYY-MM' of the oldest game already in games/, or None."""
    months = []
    for path in GAMES.glob("*"):
        if path.suffix in (".pgn", ".txt"):
            months += [f"{y}-{m}" for y, m in re.findall(r'\[Date "(\d{4})\.(\d{2})', path.read_text())]
    return min(months) if months else None


def fetch_chesscom(player, since, time_classes):
    """Download monthly archives into games/chesscom-YYYY-MM.pgn.

    A month is fetched when its file is missing, or was last written before the month
    ended (so the current month is re-fetched each run, and a finished month once more
    after it ends). Only standard chess is kept; `time_classes` (e.g. ["rapid"])
    narrows it further. Games that are already in another file are fine: the build
    de-duplicates by game key. Network trouble is a warning, never a failed build.
    """
    try:
        archives = http_json(f"{CHESSCOM_API}/{player.lower()}/games/archives")["archives"]
    except urllib.error.HTTPError as e:
        print(f"fetch: chess.com said {e.code} for player '{player}'" + (" — check `player` in config.toml" if e.code == 404 else ""))
        return
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"fetch: couldn't reach chess.com ({e}); building from the games already on disk")
        return
    months = [(url, "-".join(url.rstrip("/").split("/")[-2:])) for url in archives]
    if not months:
        print(f"fetch: no games on chess.com for '{player}'")
        return
    since = since or earliest_month_on_disk() or months[-1][1]
    GAMES.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    for url, ym in months:
        if ym < since:
            continue
        path = GAMES / f"chesscom-{ym}.pgn"
        year, month = map(int, ym.split("-"))
        month_end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
        if path.exists() and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= month_end:
            continue  # written after the month ended: complete
        try:
            data = http_json(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"fetch: {ym} failed ({e}); skipped")
            continue
        kept = [g["pgn"] for g in data.get("games", [])
                if g.get("rules") == "chess" and g.get("pgn")
                and (not time_classes or g.get("time_class") in time_classes)]
        path.write_text("\n\n".join(p.strip() for p in kept) + "\n")
        state = "in progress" if now < month_end else "complete"
        print(f"fetch: {ym} — {len(kept)} of {len(data.get('games', []))} games kept ({state})")


def cached_eval(key, game, depth, engine_box):
    path = CACHE / f"{key}.json"
    if path.exists():
        data = json.loads(path.read_text())
        if data.get("depth") == depth:
            return data
    if engine_box[0] is None:
        engine_box[0] = open_engine()
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


PUZZLE_DEPTH = 12     # per legal move; only grades alternatives, the answer comes from the depth-16 cache
PUZZLE_MAX_STEPS = 3  # a forced mate is played out move by move, up to this many of your moves


def puzzle_step(board, best, engine):
    """Score every legal move from the mover's side; the page grades a click against these."""
    pov, moves = board.turn, {}
    for move in board.legal_moves:
        san = board.san(move)
        board.push(move)
        info = engine.analyse(board, chess.engine.Limit(depth=PUZZLE_DEPTH))
        moves[move.uci()] = {"san": san, "cp": info["score"].pov(pov).score(mate_score=10000), "fen": board.fen()}
        board.pop()
    best = best or max(moves, key=lambda u: moves[u]["cp"])
    return {"fen": board.fen(), "best": best, "moves": moves}


def build_puzzle(fen, best_uci, engine):
    """One puzzle: the position before your move, played forward while it is a forced mate."""
    board, steps = chess.Board(fen), []
    while True:
        step = puzzle_step(board, best_uci if not steps else None, engine)
        steps.append(step)
        board.push(chess.Move.from_uci(step["best"]))
        if board.is_game_over() or step["moves"][step["best"]]["cp"] < 9990 or len(steps) == PUZZLE_MAX_STEPS:
            break
        reply = engine.analyse(board, chess.engine.Limit(depth=PUZZLE_DEPTH))["pv"][0]
        step["reply"] = {"uci": reply.uci(), "san": board.san(reply)}
        board.push(reply)
    return steps


def build_puzzles(out, engine_box):
    """Every reviewed moment where you missed the engine's move becomes a puzzle. Cached in cache/puzzles/."""
    folder = CACHE / "puzzles"
    puzzles = []
    for g in out:
        rv = g["review"]
        for m in (rv["moments"] if rv else []):
            p = g["plies"][m["ply"] - 1]
            if (p["ply"] % 2 == 1) != (g["colour"] == "white") or not p.get("best_uci") or p["best_uci"] == p["uci"]:
                continue
            fen = g["plies"][m["ply"] - 2]["fen"] if m["ply"] > 1 else g["start_fen"]
            pid = f"{g['key']}_{m['ply']}"
            path = folder / f"{pid}.json"
            data = json.loads(path.read_text()) if path.exists() else None
            if not data or data.get("fen") != fen or data.get("best") != p["best_uci"] or data.get("depth") != PUZZLE_DEPTH:
                if engine_box[0] is None:
                    engine_box[0] = open_engine()
                print(f"  puzzle {pid} ...", flush=True)
                data = {"fen": fen, "best": p["best_uci"], "depth": PUZZLE_DEPTH,
                        "steps": build_puzzle(fen, p["best_uci"], engine_box[0])}
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data))
            puzzles.append({"id": pid, "key": g["key"], "ply": m["ply"], "steps": data["steps"]})
    return puzzles


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
    ap.add_argument("--fetch", action="store_true", help="download your games from chess.com into games/ first")
    ap.add_argument("--since", metavar="YYYY-MM", help="with --fetch: earliest month to download (default: your oldest game on disk)")
    args = ap.parse_args()
    PLAYER = args.player
    if not PLAYER:
        sys.exit("No player set. Copy config.example.toml to config.toml and set `player`, or pass --player.")

    if args.fetch:
        fetch_chesscom(PLAYER, args.since, CONFIG.get("time_classes"))
    games = load_games()
    new = [k for k in games if not (CACHE / f"{k}.json").exists()]
    if new and not args.list:
        print(f"{len(new)} new game(s) for Stockfish — cached after this run.")
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
        puzzles = build_puzzles(out, engine_box)
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
        "player": PLAYER, "games": out, "milestones": milestones, "puzzles": puzzles,
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
