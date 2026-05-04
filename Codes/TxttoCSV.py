import csv
import re
from pathlib import Path

WHITESPACE_SPLIT = re.compile(r"\s+")
NUMERIC_TOKEN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

def looks_numeric_row(tokens: list[str], min_cols=2) -> bool:
    if len(tokens) < min_cols:
        return False
    # require that most tokens look numeric (tolerate NaN/Inf too if you want)
    for t in tokens:
        if t in {"NaN", "nan", "INF", "Inf", "inf", "-Inf", "+Inf", "-inf", "+inf"}:
            continue
        if not NUMERIC_TOKEN.match(t):
            return False
    return True

def convert_txt_to_csv(txt_path: Path, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    with txt_path.open("r", encoding="utf-8", errors="replace") as fin, \
         csv_path.open("w", newline="", encoding="utf-8") as fout:

        writer = csv.writer(fout)
        in_data = False

        for line in fin:
            line = line.strip()
            if not line:
                continue

            row = WHITESPACE_SPLIT.split(line)

            # Skip header only until we see first numeric-like row
            if not in_data:
                if looks_numeric_row(row, min_cols=2):   # or min_cols=3 if you expect 3
                    in_data = True
                else:
                    continue

            writer.writerow(row)

    print(f"Converted: {txt_path} -> {csv_path}")



def batch_convert(input_dir: str, output_dir: str | None = None) -> None:
    in_dir = Path(input_dir)
    out_dir = Path(output_dir) if output_dir else in_dir

    txt_files = sorted(in_dir.glob("*.txt"))
    if not txt_files:
        print(f"No .txt files found in: {in_dir}")
        return

    for txt_file in txt_files:
        out_file = out_dir / (txt_file.stem + ".csv")
        convert_txt_to_csv(txt_file, out_file)


if __name__ == "__main__":
    batch_convert(
        input_dir=r"E:\\NemoComsolServer\\Nemo\\Tewelde`s\\ExperimentalData",
        output_dir=r"E:\\NemoComsolServer\\Nemo\\Tewelde`s\\ExperimentalData\\csv"
    )
