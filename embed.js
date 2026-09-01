/* ===========================================================
   Standalone embeddable chess widget — drop into any HTML page.

   Usage:
     <link rel="stylesheet" href="https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.css">
     <script src="https://unpkg.com/jquery@3.7.1/dist/jquery.min.js"></script>
     <script src="https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.js"></script>
     <script src="https://unpkg.com/chess.js@0.10.3/chess.js"></script>
     <script src="https://your-site.example.com/embed.js"></script>

     <div data-chess-game data-pgn="1. e4 e5 2. Nf3 Nc6 ..." data-orientation="white"></div>

   The widget renders a board with prev/next/auto controls and a move list.
   =========================================================== */
(function () {
  'use strict';
  document.addEventListener('DOMContentLoaded', initAll);
  if (document.readyState === 'interactive' || document.readyState === 'complete') initAll();

  function initAll() {
    document.querySelectorAll('[data-chess-game]').forEach(initWidget);
  }

  let widgetCounter = 0;

  function initWidget(host) {
    if (host._chessInited) return;
    host._chessInited = true;
    const id = 'cg-' + (++widgetCounter);
    const pgn = host.getAttribute('data-pgn') || '';
    const fen = host.getAttribute('data-fen') || '';
    const orientation = host.getAttribute('data-orientation') || 'white';
    const title = host.getAttribute('data-title') || '';
    const subtitle = host.getAttribute('data-subtitle') || '';

    host.innerHTML = `
      <div class="cg-wrap">
        <style>
          .cg-wrap { font-family: system-ui, sans-serif; max-width: 540px;
                     border: 1px solid #ddd; border-radius: 8px;
                     padding: 14px; background: #fff; }
          .cg-title { font-weight: 700; font-size: 15px; }
          .cg-sub   { font-size: 12px; color: #666; margin-bottom: 8px; }
          .cg-board { width: 100%; max-width: 480px; margin: 8px auto; }
          .cg-controls { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; align-items:center; }
          .cg-controls button {
            padding: 6px 10px; font-size: 13px; cursor: pointer;
            background: #fff; border: 1px solid #ccc; border-radius: 4px;
          }
          .cg-controls button:hover { background: #f0f0f0; }
          .cg-controls .cg-status { margin-left: 8px; color: #666; font-size: 12px; }
          .cg-moves {
            font-family: ui-monospace, monospace; font-size: 13px;
            max-height: 120px; overflow-y: auto;
            padding: 8px; background: #f6f6f8; border-radius: 4px;
          }
          .cg-moves .mv { display:inline-block; padding: 1px 4px; cursor: pointer; border-radius: 3px; margin-right: 2px; }
          .cg-moves .mv:hover { background: #eef; }
          .cg-moves .mv.cur { background: #2d3142; color: #fff; }
          .cg-moves .mv-num { color: #888; padding: 0 4px; }
        </style>
        ${title ? `<div class="cg-title">${title}</div>` : ''}
        ${subtitle ? `<div class="cg-sub">${subtitle}</div>` : ''}
        <div class="cg-board" id="${id}-board"></div>
        <div class="cg-controls">
          <button data-act="first" title="Awal">⏮</button>
          <button data-act="prev" title="Mundur">◀</button>
          <button data-act="next" title="Maju">▶</button>
          <button data-act="last" title="Akhir">⏭</button>
          <button data-act="auto" title="Auto-play">▶▶</button>
          <button data-act="flip">Flip</button>
          <span class="cg-status" id="${id}-status"></span>
        </div>
        <div class="cg-moves" id="${id}-moves"></div>
      </div>
    `;

    if (typeof Chessboard === 'undefined' || typeof Chess === 'undefined') {
      host.innerHTML = '<i style="color:#cb2431">Chess libs not loaded — add chessboardjs and chess.js scripts before embed.js</i>';
      return;
    }

    const chess = new Chess();
    if (fen) {
      try { chess.load(fen); } catch { /* ignore */ }
    }
    const startFen = chess.fen();
    let moves = [];
    if (pgn) {
      try {
        chess.load_pgn(pgn);
        const all = chess.history({ verbose: true });
        moves = all.map(m => ({ san: m.san, fen_after: null }));
        // Compute fen after each move
        chess.reset();
        if (fen) chess.load(fen);
        for (const m of all) {
          chess.move(m);
          moves[all.indexOf(m)] = { san: m.san, fen_after: chess.fen() };
        }
      } catch (e) { console.warn('Embed PGN parse error:', e); }
    }
    chess.reset();
    if (fen) chess.load(fen);

    let curPly = 0;
    // Piece images: set window.CHESS_PIECE_THEME to serve them yourself,
    // e.g. '/img/chesspieces/{piece}.png'. Falls back to the chessboard.js CDN.
    const pieceTheme = window.CHESS_PIECE_THEME
        || 'https://chessboardjs.com/img/chesspieces/wikipedia/{piece}.png';
    const board = Chessboard(id + '-board', {
      position: startFen,
      orientation,
      pieceTheme,
      draggable: false,
      moveSpeed: 200,
    });

    function go(ply) {
      ply = Math.max(0, Math.min(ply, moves.length));
      curPly = ply;
      const fenAt = (ply === 0) ? startFen : moves[ply - 1].fen_after;
      board.position(fenAt);
      renderMoves();
      document.getElementById(id + '-status').textContent =
        ply === 0 ? 'Posisi awal' : `Langkah ${Math.ceil(ply/2)}${ply % 2 === 1 ? '.' : '...'}`;
    }

    function renderMoves() {
      const el = document.getElementById(id + '-moves');
      if (!moves.length) { el.innerHTML = '<i style="color:#888">Tidak ada langkah.</i>'; return; }
      let html = '';
      for (let i = 0; i < moves.length; i++) {
        if (i % 2 === 0) html += `<span class="mv-num">${(i/2)+1}.</span>`;
        const cur = (i + 1) === curPly ? 'cur' : '';
        html += `<span class="mv ${cur}" data-ply="${i+1}">${moves[i].san}</span>`;
      }
      el.innerHTML = html;
      el.querySelectorAll('.mv').forEach(m => {
        m.onclick = () => go(parseInt(m.dataset.ply));
      });
      const cur = el.querySelector('.mv.cur');
      if (cur) cur.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }

    let autoTimer = null;
    function toggleAuto() {
      if (autoTimer) {
        clearInterval(autoTimer); autoTimer = null;
      } else {
        autoTimer = setInterval(() => {
          if (curPly >= moves.length) {
            clearInterval(autoTimer); autoTimer = null; return;
          }
          go(curPly + 1);
        }, 900);
      }
    }

    host.querySelectorAll('.cg-controls button').forEach(btn => {
      btn.onclick = () => {
        switch (btn.dataset.act) {
          case 'first': go(0); break;
          case 'prev':  go(curPly - 1); break;
          case 'next':  go(curPly + 1); break;
          case 'last':  go(moves.length); break;
          case 'auto':  toggleAuto(); break;
          case 'flip':  board.flip(); break;
        }
      };
    });

    go(0);
  }
})();
