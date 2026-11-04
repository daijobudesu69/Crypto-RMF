# state/

Ditulis bot setiap siklus dan di-commit oleh workflow (`[skip ci]`). Jangan edit
manual saat watcher jalan.

| File | Isi |
|---|---|
| `equity.csv` | 1 baris per hari: rezim BTC, ekuitas paper/live, flush, basket "beli semua koin", DD, telat eksekusi. `paper_equity` = buku momentum saja; `flush_equity` = PnL buku flush (mulai dari 0); `total_equity` = keduanya dijumlah (ekuitas paper sebenarnya) |
| `orders.csv` | semua order (paper & live, momentum & flush) |
| `ranking.csv` | peringkat 1–20 per hari + apakah dipegang paper/live |
| `flush_signals.csv` | jumlah sinyal di setiap candle 4h (event kalau ≥ 10) |
| `flush_trades.csv` | trade flush paper yang selesai: alasan exit, PnL, R |
| `runs.csv` | log siklus (baris idle maks 1x per jam) |
| `momentum_paper.json` | buku paper momentum + indeks basket |
| `momentum_view.json` | rezim + peringkat hari ini (dipakai ulang oleh percobaan live di hari yang sama) |
| `momentum_live.json` | hari terakhir live sukses, percobaan per hari, puncak ekuitas (breaker) |
| `flush_paper.json` | posisi flush paper terbuka + bar terakhir yang diproses |
| `outbox.json` | pesan Telegram yang belum terkirim |
| `alerts.json`, `watchdog.json` | penanda supaya alarm tidak dikirim berulang |
| `momentum_paper_rolling.json`, `momentum_view_rolling.json` | buku paper pembanding universe bulanan (hanya kalau `universe.compare_rolling: true`) |
| `universe_rolling.json` | anggota universe bulanan (urut volume), tanggal refresh terakhir, log `[tanggal, masuk, keluar]` |
| `equity_rolling.csv`, `orders_rolling.csv` | ekuitas + basket dan order buku pembanding (tidak dicerminkan ke Sheets) |

CSV memakai `merge=union` (`.gitattributes`), jadi run yang menulis bersamaan tidak bentrok.
