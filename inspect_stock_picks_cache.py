# inspect_stock_picks_cache.py

import pandas as pd
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
file_path = PROJECT_DIR / "stock_picks_cache.pkl"

obj = pd.read_pickle(file_path)

print("========== BASIC INFO ==========")
print("type:", type(obj))

if isinstance(obj, pd.DataFrame):
    print("\n========== DATAFRAME ==========")
    print("shape:", obj.shape)
    print("columns:", obj.columns.tolist())
    print("\nhead:")
    print(obj.head(20).to_string(index=False))

elif isinstance(obj, dict):
    print("\n========== DICT ==========")
    print("keys:", list(obj.keys())[:50])

    for key, value in list(obj.items())[:10]:
        print("\n----- key:", key, "-----")
        print("value type:", type(value))

        if isinstance(value, pd.DataFrame):
            print("shape:", value.shape)
            print("columns:", value.columns.tolist())
            print(value.head(10).to_string(index=False))

        elif isinstance(value, list):
            print("list length:", len(value))
            print("first 5:", value[:5])

        elif isinstance(value, dict):
            print("dict keys:", list(value.keys())[:30])
            print("value:", value)

        else:
            print("value:", value)

elif isinstance(obj, list):
    print("\n========== LIST ==========")
    print("list length:", len(obj))
    print("first 10:")
    for item in obj[:10]:
        print(type(item), item)

else:
    print("\n========== RAW OBJECT ==========")
    print(obj)