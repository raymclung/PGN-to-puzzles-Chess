# PGN to Puzzle

Alat baris perintah yang membaca berkas PGN, mencari langkah yang mengubah posisi
seimbang menjadi kalah, lalu menjadikannya puzzle taktis.

```bash
python pgn_to_puzzles.py partai.pgn -o puzzles.json
```

## Cara kerjanya

Puzzle yang bagus lahir dari kesalahan. Program ini menelusuri setiap posisi dalam
sebuah partai dan mengevaluasinya dengan Stockfish, mencari momen ketika seorang pemain
menjatuhkan posisinya sendiri — ayunan evaluasi melebihi ambang `--swing`.

Posisi awal puzzle diambil **setelah** blunder itu terjadi. Solusinya adalah principal
variation mesin, dimainkan terus selama pihak yang melangkah hanya punya satu langkah
jelas terbaik: selisih ke langkah terbaik kedua harus lebih dari `--swing/2`. Begitu
muncul dua langkah yang sama baiknya, urutan solusi berhenti di situ.

Blunder yang tidak melahirkan puzzle bagus akan dibuang — posisi yang memang sudah kalah
telak sebelum blunder, akhir partai yang sepele, dan lanjutan yang sudah terpaksa sejak
awal.

## Soal tingkat kesulitan

Ini bagian yang paling lama saya pikirkan.

Awalnya level ditentukan oleh panjang solusi: makin panjang, makin sulit. Ternyata itu
keliru. Rangkaian panjang yang isinya langkah-langkah gamblang tetap mudah — pemain
tinggal mengikuti alurnya. Sebaliknya, satu langkah tenang yang brilian bisa sangat
sulit meski solusinya cuma satu langkah.

Jadi panjang solusi saya buang sebagai faktor. Yang menentukan sekarang adalah lanskap
evaluasinya — besar ayunan, seberapa telak posisi setelahnya — ditambah penanda
kecemerlangan seperti langkah tenang dan pengorbanan. Satu pengecualian struktural:
mat dalam satu langkah selalu level 1, karena hanya ada satu langkah yang perlu
ditemukan.

Pembagiannya dirancang membentuk kurva lonceng, supaya puzzle serupa selalu mendarat di
level yang sama dan pengguna bisa memperkirakan apa yang akan dihadapi:

| Level | Porsi | |
|---|---|---|
| 1 | ~10% | mat dalam 1, taktik gamblang |
| 2 | ~25% | |
| 3 | ~35% | |
| 4 | ~20% | |
| 5 | ~10% | langkah tenang, pengorbanan, mat panjang |

Tema-tema lain — fork, pin, mate-in-2 ke atas — hanya jadi label deskriptif, tidak
memengaruhi level.

## Tema yang dikenali

Dua puluh satu, semuanya ditulis dari nol tanpa pustaka tambahan:

`fork` `double-attack` `discovered-attack` `pin` `skewer` `x-ray` `trapped-piece`
`remove-defender` `deflection` `quiet` `sacrifice` `capture` `check` `promotion`
`mate-in-1`

Ditambah enam pola mat bernama: `smothered-mate` `anastasia-mate` `arabian-mate`
`boden-mate` `dovetail-mate` `hook-mate`

## Memakainya

```bash
pip install -r requirements.txt
```

Unduh Stockfish dari [stockfishchess.org](https://stockfishchess.org/download/), simpan
di folder `engine/`.

Contoh yang lebih spesifik — hanya puzzle sulit, maksimal dua per partai, analisis lebih
dalam:

```bash
python pgn_to_puzzles.py partai.pgn \
    --depth 18 --swing 300 --min-level 4 --max-per-game 2 \
    -o puzzles-sulit.json
```

Opsi lengkapnya: `-o` `--csv` `--engine` `--depth` `--swing` `--multipv` `--min-ply`
`--mate-only` `--min-level` `--max-puzzles` `--max-per-game` `--no-quality-filter`
`--threads` `--hash`

Ada juga dua skrip pembantu. `run_sample.py` mengambil beberapa partai pertama dari tiap
PGN di folder `pgns/` — berguna saat menyetel parameter. `run_full.py` memproses semuanya.
Keduanya memindai folder itu sendiri, jadi tinggal letakkan berkas PGN di sana.

## Keluarannya

```json
{
  "fen": "r1bq1rk1/pp2bppp/2n1pn2/...",
  "blunder_move": "Nxe5",
  "eval_before_cp": 24,
  "eval_after_cp": -412,
  "solution_san": ["Qh4", "g3", "Qxg3"],
  "themes": ["sacrifice", "discovered-attack", "mate-in-3"],
  "level": 5,
  "opening": "C46 — Three Knights Opening",
  "source_platform": "lichess"
}
```

Metadata partainya ikut terbawa: pemain, event, tanggal, ronde, pembukaan beserta kode
ECO, kontrol waktu, nama tim bila ada, dan platform asal yang disimpulkan dari URL situs.

Analisisnya berjalan streaming, jadi berkas PGN besar tidak perlu dimuat seluruhnya ke
memori.

## Kontribusi

Alat ini saya kembangkan atas penugasan dan arahan arsitek proyek tempat saya
mengerjakannya. Berkas PGN pertandingan, platform web, dan seluruh komponen yang khusus
milik penyelenggara turnamen sengaja tidak disertakan di sini.

## Lisensi

[MIT](LICENSE)
