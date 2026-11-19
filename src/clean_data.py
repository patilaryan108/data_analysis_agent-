"""
Step 2: Clean the BlinkIt data, save clean_data.csv, and load it into MySQL.

Run from the project root:  python src/clean_data.py
Expects: data/BlinkIT_Grocery_Data.xlsx  and a .env file with DB_USER, DB_PASSWORD, DB_HOST, DB_NAME
"""
import os
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parent.parent
RAW_FILE = ROOT / "data" / "BlinkIT_Grocery_Data.xlsx"
CLEAN_FILE = ROOT / "data" / "clean_data.csv"
TABLE_NAME = "blinkit_sales"


def load_raw() -> pd.DataFrame:
    if RAW_FILE.is_file():
        df = pd.read_excel(RAW_FILE)
    elif CLEAN_FILE.is_file():
        print(
            f"Raw Excel file not found at {RAW_FILE}; "
            f"using the existing cleaned CSV at {CLEAN_FILE}"
        )
        df = pd.read_csv(CLEAN_FILE)
    else:
        raise FileNotFoundError(
            f"Dataset not found. Expected the raw Excel file at {RAW_FILE}. "
            "Place BlinkIT_Grocery_Data.xlsx in the project's data folder."
        )

    print(f"Loaded raw data: {df.shape[0]} rows, {df.shape[1]} columns")
    return df


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """'Item Fat Content' -> 'item_fat_content' (MySQL-friendly names)."""
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df


def fix_fat_content(df: pd.DataFrame) -> pd.DataFrame:
    """5 messy labels -> Low Fat / Regular. Non-consumable items (ID starts with NC) -> Non-Edible."""
    mapping = {"low fat": "Low Fat", "lf": "Low Fat", "regular": "Regular", "reg": "Regular"}
    df["item_fat_content"] = (
        df["item_fat_content"].str.strip().str.lower().map(mapping)
    )
    df.loc[df["item_identifier"].str.startswith("NC"), "item_fat_content"] = "Non-Edible"
    return df


def fill_missing_weight(df: pd.DataFrame) -> pd.DataFrame:
    """Item weight is fixed per item, so copy it from other rows of the same item.
    Fallback: median of the item type, then the overall median."""
    before = df["item_weight"].isna().sum()
    df["item_weight"] = df.groupby("item_identifier")["item_weight"].transform(
        lambda s: s.fillna(s.mean())
    )
    df["item_weight"] = df.groupby("item_type")["item_weight"].transform(
        lambda s: s.fillna(s.median())
    )
    df["item_weight"] = df["item_weight"].fillna(df["item_weight"].median())
    print(f"Item weight: filled {before} missing values, {df['item_weight'].isna().sum()} left")
    return df


def fix_visibility(df: pd.DataFrame) -> pd.DataFrame:
    """A visibility of 0 is not realistic for a product on sale: treat it as missing and
    replace it with the item's average visibility."""
    zeros = (df["item_visibility"] == 0).sum()
    df["item_visibility"] = df["item_visibility"].replace(0, np.nan)
    df["item_visibility"] = df.groupby("item_identifier")["item_visibility"].transform(
        lambda s: s.fillna(s.mean())
    )
    df["item_visibility"] = df["item_visibility"].fillna(df["item_visibility"].median())
    print(f"Item visibility: replaced {zeros} zero values")
    return df


def check_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    full = df.duplicated().sum()
    key = df.duplicated(["item_identifier", "outlet_identifier"]).sum()
    print(f"Duplicates: {full} full-row, {key} on item+outlet")
    return df.drop_duplicates()


def report_outliers(df: pd.DataFrame, cols=("sales", "item_visibility", "item_weight")) -> None:
    """IQR rule. We only REPORT outliers; we do not delete sales rows."""
    for c in cols:
        q1, q3 = df[c].quantile([0.25, 0.75])
        iqr = q3 - q1
        n = ((df[c] < q1 - 1.5 * iqr) | (df[c] > q3 + 1.5 * iqr)).sum()
        print(f"Outliers in {c}: {n}")


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df["outlet_age"] = date.today().year - df["outlet_establishment_year"]
    prefix = df["item_identifier"].str[:2]
    df["item_category"] = prefix.map(
        {"FD": "Food", "DR": "Drinks", "NC": "Non-Consumable"}
    ).fillna("Other")
    return df


def save_csv(df: pd.DataFrame) -> None:
    df.to_csv(CLEAN_FILE, index=False)
    print(f"Saved {CLEAN_FILE}")


def load_to_mysql(df: pd.DataFrame) -> None:
    load_dotenv(ROOT / ".env")
    user, pwd = os.getenv("DB_USER"), os.getenv("DB_PASSWORD")
    host, name = os.getenv("DB_HOST", "localhost"), os.getenv("DB_NAME")
    engine = create_engine(f"mysql+pymysql://{user}:{pwd}@{host}/{name}")
    df.to_sql(TABLE_NAME, engine, if_exists="replace", index=False, chunksize=1000)
    print(f"Loaded {len(df)} rows into {name}.{TABLE_NAME}")


def main() -> None:
    df = load_raw()
    df = standardize_columns(df)
    df = check_duplicates(df)
    df = fix_fat_content(df)
    df = fill_missing_weight(df)
    df = fix_visibility(df)
    report_outliers(df)
    df = add_features(df)
    save_csv(df)
    load_to_mysql(df)
    print("Done.")


if __name__ == "__main__":
    main()
