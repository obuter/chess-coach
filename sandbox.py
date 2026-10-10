#!/usr/bin/env python3
"""Live Stockfish for the Sandbox tab in chess.html.

Usage:
    .venv/bin/python sandbox.py      # leave it running while you use the Sandbox tab

chess.html is a plain file and can't start programs, so this small local server does it
for the Sandbox: it runs Stockfish on whatever position the board shows and streams the
best lines back while the engine thinks. python-chess decides which moves are legal, so
the page never has to know the rules. It listens on 127.0.0.1 only and answers only
chess.html (opened from Finder, or served from here as a fallback).
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chess
import chess.engine

from analyze import open_engine

ROOT = Path(__file__).resolve().parent
PORT = 8765
MAX_DEPTH = 40       # stop thinking here...
MAX_SECONDS = 120    # ...or after this long, so a forgotten tab doesn't keep the fans running
PV_PLIES = 12        # how much of each line to send
# chess.html opened from Finder sends Origin "null"; same-origin requests send none.
ORIGINS = {None, "null", f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
FILES = {"/": "chess.html", "/chess.html": "chess.html", "/data.js": "data.js"}

PROBLEMS = [
    (chess.STATUS_EMPTY, "The board is empty."),
    (chess.STATUS_NO_WHITE_KING, "White has no king."),
    (chess.STATUS_NO_BLACK_KING, "Black has no king."),
    (chess.STATUS_TOO_MANY_KINGS, "A side has more than one king."),
    (chess.STATUS_TOO_MANY_WHITE_PAWNS, "White has more than 8 pawns."),
    (chess.STATUS_TOO_MANY_BLACK_PAWNS, "Black has more than 8 pawns."),
    (chess.STATUS_PAWNS_ON_BACKRANK, "A pawn is on the first or last row, where pawns can never stand."),
    (chess.STATUS_TOO_MANY_WHITE_PIECES, "White has more than 16 pieces."),
    (chess.STATUS_TOO_MANY_BLACK_PIECES, "Black has more than 16 pieces."),
    (chess.STATUS_OPPOSITE_CHECK, "The side that isn't to move is in check, so it can't be the other side's turn."),
    (chess.STATUS_TOO_MANY_CHECKERS, "The king is in check from more than two pieces, which no move can cause."),
]

engine = None
engine_lock = threading.Lock()   # one analysis at a time
gen_lock = threading.Lock()
generation = 0                   # bumped by every new analysis; older streams see it and stop


def board_from(fen):
    """(board, problems). Castling rights or an en passant square that don't fit the position are dropped quietly."""
    try:
        board = chess.Board(fen)
    except ValueError as e:
        return None, [f"That isn't a position the board understands ({e})."]
    board.castling_rights = board.clean_castling_rights()
    if board.status() & chess.STATUS_INVALID_EP_SQUARE:
        board.ep_square = None
    status = board.status()
    return board, [text for flag, text in PROBLEMS if status & flag]


def position(fen):
    """Every legal move with its SAN and the FEN after it: the same shape as a puzzle step's `moves`."""
    board, problems = board_from(fen)
    if board is None or problems:
        return {"fen": board.fen() if board else fen, "check": False, "over": None, "moves": {}, "problems": problems}
    moves = {}
    for move in board.legal_moves:
        san = board.san(move)
        board.push(move)
        moves[move.uci()] = {"san": san, "fen": board.fen()}
        board.pop()
    over = ("checkmate" if board.is_checkmate() else "stalemate" if board.is_stalemate()
            else "insufficient" if board.is_insufficient_material() else None)
    return {"fen": board.fen(), "check": board.is_check(), "over": over, "moves": moves, "problems": []}


