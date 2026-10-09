# paper-ledger

The $50 BTC perp paper experiment's record, written by `.github/workflows/paper.yml` on `main`.

- `ledger/paper.json`: account, open position, every trade, the desk's memory
- `ledger/kills.json` (optional): create or edit it here to flip kill switches, e.g. `{"entry_kill": true}` or `{"flat_kill": true}`

Each commit is one moment in the account's life; the commit message shows the equity at that moment.
