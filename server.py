"""Chess analyzer + puzzle library server.

Endpoints:
    GET  /                          -> index.html
    GET  /<file>                    -> static file in puzzle/
    GET  /puzzles/library           -> all saved puzzles
    POST /pgn/parse                 (body=PGN text) -> [{idx, headers, moves[]}]
    POST /eval                      (json {fen, depth}) -> {cp, best_uci, pv_san[]}
    POST /game/analyze              (json {pgn, game_idx, depth}) -> {evals[]}
    POST /puzzles/generate?...      (body=PGN text) -> {added, total, puzzles[]}
        query: depth, swing, multipv, min_ply, max, mate_only, game_idx
    POST /puzzles/library/clear     -> {cleared: true}

Run:
    py puzzle\\server.py            # listens on http://127.0.0.1:8000
    py puzzle\\server.py 9000       # custom port
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chess
import chess.engine
import chess.pgn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pgn_to_puzzles as gen  # noqa: E402


LIBRARY_FILE = HERE / "library.json"
_lib_lock = threading.Lock()

# Rate-limit buckets for /puzzles/generate — keyed by identity ("u:<id>" or "ip:<addr>")
_generate_buckets: dict = {}

# CPU/memory throttle for Stockfish. Override via environment variables:
#   STOCKFISH_THREADS  (default 1) — number of CPU cores Stockfish may use
#   STOCKFISH_HASH_MB  (default 16) — transposition-table size in MB
ENGINE_THREADS = int(os.environ.get("STOCKFISH_THREADS", "1"))
ENGINE_HASH_MB = int(os.environ.get("STOCKFISH_HASH_MB", "16"))


def launch_engine() -> chess.engine.SimpleEngine:
    """Open a fresh Stockfish process with throttled CPU/memory."""
    eng = chess.engine.SimpleEngine.popen_uci(gen.default_engine_path())
    try:
        eng.configure({"Threads": ENGINE_THREADS, "Hash": ENGINE_HASH_MB})
    except chess.engine.EngineError:
        pass  # engine doesn't support these options — proceed with defaults
    return eng


# ----------------------------------------------------------------------
# Persistent engine + LRU cache for /eval (interactive snappy responses).
# Long-running endpoints (/game/analyze, /puzzles/generate) still spawn
# their own engine to avoid blocking interactive use.
# ----------------------------------------------------------------------
_eval_engine: chess.engine.SimpleEngine | None = None
_eval_engine_lock = threading.Lock()
_eval_cache: dict = {}     # (fen, depth, multipv) -> response_dict
_EVAL_CACHE_MAX = 256


def _get_eval_engine() -> chess.engine.SimpleEngine:
    global _eval_engine
    if _eval_engine is None:
        _eval_engine = launch_engine()
    return _eval_engine


def _restart_eval_engine() -> None:
    global _eval_engine
    if _eval_engine is not None:
        try:
            _eval_engine.quit()
        except Exception:
            pass
        _eval_engine = None
    _eval_engine = launch_engine()


def _eval_with_retry(board: chess.Board, depth: int, multipv: int,
                     retries: int = 2):
    """Run engine.analyse with auto-restart on engine failure.
    Catches Stockfish process death (terminated/broken pipe/protocol errors)
    and respawns the persistent engine before retrying. Caps at `retries`
    restarts to avoid infinite loops if engine binary is fundamentally broken."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with _eval_engine_lock:
                return _get_eval_engine().analyse(
                    board, chess.engine.Limit(depth=depth), multipv=multipv)
        except (chess.engine.EngineTerminatedError,
                chess.engine.EngineError,
                BrokenPipeError, ConnectionError, OSError) as e:
            last_err = e
            sys.stderr.write(f"[eval] engine error attempt {attempt + 1}/"
                             f"{retries + 1}: {type(e).__name__}: {e}\n")
            try:
                with _eval_engine_lock:
                    _restart_eval_engine()
            except Exception as restart_err:
                sys.stderr.write(f"[eval] engine restart failed: {restart_err}\n")
                # If we can't restart, give up
                break
    raise RuntimeError(f"Eval engine failed after {retries + 1} attempts: "
                       f"{type(last_err).__name__}: {last_err}")


def _eval_cache_put(key, value) -> None:
    if len(_eval_cache) >= _EVAL_CACHE_MAX:
        # Drop oldest insertion (rough LRU; dict preserves insertion order)
        first_key = next(iter(_eval_cache))
        _eval_cache.pop(first_key, None)
    _eval_cache[key] = value


def load_library() -> list[dict]:
    if not LIBRARY_FILE.is_file():
        return []
    try:
        return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_library(items: list[dict]) -> None:
    LIBRARY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def append_to_library(new_items: list[dict]) -> int:
    """Append puzzles to library, dedup by (fen, first solution move). Returns count added."""
    with _lib_lock:
        items = load_library()
        existing = {(it.get("fen", ""), (it.get("solution_uci") or [""])[0]) for it in items}
        added = 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for it in new_items:
            key = (it.get("fen", ""), (it.get("solution_uci") or [""])[0])
            if key in existing:
                continue
            it = dict(it)
            it["id"] = f"{key[0][:30]}|{key[1]}"
            it["added_at"] = now
            items.append(it)
            existing.add(key)
            added += 1
        save_library(items)
        return added


