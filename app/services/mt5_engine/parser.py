import math
import os
import re

from bs4 import BeautifulSoup


class ReportParser:
    @staticmethod
    def _number(text: str, label: str) -> float:
        normalized = text.replace("\xa0", " ").strip()
        match = re.search(r"[-+]?\d[\d\s,]*(?:\.\d+)?", normalized)
        if match is None:
            raise ValueError(f"Could not parse {label} from MT5 report value: {text!r}")

        value_text = match.group().replace(" ", "").replace(",", "")
        value = float(value_text)
        if not math.isfinite(value):
            raise ValueError(f"MT5 report contains a non-finite {label} value.")
        return value

    def parse_html_report(self, report_path: str) -> dict:
        if not os.path.isfile(report_path):
            raise FileNotFoundError(f"MT5 report was not created: {report_path}")

        with open(report_path, "rb") as report_file:
            content = report_file.read()
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            html = content.decode("utf-16")
        elif content.startswith(b"\xef\xbb\xbf"):
            html = content.decode("utf-8-sig")
        else:
            try:
                html = content.decode("utf-8")
            except UnicodeDecodeError:
                html = content.decode("cp1252")

        soup = BeautifulSoup(html, "lxml")
        cells = [cell.get_text(" ", strip=True) for cell in soup.find_all("td")]
        metrics = {}
        for index, label in enumerate(cells[:-1]):
            normalized_label = " ".join(label.split()).casefold().rstrip(":")
            next_value = cells[index + 1]
            if normalized_label == "total net profit":
                metrics["NetProfit"] = self._number(next_value, "net profit")
            elif normalized_label in {
                "maximal drawdown",
                "equity drawdown maximal",
            } or (
                normalized_label == "balance drawdown maximal"
                and "MaxDrawdown" not in metrics
            ):
                percent = re.search(r"\(\s*([-+]?\d[\d\s,]*(?:\.\d+)?)\s*%\s*\)", next_value)
                if percent is None:
                    raise ValueError(
                        f"Could not parse maximal drawdown percentage: {next_value!r}"
                    )
                metrics["MaxDrawdown"] = self._number(percent.group(1), "drawdown")
            elif normalized_label == "profit factor":
                metrics["ProfitFactor"] = self._number(next_value, "profit factor")

        required = {"NetProfit", "MaxDrawdown", "ProfitFactor"}
        missing = required.difference(metrics)
        if missing:
            raise ValueError(
                "MT5 report is missing required metrics: "
                + ", ".join(sorted(missing))
            )
        return metrics
