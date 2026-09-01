"""Generate chess puzzles from a PGN file using Stockfish.

Usage:
    python pgn_to_puzzles.py <input.pgn> [-o out.json] [--engine path/to/stockfish]
                             [--depth 18] [--swing 200] [--mate-only]
                             [--multipv 2] [--min-ply 8] [--max-puzzles N]

A puzzle is detected when a player's move turns a roughly-equal/winning position
into a clearly-losing one (centipawn swing >= --swing, from the mover's POV).
The puzzle's starting position is *after* that blunder; the solution is the
engine's principal variation, played out as long as the side-to-move has only
one clearly best reply (gap to second-best > --swing/2).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

import chess
import chess.engine
import chess.pgn

MATE_SCORE = 100_000

# Quality filters — skip blunders that don't make good puzzles
QUALITY_EVAL_BEFORE_MIN = -200   # blunderer wasn't already losing badly
QUALITY_EVAL_AFTER_MAX = -150    # after blunder, clearly losing for blunderer


@dataclass
class Puzzle:
    game_id: str
    white: str
    black: str
    event: str
    ply: int
    fen: str
    blunder_move: str
    eval_before_cp: int
    eval_after_cp: int
    solution_uci: list[str]
    solution_san: list[str]
    themes: list[str]
    level: int  # 1=very easy ... 5=very hard
    # ---- Pointer-back metadata (added 2026-05-07) ----
    # All optional — older puzzles won't have these.
    site: str = ""             # e.g. "https://lichess.org/UEBgKDn3"
    date: str = ""             # e.g. "2024.10.03"
    round: str = ""            # e.g. "1.257"
    opening: str = ""          # ECO + name, e.g. "C46 — Three Knights Opening"
    time_control: str = ""     # e.g. "600+2"
    white_team: str = ""       # parsed from "NAME (TEAM)" if present
    black_team: str = ""       # ditto
    source_platform: str = ""  # "lichess" | "chesscom" | "" — derived from site URL


def assign_level(solution_uci: list[str], eval_before_cp: int,
                 eval_after_cp: int, themes: list[str]) -> int:
    """Heuristic difficulty: 1=very easy, 5=very hard.

    Simplified 2026-05-10 (per user request): difficulty depends ONLY on
    eval landscape (swing, post-eval magnitude), brilliance markers
    (quiet, sacrifice), and one structural exception (mate-in-1 — only
    one move to find, no calculation needed). Solution length itself is
    NOT a difficulty factor — a long forced sequence with simple moves
    is still easy, a short brilliant move is still hard. All other
    themes (fork, pin, mate-in-2+, capture, etc.) are descriptive tags
    only, with no level effect.
    """
    # 2026-05-11: Rule-based bucketing for clean bell-curve distribution
    # (L1≈10%, L2≈25%, L3≈35%, L4≈20%, L5≈10%). Each puzzle slots into
    # a fixed bucket by solution length + key markers, so similar puzzles
    # always land at the same level — predictable for users.
    has_quiet      = "quiet" in themes
    has_sacrifice  = "sacrifice" in themes
    has_capture    = "capture" in themes
    has_check      = "check" in themes
    is_mate1       = "mate-in-1" in themes
    is_mate_deep   = any(f"mate-in-{n}" in themes for n in (4, 5, 6, 7, 8, 9, 10, 11, 12))
    NAMED_MATES = {"smothered-mate", "anastasia-mate", "arabian-mate",
                   "boden-mate", "dovetail-mate", "hook-mate"}
    has_named_mate = any(t in themes for t in NAMED_MATES)
    sol_len        = len(solution_uci)
    swing          = abs(eval_before_cp - eval_after_cp)

    # Mate-in-1: always trivially L1.
    if is_mate1:
        return 1

    if sol_len <= 2:
        # 2-ply (~50% of library) — one user move.
        if has_quiet:     return 3    # subtle non-tactical 1st move
        if has_sacrifice: return 2
        if swing >= 800:  return 1    # obvious "free piece" blunder
        if has_check:     return 1    # forcing check tactic
        if has_capture:   return 2
        return 3

    if sol_len <= 4:
        # 4-ply (2 user moves)
        if has_quiet and has_sacrifice and has_named_mate: return 5
        if has_quiet:                       return 4
        if has_sacrifice:                   return 3
        if has_named_mate or is_mate_deep:  return 4
        return 3

    if sol_len <= 6:
        # 6-ply (3 user moves)
        if has_quiet and has_sacrifice: return 5
        if has_named_mate:              return 5
        return 4

    if sol_len <= 8:
        # 8-ply (4 user moves)
        if has_quiet or has_sacrifice or is_mate_deep or has_named_mate:
            return 5
        return 4

    # 9+ ply — long forced lines always L5
    return 5


def score_cp(score: chess.engine.PovScore, pov: chess.Color) -> int:
    s = score.pov(pov)
    if s.is_mate():
        m = s.mate()
        return MATE_SCORE if m and m > 0 else -MATE_SCORE
    return s.score(mate_score=MATE_SCORE) or 0


def info_score(info: chess.engine.InfoDict) -> chess.engine.PovScore:
    s = info.get("score")
    if s is None:
        return chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)
    return s


def analyse(engine: chess.engine.SimpleEngine, board: chess.Board, depth: int,
            multipv: int) -> list[chess.engine.InfoDict]:
    info = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
    return info if isinstance(info, list) else [info]


def is_forced_continuation(infos: list[chess.engine.InfoDict], pov: chess.Color,
                           swing: int) -> bool:
    if len(infos) < 2:
        return True
    best = score_cp(info_score(infos[0]), pov)
    second = score_cp(info_score(infos[1]), pov)
    return (best - second) >= swing // 2


PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}


def material_count(board: chess.Board, color: chess.Color) -> int:
    """Sum of piece values for `color`."""
    total = 0
    for pt, v in PIECE_VALUES.items():
        total += len(board.pieces(pt, color)) * v
    return total


import re as _re

_TEAM_RE = _re.compile(r"\(([A-Z][A-Z0-9 _.-]{0,15})\)\s*$")


def parse_player_team(name: str) -> tuple[str, str]:
    """Split 'BHARADWAJ, AJAY (NVIDIA)' → ('BHARADWAJ, AJAY', 'NVIDIA')."""
    if not name:
        return "", ""
    m = _TEAM_RE.search(name.strip())
    if not m:
        return name.strip(), ""
    team = m.group(1).strip()
    cleaned = name[:m.start()].strip().rstrip(",").strip()
    return cleaned, team


def detect_platform(site: str) -> str:
    """Derive 'lichess' / 'chesscom' / '' from a Site URL."""
    if not site:
        return ""
    s = site.lower()
    if "lichess.org" in s:
        return "lichess"
    if "chess.com" in s:
        return "chesscom"
    return ""


def is_trivial_endgame(board: chess.Board) -> bool:
    """Skip puzzles where one side has only a king (K+anything vs K).
    These produce repetitive 'mate-the-lone-king' patterns
    (K+Q vs K, K+R vs K, K+P vs K, etc.)."""
    white_count = chess.popcount(board.occupied_co[chess.WHITE])
    black_count = chess.popcount(board.occupied_co[chess.BLACK])
    return white_count == 1 or black_count == 1


# ============================================================================
# Theme detection helpers (added 2026-05-08 — expanded from 8 to 26 themes)
# ============================================================================


def _is_advanced_pawn_move(piece, to_sq: int) -> bool:
    """Pawn moving to 6th rank (white) or 3rd rank (black) — close to promotion."""
    if not piece or piece.piece_type != chess.PAWN:
        return False
    rank = chess.square_rank(to_sq)
    return rank >= 5 if piece.color == chess.WHITE else rank <= 2


def _detect_fork(board_after: chess.Board, to_sq: int, mover_color) -> bool:
    """The piece on `to_sq` attacks 2+ enemy pieces (= fork / double-attack)."""
    targets = 0
    for sq in board_after.attacks(to_sq):
        p = board_after.piece_at(sq)
        if p and p.color != mover_color:
            targets += 1
    return targets >= 2


def _detect_discovered_attack(board_before: chess.Board, m: chess.Move,
                              mover_color) -> bool:
    """Move m opens a new attack from a NON-moved solver slider piece."""
    board_after = board_before.copy()
    board_after.push(m)
    for sq in chess.SQUARES:
        if sq == m.to_square:
            continue
        p = board_after.piece_at(sq)
        if not p or p.color != mover_color:
            continue
        if p.piece_type not in (chess.QUEEN, chess.ROOK, chess.BISHOP):
            continue
        before_atk = (board_before.attacks(sq)
                      if board_before.piece_at(sq) else chess.SquareSet())
        for tsq in board_after.attacks(sq) - before_atk:
            target = board_after.piece_at(tsq)
            if target and target.color != mover_color:
                return True
    return False


_RAY_DELTAS = {
    chess.QUEEN:  [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)],
    chess.ROOK:   [(-1,0),(1,0),(0,-1),(0,1)],
    chess.BISHOP: [(-1,-1),(-1,1),(1,-1),(1,1)],
}


def _detect_pin_skewer_xray(board_after: chess.Board,
                            mover_color) -> tuple[bool, bool, bool]:
    """Returns (has_pin, has_skewer, has_xray) by scanning slider rays."""
    enemy = not mover_color
    has_pin = False
    for sq in chess.SQUARES:
        p = board_after.piece_at(sq)
        if p and p.color == enemy and p.piece_type != chess.KING:
            if board_after.is_pinned(enemy, sq):
                has_pin = True
                break

    has_skewer = False
    has_xray = False
    for sq in chess.SQUARES:
        p = board_after.piece_at(sq)
        if not p or p.color != mover_color or p.piece_type not in _RAY_DELTAS:
            continue
        sf, sr = chess.square_file(sq), chess.square_rank(sq)
        for df, dr in _RAY_DELTAS[p.piece_type]:
            f, r = sf + df, sr + dr
            first = second = None
            while 0 <= f < 8 and 0 <= r < 8:
                ts = chess.square(f, r)
                tp = board_after.piece_at(ts)
                if tp:
                    if first is None:
                        first = tp
                    else:
                        second = tp
                        break
                f += df; r += dr
            if first and second and first.color == enemy and second.color == enemy:
                has_xray = True
                v1 = PIECE_VALUES.get(first.piece_type, 0)
                v2 = PIECE_VALUES.get(second.piece_type, 0)
                # Skewer: front piece more valuable than back (forces front to move)
                if v1 > v2 and second.piece_type != chess.KING:
                    has_skewer = True
    return has_pin, has_skewer, has_xray


def _detect_remove_defender(board_before: chess.Board, m: chess.Move,
                            mover_color) -> bool:
    """Capture removes a piece that was defending a teammate (now hanging)."""
    if not board_before.is_capture(m):
        return False
    captured_sq = m.to_square
    if board_before.is_en_passant(m):
        captured_sq = chess.square(chess.square_file(m.to_square),
                                   chess.square_rank(m.from_square))
    enemy = not mover_color
    captured = board_before.piece_at(captured_sq)
    if not captured or captured.color != enemy:
        return False
    defended_squares = list(board_before.attacks(captured_sq))
    board_after = board_before.copy()
    board_after.push(m)
    for sq in defended_squares:
        p = board_before.piece_at(sq)
        if p and p.color == enemy and sq != captured_sq:
            attackers = board_after.attackers(mover_color, sq)
            defenders = board_after.attackers(enemy, sq)
            if attackers and not defenders:
                return True
    return False


def _detect_trapped_piece(board: chess.Board, mover_color) -> bool:
    """Enemy major/minor piece is attacked and has no safe destination."""
    enemy = not mover_color
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if not p or p.color != enemy or p.piece_type in (chess.KING, chess.PAWN):
            continue
        if not board.is_attacked_by(mover_color, sq):
            continue
        moves = [mv for mv in board.legal_moves if mv.from_square == sq]
        if not moves:
            continue
        any_safe = False
        for mv in moves:
            if not board.attackers(mover_color, mv.to_square):
                any_safe = True
                break
        if not any_safe:
            return True
    return False


def _detect_mate_patterns(board: chess.Board, mover_color,
                          last_move: chess.Move) -> set[str]:
    """Named mate patterns: back-rank, smothered, arabian, anastasia,
    boden, dovetail, hook."""
    out: set[str] = set()
    enemy = not mover_color
    king_sq = board.king(enemy)
    if king_sq is None:
        return out
    kf, kr = chess.square_file(king_sq), chess.square_rank(king_sq)
    mating = board.piece_at(last_move.to_square)
    if not mating:
        return out

    # Back-rank mate
    enemy_back = 7 if enemy == chess.WHITE else 0
    if kr == enemy_back and mating.piece_type in (chess.ROOK, chess.QUEEN):
        front = kr + (-1 if enemy == chess.WHITE else 1)
        blocked = True
        for df in (-1, 0, 1):
            nf = kf + df
            if not 0 <= nf < 8:
                continue
            nsq = chess.square(nf, front)
            p = board.piece_at(nsq)
            if p is None or p.color != enemy:
                blocked = False
                break
        if blocked:
            out.add("back-rank-mate")

    # Smothered mate (knight check, all king-neighbors are friendly)
    if mating.piece_type == chess.KNIGHT:
        all_friendly = True
        for df in (-1, 0, 1):
            for dr in (-1, 0, 1):
                if df == 0 and dr == 0:
                    continue
                nf, nr = kf + df, kr + dr
                if not (0 <= nf < 8 and 0 <= nr < 8):
                    continue
                p = board.piece_at(chess.square(nf, nr))
                if p is None or p.color != enemy:
                    all_friendly = False
                    break
            if not all_friendly:
                break
        if all_friendly:
            out.add("smothered-mate")

    # Arabian mate (rook + knight, king in corner)
    if mating.piece_type == chess.ROOK and kf in (0, 7) and kr in (0, 7):
        for sq in chess.SQUARES:
            p = board.piece_at(sq)
            if p and p.color == mover_color and p.piece_type == chess.KNIGHT:
                sf, sr = chess.square_file(sq), chess.square_rank(sq)
                if {abs(sf - kf), abs(sr - kr)} == {1, 2}:
                    out.add("arabian-mate")
                    break

    # Anastasia's mate (rook on edge file + knight, NOT in corner)
    if mating.piece_type == chess.ROOK and kf in (0, 7) and kr not in (0, 7):
        has_knight = False
        for s in chess.SQUARES:
            q = board.piece_at(s)
            if q and q.color == mover_color and q.piece_type == chess.KNIGHT:
                has_knight = True
                break
        if has_knight:
            out.add("anastasia-mate")

    # Boden's mate (mate by bishop, 2+ solver bishops on board)
    if mating.piece_type == chess.BISHOP:
        bishops = 0
        for s in chess.SQUARES:
            q = board.piece_at(s)
            if q and q.color == mover_color and q.piece_type == chess.BISHOP:
                bishops += 1
        if bishops >= 2:
            out.add("boden-mate")

    # Dovetail mate (queen adjacent, 2+ enemy pieces blocking around king)
    if mating.piece_type == chess.QUEEN:
        qsq = last_move.to_square
        qf, qr = chess.square_file(qsq), chess.square_rank(qsq)
        if abs(qf - kf) <= 1 and abs(qr - kr) <= 1:
            blocked_count = 0
            for df in (-1, 0, 1):
                for dr in (-1, 0, 1):
                    if df == 0 and dr == 0:
                        continue
                    nf, nr = kf + df, kr + dr
                    if not (0 <= nf < 8 and 0 <= nr < 8):
                        continue
                    nsq = chess.square(nf, nr)
                    if nsq == qsq:
                        continue
                    p = board.piece_at(nsq)
                    if p and p.color == enemy:
                        blocked_count += 1
            if blocked_count >= 2:
                out.add("dovetail-mate")

    # Hook mate (rook + knight + pawn — heuristic, distinct from back-rank)
    if mating.piece_type == chess.ROOK and "back-rank-mate" not in out:
        has_knight = has_pawn = False
        for s in chess.SQUARES:
            q = board.piece_at(s)
            if q and q.color == mover_color:
                if q.piece_type == chess.KNIGHT:
                    has_knight = True
                elif q.piece_type == chess.PAWN:
                    has_pawn = True
        if has_knight and has_pawn:
            out.add("hook-mate")

    return out


def detect_themes(start_board: chess.Board, solution_uci: list[str],
                  eval_solver_cp: int) -> list[str]:
    """Detect tactical / mate-pattern / positional themes from the solution.

    Auto-detected (26 themes total):
      Basic:           mate, mate-in-N, check, capture, promotion, quiet,
                       sacrifice, endgame
      Tactical motifs: fork, double-attack, pin, skewer, discovered-attack,
                       x-ray, deflection, attraction, remove-defender,
                       trapped-piece
      Mate patterns:   back-rank-mate, smothered-mate, arabian-mate,
                       anastasia-mate, boden-mate, dovetail-mate, hook-mate
      Conditions:      advanced-pawn

    NOT auto-detected (manual tagging only): overloading, interference,
    zugzwang, zwischenzug, desperado.
    """
    themes: list[str] = []
    solver_color = start_board.turn
    initial_solver_material = material_count(start_board, solver_color)

    board = start_board.copy()

    has_check = has_capture = has_promotion = False
    has_quiet_first = has_sacrifice = has_advanced_pawn = False
    has_fork = has_pin = has_skewer = has_discovered = has_xray = False
    has_remove_defender = has_trapped_piece = False
    has_attraction = has_deflection = False

    solver_moves = 0
    for i, uci in enumerate(solution_uci):
        is_solver = (i % 2 == 0)
        if is_solver:
            solver_moves += 1
        m = chess.Move.from_uci(uci)
        moved_piece = board.piece_at(m.from_square)
        gives_check = board.gives_check(m)
        is_capture = board.is_capture(m)

        if gives_check:
            has_check = True
        if is_capture:
            has_capture = True
        if m.promotion:
            has_promotion = True

        # Quiet first move (often hardest puzzles)
        if is_solver and i == 0:
            if not gives_check and not is_capture and not m.promotion:
                has_quiet_first = True

        # Advanced pawn (6th/7th rank)
        if moved_piece and _is_advanced_pawn_move(moved_piece, m.to_square):
            has_advanced_pawn = True

        if is_solver:
            board_after = board.copy()
            board_after.push(m)

            if _detect_fork(board_after, m.to_square, solver_color):
                has_fork = True
            if _detect_discovered_attack(board, m, solver_color):
                has_discovered = True
            pin, skew, xr = _detect_pin_skewer_xray(board_after, solver_color)
            has_pin = has_pin or pin
            has_skewer = has_skewer or skew
            has_xray = has_xray or xr
            if _detect_remove_defender(board, m, solver_color):
                has_remove_defender = True
            if _detect_trapped_piece(board_after, solver_color):
                has_trapped_piece = True

            # Attraction/decoy: solver sacrifices on X, opponent forced to capture on X
            if i + 1 < len(solution_uci):
                opp_next = chess.Move.from_uci(solution_uci[i + 1])
                if (board_after.is_capture(opp_next)
                        and opp_next.to_square == m.to_square
                        and board_after.is_attacked_by(not solver_color, m.to_square)):
                    has_attraction = True
        else:
            # Opponent's forced response — if it abandons a defensive duty → deflection
            piece_pre = board.piece_at(m.from_square)
            if piece_pre:
                defended = list(board.attacks(m.from_square))
                board_after_opp = board.copy()
                board_after_opp.push(m)
                for sq in defended:
                    p = board.piece_at(sq)
                    if p and p.color == piece_pre.color and sq != m.from_square:
                        if not board_after_opp.attackers(piece_pre.color, sq):
                            has_deflection = True
                            break

        board.push(m)
        if material_count(board, solver_color) < initial_solver_material:
            has_sacrifice = True

    # Named mate patterns (only if final position is checkmate)
    mate_patterns: set[str] = set()
    if board.is_checkmate() and solution_uci:
        mate_patterns = _detect_mate_patterns(
            board, solver_color, chess.Move.from_uci(solution_uci[-1])
        )

    # Compose theme list
    if eval_solver_cp >= MATE_SCORE - 1000:
        themes.append("mate")
        themes.append(f"mate-in-{solver_moves}")
        themes.extend(sorted(mate_patterns))
    if has_check:           themes.append("check")
    if has_capture:         themes.append("capture")
    if has_promotion:       themes.append("promotion")
    if has_quiet_first:     themes.append("quiet")
    if has_sacrifice:       themes.append("sacrifice")
    if has_fork:
        themes.append("fork")
        themes.append("double-attack")
    if has_pin:             themes.append("pin")
    if has_skewer:          themes.append("skewer")
    if has_discovered:      themes.append("discovered-attack")
    if has_xray:            themes.append("x-ray")
    if has_advanced_pawn:   themes.append("advanced-pawn")
    if has_remove_defender: themes.append("remove-defender")
    if has_trapped_piece:   themes.append("trapped-piece")
    if has_attraction:      themes.append("attraction")
    if has_deflection:      themes.append("deflection")

    pieces = start_board.piece_map()
    if sum(1 for p in pieces.values() if p.piece_type not in (chess.KING, chess.PAWN)) <= 6:
        themes.append("endgame")
    return themes


def iter_puzzles(pgn_path: Path, engine: chess.engine.SimpleEngine,
                 depth: int, swing: int, multipv: int, min_ply: int,
                 mate_only: bool, max_per_game: int = 0,
                 quality_filter: bool = True,
                 min_level: int = 1) -> Iterator[Puzzle]:
    with pgn_path.open(encoding="utf-8", errors="replace") as fh:
        yield from iter_puzzles_stream(fh, pgn_path.stem, engine,
                                       depth, swing, multipv, min_ply, mate_only,
                                       max_per_game, quality_filter, min_level)


def iter_puzzles_stream(fh, source_name: str, engine: chess.engine.SimpleEngine,
                        depth: int, swing: int, multipv: int, min_ply: int,
                        mate_only: bool, max_per_game: int = 0,
                        quality_filter: bool = True,
                        min_level: int = 1) -> Iterator[Puzzle]:
    """Generate puzzles. If max_per_game > 0, only the HARDEST `max_per_game` puzzles
    per game are kept (sorted by level desc, then eval-swing desc as tiebreak).
    `min_level` filters out puzzles below the given difficulty."""
    game_idx = 0
    while True:
        game = chess.pgn.read_game(fh)
        if game is None:
            return
        game_idx += 1
        headers = game.headers
        game_id = f"{source_name}#{game_idx}"

        board = game.board()
        prev_infos: list[chess.engine.InfoDict] | None = None
        game_puzzles: list[Puzzle] = []

        for move in game.mainline_moves():
            ply = board.ply()
            mover = board.turn

            if prev_infos is None:
                prev_infos = analyse(engine, board, depth, multipv)
            eval_before = score_cp(info_score(prev_infos[0]), mover)

            board.push(move)

            if board.is_game_over() or ply < min_ply:
                prev_infos = None
                continue

            infos_after = analyse(engine, board, depth, multipv)
            eval_after = score_cp(info_score(infos_after[0]), mover)
            drop = eval_before - eval_after

            is_blunder = drop >= swing
            is_mate_puzzle = eval_after <= -(MATE_SCORE - 1000)
            if mate_only and not is_mate_puzzle:
                is_blunder = False

            # Quality filters: skip puzzles from already-losing positions,
            # blunders that don't actually result in a clearly losing position,
            # and trivial K-vs-K-with-stuff endings (mating the lone king is repetitive).
            if quality_filter and is_blunder:
                if eval_before < QUALITY_EVAL_BEFORE_MIN:
                    is_blunder = False
                elif eval_after > QUALITY_EVAL_AFTER_MAX:
                    is_blunder = False
                elif is_trivial_endgame(board):
                    is_blunder = False

            if is_blunder:
                puzzle = build_puzzle(
                    engine, board.copy(), headers, game_id, ply,
                    move, eval_before, eval_after, depth, swing, multipv,
                )
                if puzzle is not None:
                    game_puzzles.append(puzzle)

            prev_infos = infos_after

        # End of game: pick top N by difficulty, then apply min_level filter
        if game_puzzles:
            game_puzzles.sort(
                key=lambda p: (-p.level, -abs(p.eval_before_cp - p.eval_after_cp))
            )
            if max_per_game:
                game_puzzles = game_puzzles[:max_per_game]
            for pz in game_puzzles:
                if pz.level >= min_level:
                    yield pz


def build_puzzle(engine: chess.engine.SimpleEngine, board: chess.Board,
                 headers, game_id: str, ply: int, blunder: chess.Move,
                 eval_before: int, eval_after: int,
                 depth: int, swing: int, multipv: int) -> Puzzle | None:
    solution_uci: list[str] = []
    solution_san: list[str] = []
    solver = board.turn
    start_fen = board.fen()

    infos = analyse(engine, board, depth, multipv)
    if not is_forced_continuation(infos, board.turn, swing):
        return None

    while not board.is_game_over():
        pv = infos[0].get("pv") or []
        if not pv:
            break
        next_move = pv[0]
        solution_san.append(board.san(next_move))
        solution_uci.append(next_move.uci())
        board.push(next_move)
        if len(solution_uci) >= 12 or board.is_game_over():
            break
        infos = analyse(engine, board, depth, multipv)
        if board.turn == solver and not is_forced_continuation(infos, board.turn, swing):
            break

    if not solution_uci:
        return None

    start_board = chess.Board(start_fen)
    themes = detect_themes(start_board, solution_uci, -eval_after)
    level = assign_level(solution_uci, eval_before, eval_after, themes)

    # ---- Pointer-back metadata: parse names + URL fields from PGN headers ----
    raw_white = headers.get("White", "?")
    raw_black = headers.get("Black", "?")
    white_name, white_team = parse_player_team(raw_white)
    black_name, black_team = parse_player_team(raw_black)
    site = headers.get("Site", "")
    eco = headers.get("ECO", "")
    opening_name = headers.get("Opening", "")
    opening = (f"{eco} — {opening_name}" if eco and opening_name
               else opening_name or eco or "")

    return Puzzle(
        game_id=game_id,
        white=white_name or raw_white,
        black=black_name or raw_black,
        event=headers.get("Event", "?"),
        ply=ply + 1,
        fen=start_board.fen(),
        blunder_move=blunder.uci(),
        eval_before_cp=eval_before,
        eval_after_cp=eval_after,
        solution_uci=solution_uci,
        solution_san=solution_san,
        themes=themes,
        level=level,
        site=site,
        date=headers.get("Date", ""),
        round=headers.get("Round", ""),
        opening=opening,
        time_control=headers.get("TimeControl", ""),
        white_team=white_team,
        black_team=black_team,
        source_platform=detect_platform(site),
    )



def default_engine_path() -> str:
    here = Path(__file__).resolve().parent
    for cand in (here / "engine" / "stockfish.exe",
                 here / "engine" / "stockfish",
                 here / "stockfish.exe",
                 here / "stockfish"):
        if cand.is_file():
            return str(cand)
    return "stockfish"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pgn", type=Path, help="Input PGN file")
    p.add_argument("-o", "--output", type=Path, default=Path("puzzles.json"))
    p.add_argument("--engine", default=default_engine_path(),
                   help="Path to Stockfish binary (default: ./engine/stockfish[.exe] next to this script, then PATH)")
    p.add_argument("--depth", type=int, default=18)
    p.add_argument("--swing", type=int, default=200,
                   help="Min centipawn swing to flag a blunder (default 200 = 2 pawns)")
    p.add_argument("--multipv", type=int, default=2)
    p.add_argument("--min-ply", type=int, default=8,
                   help="Skip the opening (plies before this index)")
    p.add_argument("--mate-only", action="store_true",
                   help="Only emit puzzles where the side-to-move can force mate")
    p.add_argument("--max-puzzles", type=int, default=0,
                   help="Stop after N puzzles (0 = no limit)")
    p.add_argument("--max-per-game", type=int, default=3,
                   help="Max puzzles per game (default 3, 0 = no limit). "
                        "Keeps the HARDEST puzzles by level then eval-swing.")
    p.add_argument("--min-level", type=int, default=1, choices=[1, 2, 3, 4, 5],
                   help="Only emit puzzles with level >= N (default 1)")
    p.add_argument("--no-quality-filter", action="store_true",
                   help="Disable quality filters (eval_before/eval_after thresholds)")
    p.add_argument("--csv", action="store_true", help="Emit CSV instead of JSON")
    p.add_argument("--threads", type=int, default=1,
                   help="Stockfish CPU threads (default 1 = lowest CPU usage)")
    p.add_argument("--hash", dest="hash_mb", type=int, default=16,
                   help="Stockfish hash size in MB (default 16)")
    args = p.parse_args()

    if not args.pgn.is_file():
        print(f"PGN not found: {args.pgn}", file=sys.stderr)
        return 2

    try:
        engine = chess.engine.SimpleEngine.popen_uci(args.engine)
    except FileNotFoundError:
        print(f"Stockfish not found at '{args.engine}'. Install it or pass --engine PATH.",
              file=sys.stderr)
        return 2

    try:
        engine.configure({"Threads": args.threads, "Hash": args.hash_mb})
        print(f"Engine throttle: Threads={args.threads}, Hash={args.hash_mb}MB",
              file=sys.stderr)
    except chess.engine.EngineError:
        pass

    puzzles: list[Puzzle] = []
    try:
        for pz in iter_puzzles(args.pgn, engine, args.depth, args.swing,
                               args.multipv, args.min_ply, args.mate_only,
                               max_per_game=args.max_per_game,
                               quality_filter=not args.no_quality_filter,
                               min_level=args.min_level):
            puzzles.append(pz)
            print(f"[{len(puzzles):4d}] L{pz.level} {pz.game_id} ply {pz.ply}  "
                  f"{pz.blunder_move} -> {' '.join(pz.solution_san)}",
                  file=sys.stderr)
            if args.max_puzzles and len(puzzles) >= args.max_puzzles:
                break
    finally:
        engine.quit()

    if args.csv:
        import csv
        with args.output.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["game_id", "white", "black", "event", "ply", "fen",
                        "blunder", "eval_before", "eval_after",
                        "solution_uci", "solution_san", "themes", "level"])
            for pz in puzzles:
                w.writerow([pz.game_id, pz.white, pz.black, pz.event, pz.ply,
                            pz.fen, pz.blunder_move, pz.eval_before_cp,
                            pz.eval_after_cp, " ".join(pz.solution_uci),
                            " ".join(pz.solution_san), " ".join(pz.themes),
                            pz.level])
    else:
        args.output.write_text(
            json.dumps([asdict(pz) for pz in puzzles], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(f"Wrote {len(puzzles)} puzzles -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
