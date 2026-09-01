"""Sample run: ambil beberapa game pertama dari tiap PGN di pgns/, lalu tulis ke library.json."""
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
SAMPLE_DIR = HERE / "_sample"
ENGINE_PATH = HERE / "engine" / "stockfish.exe"
LIBRARY = HERE / "library.json"

GAMES_PER_FILE = 10
SOURCES = [(p.name, GAMES_PER_FILE) for p in sorted(PGN_DIR.glob("*.pgn"))]
DEPTH = 12
SWING = 250
MULTIPV = 2
MIN_PLY = 10
MAX_PER_GAME = 3
QUALITY_FILTER = True
MIN_LEVEL = 1   # raise to 4 or 5 to only collect hard puzzles


def build_sample(fname: str, n: int) -> Path:
    """Copy first n games from PGN_DIR/fname into SAMPLE_DIR/fname (same name → clean game_id)."""
    src = PGN_DIR / fname
    dst = SAMPLE_DIR / fname
    chunks: list[str] = []
    with src.open(encoding="utf-8", errors="replace") as fh:
        while len(chunks) < n:
            g = chess.pgn.read_game(fh)
            if g is None:
                break
            exp = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
            chunks.append(g.accept(exp))
    dst.write_text("\n\n".join(chunks) + "\n", encoding="utf-8")
    return dst


def merge_into_library(new_items: list[dict]) -> tuple[int, int]:
    """Append puzzles to library.json with dedup by (fen, first_solution_move)."""
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
    SAMPLE_DIR.mkdir(exist_ok=True)
    print(f"=== Sample run: {sum(n for _, n in SOURCES)} games total ===", file=sys.stderr)
    print(f"Config: depth={DEPTH} swing={SWING} multipv={MULTIPV} min_ply={MIN_PLY} "
          f"max_per_game={MAX_PER_GAME} quality_filter={QUALITY_FILTER}", file=sys.stderr)
    print(f"Engine: {ENGINE_PATH}", file=sys.stderr)
    if not ENGINE_PATH.is_file():
        print(f"ERROR: Stockfish not found at {ENGINE_PATH}", file=sys.stderr)
        return 2

    sample_paths: list[Path] = []
    for fname, n in SOURCES:
        p = build_sample(fname, n)
        print(f"  sampled -> {p.name} ({p.stat().st_size:,} bytes)", file=sys.stderr)
        sample_paths.append(p)

    print(f"\nStarting Stockfish...", file=sys.stderr)
    engine = chess.engine.SimpleEngine.popen_uci(str(ENGINE_PATH))

    all_puzzles: list[dict] = []
    t0 = time.time()
    try:
        for sp in sample_paths:
            print(f"\n--- Analyzing {sp.name} ---", file=sys.stderr)
            t_file = time.time()
            count_before = len(all_puzzles)
            for pz in gen.iter_puzzles(sp, engine, DEPTH, SWING, MULTIPV, MIN_PLY,
                                       False, max_per_game=MAX_PER_GAME,
                                       quality_filter=QUALITY_FILTER,
                                       min_level=MIN_LEVEL):
                d = asdict(pz)
                all_puzzles.append(d)
                elapsed = time.time() - t0
                sol_preview = " ".join(pz.solution_san[:4])
                if len(pz.solution_san) > 4:
                    sol_preview += "..."
                # Highlight "brilliant" markers in the inline log
                bright = []
                if "quiet" in pz.themes: bright.append("quiet")
                if "sacrifice" in pz.themes: bright.append("sac")
                if any(t.startswith("mate-in-") for t in pz.themes):
                    mt = next(t for t in pz.themes if t.startswith("mate-in-"))
                    bright.append(mt[5:])  # e.g. "in-3"
                tags = (" [" + ",".join(bright) + "]") if bright else ""
                print(
                    f"[{len(all_puzzles):3d}] ({elapsed:6.1f}s) L{pz.level}{tags} "
                    f"{pz.game_id} ply {pz.ply}: {pz.blunder_move} -> {sol_preview}",
                    file=sys.stderr,
                )
            dt = time.time() - t_file
            new_in_file = len(all_puzzles) - count_before
            print(f"--- {sp.name}: {new_in_file} puzzles in {dt:.1f}s ---", file=sys.stderr)
    finally:
        engine.quit()

    elapsed = time.time() - t0
    print(f"\n=== Engine done in {elapsed:.1f}s ===", file=sys.stderr)

    added, total = merge_into_library(all_puzzles)
    print(f"Puzzles found: {len(all_puzzles)}", file=sys.stderr)
    print(f"New (after dedup): {added}", file=sys.stderr)
    print(f"Library total: {total}", file=sys.stderr)

    # Level distribution (across new puzzles found in this run)
    if all_puzzles:
        level_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for p in all_puzzles:
            level_counts[p["level"]] = level_counts.get(p["level"], 0) + 1
        labels = {1: "Mudah sekali", 2: "Mudah", 3: "Normal",
                  4: "Sulit", 5: "Sangat sulit"}
        print("\n=== Distribusi level ===", file=sys.stderr)
        for lv in (1, 2, 3, 4, 5):
            n = level_counts.get(lv, 0)
            bar = "█" * n
            print(f"  L{lv} ({labels[lv]:14s}): {n:3d}  {bar}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
