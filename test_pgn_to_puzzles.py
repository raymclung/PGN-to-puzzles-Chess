"""Tests for the pure logic in pgn_to_puzzles.

Everything here runs without Stockfish. The engine-facing code needs a real
binary and takes seconds per position, so it is left out on purpose — these
tests cover the parts that decide what a puzzle *is*: difficulty bucketing,
theme detection, and the filters that throw poor candidates away.

    python -m unittest discover -v
"""
from __future__ import annotations

import unittest

import chess

import pgn_to_puzzles as gen


class AssignLevel(unittest.TestCase):
    """Difficulty is driven by the eval landscape and brilliance markers,
    never by how long the solution happens to be."""

    def level(self, solution, before, after, themes):
        return gen.assign_level(solution, before, after, themes)

    def test_mate_in_one_is_always_easiest(self):
        # Only one move to find, no calculation — level 1 regardless of swing.
        for swing in (100, 900, 5000):
            self.assertEqual(
                self.level(["d8h4"], 0, -swing, ["mate-in-1", "check"]), 1,
                f"mate-in-1 should stay level 1 at swing {swing}")

    def test_short_solution_can_still_be_hard(self):
        # A quiet first move is hard even when the line is two ply long.
        quiet = self.level(["a1a7"], 20, -400, ["quiet"])
        loud = self.level(["a1a7"], 20, -400, ["capture"])
        self.assertGreater(quiet, loud,
                           "a quiet move should outrank a plain capture")

    def test_long_solution_is_not_automatically_hard(self):
        # Six ply of obvious checks must not beat a two-ply quiet move.
        long_obvious = self.level(["a1a2", "b1b2", "c1c2", "d1d2", "e1e2", "f1f2"],
                                  20, -400, ["check", "capture"])
        short_quiet = self.level(["a1a2"], 20, -400, ["quiet"])
        self.assertLessEqual(long_obvious, short_quiet + 1,
                             "solution length alone should not drive difficulty")

    def test_obvious_blunder_is_easy(self):
        # An 800+ centipawn swing on a single move is a free piece.
        self.assertEqual(self.level(["a1a2"], 100, -800, ["capture"]), 1)

    def test_brilliancy_markers_reach_the_top_level(self):
        self.assertEqual(
            self.level(["a1a2", "b1b2", "c1c2", "d1d2"], 20, -600,
                       ["quiet", "sacrifice", "smothered-mate"]), 5)

    def test_level_always_within_range(self):
        cases = [
            ([], 0, 0, []),
            (["a1a2"], 0, 0, []),
            (["a1a2"] * 12, 5000, -5000, ["quiet", "sacrifice", "check", "capture"]),
            (["a1a2"] * 3, -300, -900, ["fork"]),
        ]
        for solution, before, after, themes in cases:
            with self.subTest(solution=len(solution), themes=themes):
                lv = self.level(solution, before, after, themes)
                self.assertIn(lv, (1, 2, 3, 4, 5))


class MaterialCount(unittest.TestCase):
    def test_starting_position_is_balanced(self):
        b = chess.Board()
        self.assertEqual(gen.material_count(b, chess.WHITE),
                         gen.material_count(b, chess.BLACK))

    def test_missing_queen_lowers_the_count(self):
        full = chess.Board()
        no_queen = chess.Board("rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        self.assertLess(gen.material_count(no_queen, chess.BLACK),
                        gen.material_count(full, chess.BLACK))
        # White is untouched.
        self.assertEqual(gen.material_count(no_queen, chess.WHITE),
                         gen.material_count(full, chess.WHITE))

    def test_bare_king_counts_nothing(self):
        b = chess.Board("4k3/8/8/8/8/8/8/4K2R w K - 0 1")
        self.assertEqual(gen.material_count(b, chess.BLACK), 0)
        self.assertGreater(gen.material_count(b, chess.WHITE), 0)


class TrivialEndgame(unittest.TestCase):
    """Positions where one side is down to a bare king produce repetitive
    'mate the lone king' puzzles, so they are filtered out."""

    def test_lone_king_is_trivial(self):
        self.assertTrue(gen.is_trivial_endgame(
            chess.Board("4k3/8/8/8/8/8/8/3QK3 w - - 0 1")))

    def test_starting_position_is_not_trivial(self):
        self.assertFalse(gen.is_trivial_endgame(chess.Board()))

    def test_two_pieces_each_is_not_trivial(self):
        self.assertFalse(gen.is_trivial_endgame(
            chess.Board("4k2r/8/8/8/8/8/8/4K2R w Kk - 0 1")))


class DetectThemes(unittest.TestCase):
    def themes(self, fen, moves, solver_cp=500):
        return gen.detect_themes(chess.Board(fen), moves, solver_cp)

    def test_capture_is_tagged(self):
        # White queen takes the undefended rook on h5.
        t = self.themes("4k3/8/8/7r/8/8/8/3QK3 w - - 0 1", ["d1h5"])
        self.assertIn("capture", t)

    def test_check_is_tagged(self):
        t = self.themes("4k3/8/8/8/8/8/8/3QK3 w - - 0 1", ["d1d8"])
        self.assertIn("check", t)

    def test_mate_in_one_is_tagged(self):
        # Back-rank mate: the king on g8 is walled in by its own f7/g7/h7
        # pawns, so Ra8 covers the whole eighth rank with no escape.
        fen = "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1"
        board = chess.Board(fen)
        board.push(chess.Move.from_uci("a1a8"))
        self.assertTrue(board.is_checkmate(),
                        "test fixture must really be mate, not just check")

        # mate-in-N is only tagged when the engine score says "mate", not when
        # the board merely happens to be mate — so pass a mate score here.
        t = self.themes(fen, ["a1a8"], solver_cp=gen.MATE_SCORE)
        self.assertIn("mate-in-1", t)

    def test_mate_score_required_for_mate_theme(self):
        """A checkmating move scored as an ordinary advantage is not tagged
        mate-in-N. The tag reflects the engine's verdict, not the board."""
        fen = "6k1/5ppp/8/8/8/8/8/R3K3 w Q - 0 1"
        t = self.themes(fen, ["a1a8"], solver_cp=500)
        self.assertNotIn("mate-in-1", t)
        self.assertIn("check", t)

    def test_quiet_move_has_no_capture_or_check(self):
        t = self.themes("4k3/8/8/8/8/8/8/3QK3 w - - 0 1", ["d1d4"])
        self.assertNotIn("capture", t)
        self.assertNotIn("check", t)

    def test_returns_a_list_of_strings(self):
        t = self.themes("4k3/8/8/7r/8/8/8/3QK3 w - - 0 1", ["d1h5"])
        self.assertIsInstance(t, list)
        for name in t:
            self.assertIsInstance(name, str)
            self.assertTrue(name, "theme names must not be empty")


class EnginePath(unittest.TestCase):
    def test_falls_back_to_bare_name(self):
        """With no bundled binary the caller can still rely on PATH."""
        path = gen.default_engine_path()
        self.assertTrue(path, "engine path must never be empty")
        self.assertIsInstance(path, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
