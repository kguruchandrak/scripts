"""Convert a Parquet file (or every Parquet file in a folder) to CSV.

Usage:
    python parquet_to_csv.py data.parquet
    python parquet_to_csv.py data.parquet -o out/data.csv
    python parquet_to_csv.py parquet_folder/ -o csv_folder/

Requires: pip install pandas pyarrow
"""
import argparse
from pathlib import Path

import pandas as pd


def convert(src: Path, dst: Path) -> None:
    df = pd.read_parquet(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dst, index=False)
    print(f"{src} -> {dst} ({len(df):,} rows, {len(df.columns)} columns)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Parquet file(s) to CSV.")
    parser.add_argument("input", type=Path, help="Parquet file or folder of Parquet files")
    parser.add_argument("-o", "--output", type=Path, help="Output CSV file (or folder when input is a folder)")
    args = parser.parse_args()

    if args.input.is_dir():
        out_dir = args.output or args.input
        files = sorted(args.input.glob("*.parquet"))
        if not files:
            parser.error(f"No .parquet files found in {args.input}")
        for f in files:
            convert(f, out_dir / f"{f.stem}.csv")
    elif args.input.is_file():
        convert(args.input, args.output or args.input.with_suffix(".csv"))
    else:
        parser.error(f"Input not found: {args.input}")


if __name__ == "__main__":
    main()
