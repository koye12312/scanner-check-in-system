import csv
import os
from pathlib import Path

# ✅ Import REG_CSV from app.py so we use the same file path
from app import REG_CSV  

EXPECTED_COLUMNS = 11  # Adjust if you add more later

if not os.path.exists(REG_CSV):
    print(f"❌ File {REG_CSV} not found.")
    exit()

rows = []
with open(REG_CSV, newline="") as f:
    reader = csv.reader(f)
    for row in reader:
        if len(row) < EXPECTED_COLUMNS:
            row += [""] * (EXPECTED_COLUMNS - len(row))
        rows.append(row)

# Save back to file
with open(REG_CSV, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerows(rows)

print(f"✅ CSV now has {EXPECTED_COLUMNS} columns for all rows.")
