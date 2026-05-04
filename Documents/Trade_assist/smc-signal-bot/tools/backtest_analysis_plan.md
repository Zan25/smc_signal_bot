# Backtest Analysis Plan

Berdasarkan hasil baseline, improvement yang akan dievaluasi (apply hanya jika backtest mendukung):

## Hipotesis dasar (akan divalidasi)

| H | Hipotesis | Cek di data |
|---|---|---|
| 1 | Setup tanpa BOS / CHoCH_entry punya WR rendah | Group by `bos in score path` vs not |
| 2 | Intraday `max_htf_conflicts=2` (4h_bear+1h_bull) loss-prone | Filter trades by trend alignment |
| 3 | Asia session (00-08 WIB) WR lebih rendah | Group by hour of `entry_ts` |
| 4 | Counter-trend (LONG pada BTC bearish, SHORT pada BTC bullish) loss | Need BTC bias data |
| 5 | Approaching=False signals di zona terlalu dalam (>50% OB) loss | (sudah dicegah CE check di paper_trader, tapi backtest pakai current_price) |
| 6 | Flag pattern bonus signals lebih winning | Group by `flag_pattern` |

## Logic improvements (akan apply yang divalidasi)

### A. Weighted confluence scoring
```
Faktor                          Score
TF alignment (always)              1
BOS searah                         2  (dari 1)
CHoCH di entry_tf                  2  (dari 1)
Flag pattern                       2  (dari 1)
OB+FVG overlap                     2  (dari 1)
Zone bonus                         1
OB di zone                         1
FVG di zone                        1
Liquidity nearby                   1
                                  ──
Max                               13

min_confidence target:
  swing: 7  (dari 4)
  intraday: 6  (dari 3)
```

### B. Intraday tighter alignment
`max_htf_conflicts: 2 → 1` — kecuali ada flag_pattern terdeteksi (override).

### C. Session filter
Skip 17:00-22:30 UTC (00:00-05:30 WIB) untuk intraday. Swing tidak.

### D. Volume confirmation OB
Impulsive move yang membentuk OB harus punya min 1 candle volume > 1.3× MA20 volume. Untuk reject false impulses di low-liq.

### E. Trailing SL setelah TP1 (penggantian "freeze BE")
- Setelah TP1 hit, trail SL pakai swing low/high terbaru di entry_tf  
- Kalau swing baru tidak ada, fallback ke 1.5×ATR trailing dari high/low post-TP1

### F. ATR-based SL minimum
SL minimum = max(ob_buffer, 1.0×ATR ob_tf). Hindari SL ditembus oleh wick normal.

### G. Funding rate filter (live only)
Skip pair dengan funding_rate searah posisi > +0.06% / 8h (saat Anda LONG dan funding +0.06% = "harga LONG sudah mahal"). Tidak applicable di backtest historical kecuali fetch funding history.
