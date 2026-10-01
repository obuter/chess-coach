"""explain.py KEY [KEY...] — board-verified facts for writing a review.

For each game: header, eval curve, clock, how it ended; then for every move of the
player that cost >= 10% (and every opponent blunder >= 20%), the mechanics read off
the actual board: what the best move captures and whether that target was guarded,
whether the best move / reply gives check or mate, what the reply captures and
whether it was guarded, material swing over the engine's line.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chess  # noqa: E402
import build  # noqa: E402
from analyze import fmt_eval  # noqa: E402

V = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
NAME = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}


def mat(b, c):
    return sum(V[p.piece_type] for p in b.piece_map().values() if p.color == c)


def cap_desc(b, mv):
    """'Bxb5 takes pawn b5 (guarded by 1: a6-pawn)' — before the move is pushed."""
    san = b.san(mv)
    if not b.is_capture(mv):
        return san + (" (check)" if b.gives_check(mv) else "")
    if b.is_en_passant(mv):
        return san + " (en passant)"
    victim = b.piece_at(mv.to_square)
    guards = b.attackers(victim.color, mv.to_square)
    gl = ", ".join(f"{NAME[b.piece_at(s).piece_type]} {chess.square_name(s)}" for s in guards)
    return f"{san} takes {NAME[victim.piece_type]} {chess.square_name(mv.to_square)} " + (
        f"(guarded by {len(guards)}: {gl})" if guards else "(UNGUARDED)") + (" +check" if b.gives_check(mv) else "")


def line_swing(b, ucis, pov, n=4):
    b = b.copy()
    start = mat(b, pov) - mat(b, not pov)
    for u in ucis[:n]:
        m = chess.Move.from_uci(u)
        if m not in b.legal_moves:
            break
        b.push(m)
    return (mat(b, pov) - mat(b, not pov)) - start


def uci_line(b, san_line):
    """Convert '12. Nxe5 d6 13. Nf3' to uci list, from board b."""
    out, bb = [], b.copy()
    for tok in (san_line or "").split():
        if tok[0].isdigit():
            continue
        try:
            m = bb.parse_san(tok)
        except ValueError:
            break
        out.append(m.uci()); bb.push(m)
    return out


def main():
    games = build.load_games()
    for key in sys.argv[1:]:
        game, _ = games[key]
        ev = json.load(open(build.CACHE / f"{key}.json"))
        s = build.summarise(key, game, "", ev, None)
        pov = chess.WHITE if s["colour"] == "white" else chess.BLACK
        sg = 1 if pov == chess.WHITE else -1
        P = s["plies"]
        print("=" * 100)
        print(f"{key} | {s['colour']} vs {s['opponent']} ({s['opp_elo']}), me {s['my_elo']} | {s['result']} {s['termination']} | tc {s['time_control']} | acc {s['accuracy']}/{s['opp_accuracy']} | {s['counts']}")
        print("opening:", " ".join((f"{p['move_no']}." if p['ply'] % 2 else "") + p["san"] for p in P[:12]))
        curve = [f"m{p['move_no']}:{fmt_eval(sg * p['cp'])}" for p in P if p["move_no"] % 5 == 0 and (p["ply"] % 2 == 1) == (pov == chess.WHITE)]
        print("curve:", " ".join(curve), "| final", fmt_eval(sg * P[-1]["cp"]), f"| plies {len(P)}")
        clocks = [(p["move_no"], p["clock"]) for p in P if p["clock"] is not None and (p["ply"] % 2 == 1) == (pov == chess.WHITE)]
        if clocks:
            opp_clk = [p["clock"] for p in P if p["clock"] is not None and (p["ply"] % 2 == 1) != (pov == chess.WHITE)]
            print(f"my clock at end {clocks[-1][1]:.0f}s, opp {opp_clk[-1] if opp_clk else '?'}s; my min {min(c for _, c in clocks):.0f}s")
        mates_missed = [p["move_no"] for p in P if (p["ply"] % 2 == 1) == (pov == chess.WHITE) and p["best_san"] and p["best_san"].endswith("#") and p["san"] != p["best_san"]]
        if mates_missed:
            print("MATE-IN-1 MISSED at moves:", mates_missed)
        board = chess.Board(ev["start_fen"])
        prev = ev["start_cp"]
        for i, p in enumerate(P):
            mine = (p["ply"] % 2 == 1) == (pov == chess.WHITE)
            flag = (mine and p["cls"] in ("mistake", "blunder")) or (not mine and p["cls"] == "blunder")
            if flag:
                who = "ME " if mine else "opp"
                played = chess.Move.from_uci(p["uci"])
                best = chess.Move.from_uci(p["best_uci"]) if p["best_uci"] else None
                print(f" {who} ply {p['ply']:3d} {p['move_no']}{'.' if p['ply'] % 2 else '...'}{p['san']:7s} {p['cls']:8s} -{p['lost']:.0f}%  "
                      f"{fmt_eval(sg * prev)} -> {fmt_eval(sg * p['cp'])}  clk {p['clock']}")
                if mine:
                    print(f"      played: {cap_desc(board, played)}")
                    if best:
                        bl = uci_line(board, p["best_line"])
                        print(f"      best:   {cap_desc(board, best)} | line {p['best_line']} | swing {line_swing(board, bl, pov):+d}")
                after = board.copy(); after.push(played)
                nxt = P[i + 1] if i + 1 < len(P) else None
                if mine and nxt and nxt["best_uci"]:
                    r = chess.Move.from_uci(nxt["best_uci"])
                    rl = uci_line(after, p["reply_line"])
                    print(f"      reply:  {cap_desc(after, r)} | line {p['reply_line']} | swing {line_swing(after, rl, pov):+d}"
                          f" | actually played {nxt['san']}")
            board.push(chess.Move.from_uci(p["uci"]))
            prev = p["cp"]
        print(f"final: {board.fen()}  material me {mat(board, pov)} v {mat(board, not pov)}")


if __name__ == "__main__":
    main()
