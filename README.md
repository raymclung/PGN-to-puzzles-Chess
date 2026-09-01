<div align="center">

# ♟️ PGN → Puzzle

**Alat baris perintah yang mengubah berkas PGN menjadi puzzle taktis catur, memakai Stockfish.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![python-chess](https://img.shields.io/badge/python--chess-1.999-1F4FD9?style=flat-square)](https://python-chess.readthedocs.io/)
[![Stockfish](https://img.shields.io/badge/Engine-Stockfish-15B79E?style=flat-square)](https://stockfishchess.org/)
[![CLI](https://img.shields.io/badge/Antarmuka-CLI-6B7280?style=flat-square)](#-penggunaan)

</div>

---

## 📖 Cara kerjanya

Puzzle yang bagus lahir dari kesalahan. Alat ini menelusuri setiap posisi dalam sebuah
partai, lalu mencari momen ketika seorang pemain **mengubah posisi yang seimbang atau
menang menjadi jelas kalah**.

```
partai.pgn ──▶ telusuri tiap langkah ──▶ evaluasi dengan Stockfish
                                              │
                        ayunan evaluasi ≥ --swing centipawn?
                                              │ ya
                                              ▼
              posisi SETELAH blunder  ──▶  puzzle
              solusi = principal variation, selama hanya ada satu langkah terbaik
```

Posisi awal puzzle diambil **setelah** blunder terjadi. Solusinya adalah *principal
variation* mesin, dimainkan terus selama pihak yang melangkah hanya punya satu langkah
jelas terbaik — selisih ke langkah terbaik kedua harus melebihi `--swing/2`. Begitu
muncul dua langkah yang sama baiknya, urutan solusi dihentikan di situ.

## ✨ Yang membedakan

**Deteksi 21 tema taktis**, ditulis sendiri tanpa pustaka luar:

| Kelompok | Tema |
|---|---|
| Serangan ganda | `fork`, `double-attack`, `discovered-attack` |
| Membatasi gerak | `pin`, `skewer`, `x-ray`, `trapped-piece` |
| Membongkar pertahanan | `remove-defender`, `deflection` |
| Pola mat bernama | `smothered-mate`, `anastasia-mate`, `arabian-mate`, `boden-mate`, `dovetail-mate`, `hook-mate` |
| Penanda kualitas | `quiet`, `sacrifice`, `capture`, `check`, `promotion`, `mate-in-1` |

**Tingkat kesulitan 1–5 yang dikalibrasi.** Levelnya ditentukan oleh lanskap evaluasi
dan penanda kecemerlangan — bukan oleh panjang solusi. Alasannya disengaja: rangkaian
panjang berisi langkah-langkah gamblang tetap mudah, sementara satu langkah tenang yang
brilian tetap sulit. Pembagiannya dirancang membentuk kurva lonceng:

| Level | Porsi | Ciri |
|---|---|---|
| 1 — sangat mudah | ~10% | mat dalam 1 langkah, taktik gamblang |
| 2 — mudah | ~25% | |
| 3 — normal | ~35% | |
| 4 — sulit | ~20% | |
| 5 — sangat sulit | ~10% | langkah tenang, pengorbanan, mat panjang |

**Penyaring kualitas.** Blunder yang tidak melahirkan puzzle bagus akan dibuang: posisi
yang memang sudah kalah telak sebelum blunder, akhir partai yang sepele, dan lanjutan
yang sudah dipaksa sejak awal.

## 🚀 Penggunaan

```bash
pip install -r requirements.txt
```

Unduh Stockfish dari [stockfishchess.org/download](https://stockfishchess.org/download/),
lalu simpan di `engine/`.

```bash
python pgn_to_puzzles.py partai.pgn -o puzzles.json
```

Contoh yang lebih spesifik — hanya puzzle sulit, maksimal dua per partai:

```bash
python pgn_to_puzzles.py partai.pgn \
    --depth 18 --swing 300 --min-level 4 --max-per-game 2 \
    -o puzzles-sulit.json
```

### Opsi

| Opsi | Arti |
|---|---|
| `-o`, `--csv` | Keluaran JSON dan/atau CSV |
| `--engine` | Lokasi biner Stockfish |
| `--depth` | Kedalaman analisis (makin dalam makin akurat, makin lambat) |
| `--swing` | Ambang ayunan centipawn yang dianggap blunder |
| `--multipv` | Jumlah langkah kandidat yang dievaluasi |
| `--min-ply` | Lewati pembukaan sampai langkah ke-N |
| `--mate-only` | Hanya ambil puzzle yang berujung mat |
| `--min-level` | Saring berdasarkan tingkat kesulitan |
| `--max-puzzles`, `--max-per-game` | Batasi jumlah keluaran |
| `--no-quality-filter` | Matikan penyaring kualitas |
| `--threads`, `--hash` | Setelan sumber daya mesin |

### Skrip pembantu

- **`run_sample.py`** — ambil beberapa partai pertama dari tiap PGN di `pgns/`, berguna saat menyetel parameter
- **`run_full.py`** — proses seluruh berkas PGN di `pgns/`

Keduanya memindai folder `pgns/` secara otomatis, jadi tinggal letakkan berkas PGN di sana.

## 📦 Keluaran

Tiap puzzle tersimpan sebagai satu objek JSON:

```json
{
  "game_id": "partai-001",
  "fen": "r1bq1rk1/pp2bppp/2n1pn2/...",
  "blunder_move": "Nxe5",
  "eval_before_cp": 24,
  "eval_after_cp": -412,
  "solution_uci": ["d8h4", "g2g3", "h4g3"],
  "solution_san": ["Qh4", "g3", "Qxg3"],
  "themes": ["sacrifice", "discovered-attack", "mate-in-3"],
  "level": 5,
  "opening": "C46 — Three Knights Opening",
  "time_control": "600+2",
  "source_platform": "lichess"
}
```

Metadata partai ikut terbawa: pemain, event, tanggal, ronde, pembukaan (kode ECO),
kontrol waktu, nama tim bila ada, dan platform asal yang disimpulkan dari URL situs.

## 🛠️ Teknologi

| Aspek | Pilihan |
|---|---|
| **Bahasa** | Python 3.10+ |
| **Dependensi** | `python-chess` — hanya satu |
| **Mesin catur** | Stockfish, lewat protokol UCI |
| **Antarmuka** | CLI dengan `argparse` |
| **Keluaran** | JSON dan CSV |

Analisis berjalan dalam mode *streaming*, sehingga berkas PGN besar tidak perlu dimuat
seluruhnya ke memori.

## 🤝 Kontribusi

Bagian terbesar alat ini saya tulis sendiri — logika deteksi blunder, deteksi tema
taktis, dan penentuan tingkat kesulitan. Pengembangannya berlangsung dalam sebuah
proyek bersama, dengan arahan dan tinjauan dari rekan sekaligus arsitek proyek
tersebut.

Berkas PGN pertandingan, platform web, serta seluruh komponen yang khusus milik
penyelenggara turnamen sengaja **tidak** disertakan di repositori ini.

## 📄 Lisensi

Dirilis di bawah [Lisensi MIT](LICENSE).

---

<div align="center">
<sub>Dibuat oleh <a href="https://github.com/raymclung">@raymclung</a></sub>
</div>