def read_specific_game(pgn_text: str, game_idx: int) -> chess.pgn.Game | None:
    stream = io.StringIO(pgn_text)
    g = None
    for _ in range(game_idx + 1):
        g = chess.pgn.read_game(stream)
        if g is None:
            return None
    return g


def game_to_pgn_string(game: chess.pgn.Game) -> str:
    out = io.StringIO()
    exporter = chess.pgn.FileExporter(out)
    game.accept(exporter)
    return out.getvalue()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n > 0 else b""

    def _read_json(self) -> dict:
        raw = self._read_body().decode("utf-8") or "{}"
        return json.loads(raw)

    def _send_json(self, data, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Force no-cache on ALL responses (HTML/JS/CSS/JSON/images) so browser
        # always pulls fresh code during dev. Avoids "stale JS" debugging hell.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # CORS: let other local pages call /eval for engine analysis.
        # Wildcard is fine here because this server is meant for local use.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        # CORS preflight: respond 204 with the allow-headers above.
        self.send_response(204)
        self.end_headers()

    def _is_local(self) -> bool:
        """Destructive routes are limited to loopback callers."""
        ip = self.client_address[0] if self.client_address else ""
        if ip in ("127.0.0.1", "::1", "localhost"):
            return True
        self._send_json({"error": "Hanya dapat dipanggil dari localhost"}, code=403)
        return False

    def _check_generate_rate_limit(self) -> bool:
        """Per-identity rate limit for /puzzles/generate.
        Three per hour per client IP. Loopback callers are exempt.
        Returns True if allowed, sends 429 if not."""
        import time
        if True:
            ip = self.client_address[0] if self.client_address else "unknown"
            # Loopback addresses are dev-only — exempt from rate limit
            if ip in ("127.0.0.1", "::1", "localhost"):
                return True
            identity = f"ip:{ip}"
            limit = 3
        else:
            limit = 10
        window = 3600  # 1 hour
        now = time.time()
        bucket = _generate_buckets.setdefault(identity, [])
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            wait_min = int((window - (now - bucket[0])) / 60) + 1
            self._send_json({
                "error": f"Terlalu banyak generate ({len(bucket)}/{limit}). "
                         f"Coba lagi {wait_min} menit lagi.",
            }, code=429)
            return False
        bucket.append(now)
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/puzzles/library":
            self._send_json(load_library())
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/pgn/parse":
                self._handle_parse()
            elif path == "/eval":
                self._handle_eval()
            elif path == "/game/analyze":
                self._handle_game_analyze()
            elif path == "/puzzles/generate":
                # Open to everyone, but rate-limited to prevent abuse.
                if not self._check_generate_rate_limit():
                    return
                self._handle_generate()
            elif path == "/puzzles/library/clear":
                if not self._is_local():
                    return
                self._handle_clear()
            else:
                self.send_error(404, f"Unknown endpoint: {path}")
        except Exception as e:
            sys.stderr.write(f"ERROR {path}: {e}\n")
            self.send_error(500, str(e))

    def _handle_parse(self):
        text = self._read_body().decode("utf-8", errors="replace")
        if not text.strip():
            self.send_error(400, "Empty PGN")
            return
        games = []
        stream = io.StringIO(text)
        idx = 0
        while True:
            game = chess.pgn.read_game(stream)
            if game is None:
                break
            board = game.board()
            start_fen = board.fen()
            moves = []
            for mv in game.mainline_moves():
                san = board.san(mv)
                uci = mv.uci()
                board.push(mv)
                moves.append({"san": san, "uci": uci, "fen_after": board.fen()})
            games.append({
                "idx": idx,
                "headers": dict(game.headers),
                "start_fen": start_fen,
                "moves": moves,
            })
            idx += 1
        self._send_json(games)

    def _handle_eval(self):
        data = self._read_json()
        fen = data["fen"]
        depth = int(data.get("depth", 14))
        multipv = max(1, min(8, int(data.get("multipv", 1))))

        # Cache fast-path: same (fen, depth, multipv) returns instantly
        cache_key = (fen, depth, multipv)
        cached = _eval_cache.get(cache_key)
        if cached is not None:
            self._send_json(cached)
            return

        board = chess.Board(fen)
        # Auto-retry with engine respawn on Stockfish process death
        try:
            result = _eval_with_retry(board, depth, multipv)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, code=503)
            return
        infos = result if isinstance(result, list) else [result]

        lines: list[dict] = []
        for info in infos:
            cp = gen.score_cp(gen.info_score(info), chess.WHITE)
            pv = info.get("pv") or []
            pv_uci = [mv.uci() for mv in pv[:12]]
            pv_san: list[str] = []
            b = chess.Board(fen)
            for mv in pv[:12]:
                try:
                    pv_san.append(b.san(mv))
                    b.push(mv)
                except Exception:
                    break
            lines.append({"cp": cp, "pv_uci": pv_uci, "pv_san": pv_san})

        top = lines[0] if lines else {"cp": 0, "pv_uci": [], "pv_san": []}
        response = {
            "cp": top["cp"],
            "best_uci": top["pv_uci"][0] if top["pv_uci"] else None,
            "pv_san": top["pv_san"],
            "lines": lines,
        }
        _eval_cache_put(cache_key, response)
        self._send_json(response)

    def _handle_game_analyze(self):
        data = self._read_json()
        pgn_text = data.get("pgn", "")
        game_idx = int(data.get("game_idx", 0))
        depth = int(data.get("depth", 12))
        game = read_specific_game(pgn_text, game_idx)
        if game is None:
            self.send_error(400, f"Game index {game_idx} not found")
            return
        engine = launch_engine()
        evals = []
        try:
            board = game.board()
            info = engine.analyse(board, chess.engine.Limit(depth=depth))
            evals.append({"ply": 0, "cp": gen.score_cp(gen.info_score(info), chess.WHITE)})
            for mv in game.mainline_moves():
                board.push(mv)
                info = engine.analyse(board, chess.engine.Limit(depth=depth))
                evals.append({
                    "ply": board.ply(),
                    "cp": gen.score_cp(gen.info_score(info), chess.WHITE),
                })
        finally:
            engine.quit()
        self._send_json({"evals": evals})

    def _handle_generate(self):
        params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        depth = int(params.get("depth", 12))
        swing = int(params.get("swing", 200))
        multipv = int(params.get("multipv", 2))
        min_ply = int(params.get("min_ply", 8))
        max_p = int(params.get("max", 0))
        mate_only = params.get("mate_only", "0") in ("1", "true", "yes")
        game_idx = int(params.get("game_idx", -1))

        text = self._read_body().decode("utf-8", errors="replace")
        if not text.strip():
            self.send_error(400, "Empty PGN")
            return

        # Optional `name` query param sets the source label in game_id.
        # Falls back to "game{idx}" or "paste".
        custom_name = params.get("name", "")
        if game_idx >= 0:
            g = read_specific_game(text, game_idx)
            if g is None:
                self.send_error(400, f"Game index {game_idx} not found")
                return
            text = game_to_pgn_string(g)
            source_name = custom_name or f"game{game_idx}"
        else:
            source_name = custom_name or "paste"

        # Incremental save: flush every N puzzles found so a server crash
        # mid-generation doesn't lose hours of work. Default 10 = ~5s overhead
        # per 100 puzzles for SQLite-free JSON write.
        INCREMENTAL_BATCH = 10
        engine = launch_engine()
        new_puzzles: list[dict] = []   # full list for response
        batch: list[dict] = []          # rolling buffer flushed every BATCH
        total_added = 0
        try:
            stream = io.StringIO(text)
            for pz in gen.iter_puzzles_stream(stream, source_name, engine,
                                              depth, swing, multipv,
                                              min_ply, mate_only):
                d = asdict(pz)
                new_puzzles.append(d)
                batch.append(d)
                sys.stderr.write(f"  [{len(new_puzzles):3d}] {pz.game_id} ply {pz.ply}  "
                                 f"{pz.blunder_move}\n")
                if len(batch) >= INCREMENTAL_BATCH:
                    total_added += append_to_library(batch)
                    batch = []
                if max_p and len(new_puzzles) >= max_p:
                    break
        finally:
            # Always flush remaining batch on exit (success, exception, or break)
            if batch:
                try:
                    total_added += append_to_library(batch)
                except Exception as flush_err:
                    sys.stderr.write(f"final library flush failed: {flush_err}\n")
            try: engine.quit()
            except Exception: pass

        self._send_json({
            "added": total_added,
            "total": len(new_puzzles),
            "puzzles": new_puzzles,
        })

    def _handle_clear(self):
        # Destructive: limited to loopback callers.
        if not self._is_local():
            return
        with _lib_lock:
            save_library([])
        self._send_json({"cleared": True})

    def log_message(self, format, *args):  # noqa: A002 — match BaseHTTPRequestHandler signature
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    engine_path = gen.default_engine_path()
    try:
        probe = launch_engine()
        probe.quit()
    except Exception as e:
        print(f"FATAL: engine failed at {engine_path}: {e}", file=sys.stderr)
        print("Place a Stockfish binary at puzzle\\engine\\stockfish.exe.", file=sys.stderr)
        return 2
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"FATAL: cannot bind 127.0.0.1:{port}: {e}", file=sys.stderr)
        return 2
    print(f"Server: http://127.0.0.1:{port}/", file=sys.stderr)
    print(f"Engine: {engine_path}", file=sys.stderr)
    print(f"Engine throttle: Threads={ENGINE_THREADS}, Hash={ENGINE_HASH_MB}MB "
          f"(set STOCKFISH_THREADS / STOCKFISH_HASH_MB env vars to change)",
          file=sys.stderr)
    print(f"Library: {LIBRARY_FILE}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