def engine_lines(board, infos):
    """Stockfish's best lines, scored from White's side (mate encoded as in analyze.py)."""
    out = []
    for info in infos:
        pv, score = info["pv"], info["score"]
        b, san = board.copy(stack=False), []
        for move in pv[:PV_PLIES]:
            san.append(b.san(move))
            b.push(move)
        out.append({"cp": score.white().score(mate_score=10000), "uci": [m.uci() for m in pv[:PV_PLIES]], "san": san})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def allowed(self, api):
        # Websites can't start the engine: they send their own Origin, or load it as an image or script.
        if self.headers.get("Origin") in ORIGINS and (not api or self.headers.get("Sec-Fetch-Dest") in (None, "empty")):
            return True
        self.send_error(403)
        return False

    def head(self, ctype, length=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def body(self, ctype, data):
        self.head(ctype, len(data))
        self.wfile.write(data)

    def event(self, data, name=None):
        self.wfile.write(((f"event: {name}\n" if name else "") + f"data: {data}\n\n").encode())

    def do_OPTIONS(self):
        if not self.allowed(False):
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in FILES:
            path = ROOT / FILES[url.path]
            if not self.allowed(False):
                return
            if not path.exists():
                return self.send_error(404, f"{path.name} not found; run build.py")
            ctype = "text/html" if path.suffix == ".html" else "text/javascript"
            return self.body(f"{ctype}; charset=utf-8", path.read_bytes())
        if not self.allowed(True):
            return
        if url.path == "/ping":
            self.body("application/json", json.dumps({"engine": engine.id.get("name", "Stockfish")}).encode())
        elif url.path == "/moves":
            self.body("application/json", json.dumps(position(q.get("fen", ""))).encode())
        elif url.path == "/analyse":
            try:
                lines = max(1, min(5, int(q.get("lines", 3))))
            except ValueError:
                lines = 3
            self.analyse(q.get("fen", ""), lines)
        else:
            self.send_error(404)

    def analyse(self, fen, lines):
        """Server-sent events: the best lines after each finished depth, then `done` at the depth or time cap,
        or `busy` if another page took Stockfish over."""
        global generation
        board, problems = board_from(fen)
        self.head("text/event-stream")
        try:
            if board is None or problems or board.is_game_over():
                return self.event("{}", "done")
            with gen_lock:
                generation += 1
                mine = generation
            with engine_lock:
                if generation == mine:
                    self.stream(board, lines, mine)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page moved to another position or was closed: stop thinking about this one

    def stream(self, board, lines, mine):
        """Send a snapshot only when every line has finished the same depth with an exact score.

        Stockfish also reports half-finished searches: one line already at depth 20 while the others
        are still at 19, or a temporary "at least" / "at most" score. Showing those makes the lines
        jump around, so they are read but never sent.
        """
        start = last_write = time.monotonic()
        want = min(lines, board.legal_moves.count())
        latest, sent, finished = {}, None, False
        with engine.analysis(board, multipv=lines) as an:
            while True:
                time.sleep(0.2)
                if generation != mine:
                    # Another page asked for Stockfish. Say so, so that page doesn't think the engine died
                    # and take it straight back (a page that moved on has closed this stream already).
                    self.event("{}", "busy")
                    return
                try:
                    while not an.would_block():
                        info = an.get()
                        if "pv" in info and "score" in info and not info.get("lowerbound") and not info.get("upperbound"):
                            latest[info.get("multipv", 1)] = info
                except chess.engine.AnalysisComplete:
                    finished = True
                depths = {info.get("depth", 0) for info in latest.values()}
                now = time.monotonic()
                if len(latest) == want and len(depths) == 1:
                    payload = {"depth": depths.pop(), "lines": engine_lines(board, [latest[k] for k in sorted(latest)])}
                    if payload != sent:
                        self.event(json.dumps(payload))
                        sent, last_write = payload, now
                if now - last_write >= 2:
                    self.wfile.write(b": still thinking\n\n")  # a closed page shows up here as BrokenPipe
                    last_write = now
                if finished or (sent and sent["depth"] >= MAX_DEPTH) or now - start >= MAX_SECONDS:
                    self.event("{}", "done")
                    return


def main():
    global engine
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is busy. Is sandbox.py already running in another Terminal window?")
    engine = open_engine()
    engine.configure({"Hash": 256})
    print(f"{engine.id.get('name', 'Stockfish')} is ready. Open the Sandbox tab in chess.html and leave this window open.")
    print(f"If the tab can't connect, open http://127.0.0.1:{PORT}/#/sandbox instead. Ctrl+C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        engine.quit()


if __name__ == "__main__":
    main()
