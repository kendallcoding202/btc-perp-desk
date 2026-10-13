# Quick checks from the final critique

The final critic ran these two scripts; another agent then reviewed each one. They are kept as a record. Paths inside them point at the audit session's scratch folder.

- **`xs_ext.py`: xs_carry on a wider coin list.** The same frozen rules on a point-in-time top 25 by 30-day volume, from a 75-coin pool that includes LUNA and FTT.
  - The review found two bugs. Dead contracts' flat, zero-volume bars were treated as tradable, and ties at the baseline funding rate were broken by column order.
  - Corrected: 2024–26 Sharpe about 0.45 (0.40–0.51 across tie-breaks), about +2% at double costs, 2022 about −30%, and a 2020–23 worst drop of 43–50%, mostly from the LUNA long.
  - On all 75 coins: −0.49, driven by shorting rallying memecoins.
  - Verdict: xs_carry fails on a wider, less survivor-biased universe.
- **`vt_pre2020.py`: VT's frozen rules on BTC/ETH spot, 2017-10 to 2019-12.**
  - Two coins at 0.65/3 each, with the third sleeve idle.
  - Sharpe 1.20 (+43%) with no funding, or 1.12 (+39%) with a 10%/yr funding stand-in. Worst drop 19%.
  - From 2018-01 on, the Sharpe is only 0.5–0.6.
  - The 2018 result (−10% vs −77% for buy-and-hold) is all low exposure: about 6% of equity on average, and flat 64% of days.
  - The review found no bugs.
