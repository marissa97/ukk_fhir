"""Calculate Observation summary statistics from the local FHIR SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path


def age_band(birth_date: str | None, effective_date: str | None) -> str:
    if not birth_date:
        return "unknown"
    try:
        reference_date = date.fromisoformat((effective_date or date.today().isoformat())[:10])
        born = date.fromisoformat(birth_date)
    except ValueError:
        return "unknown"
    age = reference_date.year - born.year - ((reference_date.month, reference_date.day) < (born.month, born.day))
    if age < 0:
        return "unknown"
    lower = (age // 10) * 10
    return f"{lower}-{lower + 9}"


def summarize(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "standard_deviation": statistics.stdev(values) if len(values) > 1 else 0.0,
        "minimum": min(values),
        "maximum": max(values),
    }


def analyse(
    database_path: str | Path,
    observation_value: str,
    patient_id: str | None = None,
    group_by: str = "age-band",
) -> dict[str, dict[str, float | int]]:
    """Return numeric Observation statistics grouped by age band, sex, or encounter type."""
    group_columns = {
        "age-band": None,
        "sex": "p.gender",
        "encounter-type": "e.encounter_type",
    }
    if group_by not in group_columns:
        raise ValueError(f"Unsupported group: {group_by}")

    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT o.value, o.effective_date_time, p.birth_date, p.gender, e.encounter_type "
            "FROM observations o "
            "JOIN patients p ON p.patient_id = o.patient_id "
            "LEFT JOIN encounters e ON e.encounter_id = o.encounter_id "
            "WHERE (o.observation_subtype = ? OR o.observation_code = ?) "
            "AND (? IS NULL OR o.patient_id = ?)",
            (observation_value, observation_value, patient_id, patient_id),
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[str, list[float]] = defaultdict(list)
    for value, effective_date, birth_date, gender, encounter_type in rows:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if group_by == "age-band":
            group = age_band(birth_date, effective_date)
        elif group_by == "sex":
            group = gender or "unknown"
        else:
            group = encounter_type or "unknown"
        grouped[group].append(numeric_value)
    return {group: summarize(values) for group, values in sorted(grouped.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obs-value", required=True, help="Observation subtype or LOINC code")
    parser.add_argument("--patient-id", help="Pseudonymized patient ID, such as PAT-...")
    parser.add_argument(
        "--group-by",
        choices=("age-band", "sex", "encounter-type"),
        default="age-band",
    )
    parser.add_argument("--output-dir", default="fhir_output")
    parser.add_argument("--database")
    arguments = parser.parse_args()

    database_path = arguments.database or Path(arguments.output_dir) / "fhir_resources.sqlite3"
    result = analyse(database_path, arguments.obs_value, arguments.patient_id, arguments.group_by)
    output = json.dumps(result, indent=2)
    output_directory = Path(arguments.output_dir)
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "analysis.txt").write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())