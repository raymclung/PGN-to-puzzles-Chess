"""Full run: ALL games from all 3 PGN files. Append to library.json with dedup.

Estimated time: ~60-70 minutes at depth 12 (1,367 games).
Run in background — output piped to stderr.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import chess
import chess.engine
import chess.pgn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pgn_to_puzzles as gen  # noqa: E402

PGN_DIR = HERE / "pgns"
ENGINE_PATH = HERE / "engine" / "stockfish.exe"
LIBRARY = HERE / "library.json"

SOURCES = [p.name for p in sorted(PGN_DIR.glob("*.pgn"))]

# Same config as the validated sample run
DEPTH = 12
SWING = 250
MULTIPV = 2
MIN_PLY = 10
MAX_PER_GAME = 3
QUALITY_FILTER = True
MIN_LEVEL = 1


def merge_into_library(new_items: list[dict]) -> tuple[int, int]:
    items = json.loads(LIBRARY.read_text(encoding="utf-8")) if LIBRARY.is_file() else []
    seen = {(p["fen"], (p["solution_uci"] or [""])[0]) for p in items}
    added = 0
    for p in new_items:
        key = (p["fen"], (p["solution_uci"] or [""])[0])
        if key not in seen:
            items.append(p)
            seen.add(key)
            added += 1
    LIBRARY.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return added, len(items)


def main() -> int:
    if not ENGINE_PATH.is_file():
        print(f"ERROR: Stockfish not found at {ENGINE_PATH}", file=sys.stderr)
        return 2

    print(f"=== FULL RUN: {len(SOURCES)} PGN files ===", file=sys.stderr)
    print(f"Config: depth={DEPTH} swing={SWING} multipv={MULTIPV} min_ply={MIN_PLY} "
          f"max_per_game={MAX_PER_GAME} quality_filter={QUALITY_FILTER} min_level={MIN_LEVEL}",
          file=sys.stderr)

    engine = chess.engine.SimpleEngine.popen_uci(str(ENGINE_PATH))

    grand_total: list[dict] = []
    t0 = time.time()
    try:
        for fname in SOURCES:
            sp = PGN_DIR / fname
            print(f"\n--- Analyzing {fname} ---", file=sys.stderr)
            t_file = time.time()
            file_count = 0
            for pz in gen.iter_puzzles(sp, engine, DEPTH, SWING, MULTIPV, MIN_PLY,
                                       False, max_per_game=MAX_PER_GAME,
                                       quality_filter=QUALITY_FILTER,
                                       min_level=MIN_LEVEL):
                d = asdict(pz)
                grand_total.append(d)
                file_count += 1
                # Periodic progress (every 25 puzzles to keep log readable)
                if file_count % 25 == 0:
                    elapsed = time.time() - t0
                    bright = []
                    if "quiet" in pz.themes: bright.append("quiet")
                    if "sacrifice" in pz.themes: bright.append("sac")
                    tags = (" [" + ",".join(bright) + "]") if bright else ""
                    print(
                        f"  ...[{len(grand_total):4d}] ({elapsed/60:5.1f}min) "
                        f"L{pz.level}{tags} {pz.game_id} ply {pz.ply}",
                        file=sys.stderr,
                    )
            dt_file = time.time() - t_file
            print(f"--- {fname}: {file_count} puzzles in {dt_file/60:.1f}min ---",
                  file=sys.stderr)
    finally:
        engine.quit()

    elapsed = time.time() - t0
    print(f"\n=== Engine done in {elapsed/60:.1f} min ({elapsed:.0f}s) ===",
          file=sys.stderr)

    added, total = merge_into_library(grand_total)
    print(f"Puzzles found: {len(grand_total)}", file=sys.stderr)
    print(f"New (after dedup): {added}", file=sys.stderr)
    print(f"Library total: {total}", file=sys.stderr)

    if grand_total:
        level_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for p in grand_total:
            level_counts[p["level"]] = level_counts.get(p["level"], 0) + 1
        labels = {1: "Mudah sekali", 2: "Mudah", 3: "Normal",
                  4: "Sulit", 5: "Sangat sulit"}
        print("\n=== Distribusi level (puzzle baru run ini) ===", file=sys.stderr)
        for lv in (1, 2, 3, 4, 5):
            n = level_counts.get(lv, 0)
            bar = "#" * min(n, 60)
            print(f"  L{lv} ({labels[lv]:14s}): {n:4d}  {bar}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
