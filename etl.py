"""Compose entry point: retrieve the configured cohort and produce analysis output."""

import os
import sys

from fhir_analyse import main as analyse_main
from fhir_retriever import main as retrieve_main


def main() -> int:
    output_dir = os.environ.get("FHIR_OUTPUT_DIR", "/data")
    # FHIR_COHORT_MODE=all loads multiple Patients (capped by FHIR_PATIENT_LIMIT); otherwise one Patient.
    if os.environ.get("FHIR_COHORT_MODE", "single").lower() == "all":
        sys.argv = [
            "fhir_retriever",
            "--all-patients", "--limit", os.environ.get("FHIR_PATIENT_LIMIT", "10"),
            "--output-dir", output_dir,
            "--condition", "--observation", "--encounter", "--refresh",
        ]
    else:
        patient_id = os.environ.get("FHIR_COHORT_PATIENT_ID", "sindhu-syn-000004")
        sys.argv = [
            "fhir_retriever",
            "--patient-observations-encounters", patient_id,
            "--output-dir", output_dir,
            "--condition", "--observation", "--encounter", "--refresh",
        ]
    if retrieve_main():
        return 1
    sys.argv = ["fhir_analyse", "--obs-value", "1742-6", "--group-by", "sex", "--output-dir", output_dir]
    return analyse_main()


if __name__ == "__main__":
    raise SystemExit(main())