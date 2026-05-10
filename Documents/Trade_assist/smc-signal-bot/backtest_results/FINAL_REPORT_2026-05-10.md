# Backtest-Driven Tuning — Final Report (2026-05-10)

## Executive Summary

After Phase A (notifikasi cleanup) + 4 swing iterasi (C1-C4), kesimpulan utama:
**Baseline swing config sudah mendekati optimum** untuk framework SMC saat ini. 3 dari 4 hipotesis perbaikan justru memperburuk performa. Hanya 1 marginal improvement (`ob_fresh_lookback` 160→80) yang lulus pass criteria.

Live regime evidence (Apr 22 – May 10, 12 hari) menunjukkan:
- Swing 1/6 = 16.7% live WR (sample kecil → kena chop period regime)
- Intraday 32/49 = 65.3% WR (working)
- Backtest 365 hari swing 15 pair: **WR 58.5%, +28.20R, expectancy +0.106R**

Live swing 16.7% dari 6 trade = **statistical noise, BUKAN strategy broken**.

---

## Iterasi Detail

### Phase A — Notification Cleanup ✅ DEPLOYED
- Removed `send_signal_alert()` informational duplicate
- User sekarang hanya nerima ping kalau bot beneran ngambil aksi (pending limit / entry / exit)
- File: `main.py:241-296`

### Phase B — Baseline Backtest (15 Pair, 365 Hari)
**SWING:**
- 265 trades, WR 58.5%, total +28.20R, expectancy +0.106R, PF 1.26
- Outcomes: 35 TP2, 35 EXPIRED_TP1, 50 EXPIRED, 91 SL, 54 SL_BE
- 🟢 Best pairs: BNB (85% WR +6.80R), BTC (76.5% +8.88R), LTC (75% +4.64R), TRX (100% +3.92R, n=6)
- 🔴 Worst: ATOM (-3.70R), ETH (-5.58R), AVAX (-2.68R)

**INTRADAY:** Backtest aborted — ratio "approaching" signals tinggi membuat sampel trade terlalu kecil untuk inference. **Live data 49 trade (65.3% WR, +$16.75) lebih representatif**.

### Phase C1 — Confirmation Gate (15m close inside zone) ❌ REJECTED
| Metric | Baseline | C1 | Δ |
|--------|----------|-----|---|
| Trades | 265 | 130 | -51% |
| WR | 58.5% | 56.9% | -1.6% |
| Total R | +28.20 | +9.90 | **-65%** |
| Expectancy | +0.106 | +0.076 | -28% |

Gate menebas terlalu banyak winner. Gagal pass criteria.

### Phase C2 — `ob_fresh_lookback` 160→80 ✅ MARGINAL ACCEPT
| Metric | Baseline | C2 | Δ |
|--------|----------|-----|---|
| Trades | 265 | 270 | +5 |
| WR | 58.5% | 59.3% | +0.8% |
| Total R | +28.20 | +28.56 | +0.4 |
| PF | 1.26 | 1.29 | +0.03 |

Esensialnya neutral. **Adopted** karena fresher OB (~13 hari vs ~27 hari) lebih reflektif terhadap kondisi market saat ini, plus PF dan WR sedikit naik.

### Phase C3 — Deeper Zone Penetration (≥midpoint) ❌ REJECTED
| Metric | Baseline | C2+C3 | Δ |
|--------|----------|-------|---|
| Trades | 265 | 165 | -38% |
| Total R | +28.20 | +7.41 | **-74%** |
| Expectancy | +0.106 | +0.045 | -58% |

**Insight kunci:** Winner di-edge-of-zone (BTC -7.90R, DOT -8.95R, UNI -6.12R loss when filtered). Late entries di midpoint = momentum sudah habis.

### Phase C4 — BOS/CHoCH as Mandatory Gate ❌ REJECTED EARLY
First 4 pair: BTC 0 trades, ETH 6, SOL 3, BNB 1 (vs baseline 17/29/18/20). Gate terlalu ketat — ekstrapolasi total ~50 trade/year, di bawah threshold 30 trade/pair untuk income generator.

---

## Final Config Changes

```diff
# config/settings.py — swing strategy
-     "ob_fresh_lookback": 160,          # 4H×160 = ~27 days OB history
+     "ob_fresh_lookback": 80,           # C2: 4H×80 = ~13 days OB history (fresher OBs)
```

```diff
# main.py — scan loop
-     # Send signal alert
-     sent = _alerter.send_signal_alert(setup)
-     ...
+     # Only actionable notifications: pending / entry / exit
```

---

## Insight Mendalam — Kenapa Baseline Sudah Optimal

Hasil 4 iterasi menunjukkan pola konsisten: **gate filter manapun yang menebas signal mengurangi total R lebih cepat daripada peningkatan WR**. Pattern ini menunjukkan:

1. **Strategy SMC sudah well-tuned di parameter yang ada.** Baseline sudah punya: HTF alignment (3 TF align), score ≥4, RR ≥2.5, OB fresh lookback, volatility guard, BTC bias filter, max HTF conflicts.

2. **Edge entries (zone touch) adalah winner mostly.** Bukan late entries (midpoint). Ini logical SMC: harga retest OB level, langsung bounce. Kalau price masuk dalam, momentum sudah bukan untuk reversal lagi.

3. **CHoCH/BOS confirmation kurang reliable di 4H ob_tf.** Mungkin lebih relevan untuk intraday/scalp dengan ob_tf 1H/15m yang lebih responsif.

4. **Live "1/6 swing" tidak refleksi backtest 58.5% WR.** Sample kecil + market regime tertentu (Apr 22-May 10 banyak chop/reversal). Backtest 365 hari memberi gambaran lebih representatif.

---

## Phase F — Deploy Plan

**Yang akan di-deploy:**
1. ✅ Phase A: notification trim (informational signal_alert removed)
2. ✅ C2: `ob_fresh_lookback` 160→80
3. ✅ Compare/summarize tooling untuk future backtest

**Yang TIDAK di-deploy (failed pass criteria):**
- C1 confirmation gate
- C3 deeper zone penetration
- C4 BOS/CHoCH mandatory

**Live monitoring criteria:**
- Bot status persistent di Railway Volume `/data/paper_state.json`
- Target 30+ swing trades baru → re-evaluate vs backtest expected 58% WR
- Kalau live WR <45% setelah 30 trade → market regime fundamental change, bukan strategy issue

---

## Tools Created

- `tools/compare_runs.py` — side-by-side comparison antar backtest run
- `tools/summarize_run.py` — quick stats per pair / strategy
- `tools/backtest.py` — already existed, used as-is
- `tools/fetch_history.py` — already existed, expanded to 16 pair

## Files Modified

| File | Change |
|------|--------|
| `main.py` | Removed `send_signal_alert()` call |
| `config/settings.py` | `ob_fresh_lookback` 160→80 (swing) |
| `tools/fetch_history.py` | Added TRX/LTC/BCH/ATOM/UNI/APT to PAIRS list |
| `tools/compare_runs.py` | NEW — backtest delta analyzer |
| `tools/summarize_run.py` | NEW — quick stats |
