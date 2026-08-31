"""Create small synthetic files that document the private input schemas.

The generated values are artificial and are not used in the published results.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "sample_data"
TICKERS = ["SAMPLE_A", "SAMPLE_B", "SAMPLE_C"]


def write_csv(name: str, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUTPUT_DIR / name).open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(5)]

    price_rows = []
    mcap_rows = []
    volume_rows = []
    factor_rows = []
    for i, current in enumerate(dates):
        price_rows.append(
            {"Date": current.isoformat(), **{ticker: 100 + 5 * j + i for j, ticker in enumerate(TICKERS)}}
        )
        mcap_rows.append(
            {
                "Date": current.isoformat(),
                **{ticker: 1_000_000_000 + 100_000_000 * j + 10_000_000 * i for j, ticker in enumerate(TICKERS)},
            }
        )
        volume_rows.append({"Date": current.isoformat(), "KOSPI 200": 100_000_000 + i * 1_000_000})
        factor_rows.append(
            {
                "Date": current.isoformat(),
                "KOSPI200": 400 + i,
                "HML": 100 + i * 0.2,
                "SMB": 100 + i * 0.1,
                "MOM": 100 + i * 0.3,
                "CD(91)": 3.0,
            }
        )

    write_csv("prices_sample.csv", ["Date", *TICKERS], price_rows)
    write_csv("market_cap_sample.csv", ["Date", *TICKERS], mcap_rows)
    write_csv("kospi200_volume_sample.csv", ["Date", "KOSPI 200"], volume_rows)
    write_csv("ff4_factors_sample.csv", ["Date", "KOSPI200", "HML", "SMB", "MOM", "CD(91)"], factor_rows)


if __name__ == "__main__":
    main()
