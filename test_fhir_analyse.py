import sqlite3
import tempfile
import unittest
from pathlib import Path

from fhir_analyse import analyse, main


class FHIRAnalyseTests(unittest.TestCase):
    def test_calculates_statistics_by_sex(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "fhir.sqlite3"
            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE patients (patient_id TEXT PRIMARY KEY, birth_date TEXT, gender TEXT);
                CREATE TABLE encounters (encounter_id TEXT PRIMARY KEY, encounter_type TEXT);
                CREATE TABLE observations (patient_id TEXT, encounter_id TEXT, observation_subtype TEXT,
                    observation_code TEXT, effective_date_time TEXT, value TEXT);
                INSERT INTO patients VALUES ('PAT-1', '1980-01-01', 'female');
                INSERT INTO encounters VALUES ('ENC-1', 'ambulatory');
                INSERT INTO observations VALUES ('PAT-1', 'ENC-1', 'Alanine Aminotransferase', '1742-6',
                    '2024-01-01', '10');
                INSERT INTO observations VALUES ('PAT-1', 'ENC-1', 'Alanine Aminotransferase', '1742-6',
                    '2024-01-02', '20');
                """
            )
            database.close()
            result = analyse(database_path, "Alanine Aminotransferase", group_by="sex")

        self.assertEqual(result["female"], {
            "count": 2,
            "mean": 15.0,
            "median": 15.0,
            "standard_deviation": 7.0710678118654755,
            "minimum": 10.0,
            "maximum": 20.0,
        })

    def test_cli_writes_analysis_text_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "fhir_resources.sqlite3"
            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE patients (patient_id TEXT PRIMARY KEY, birth_date TEXT, gender TEXT);
                CREATE TABLE encounters (encounter_id TEXT PRIMARY KEY, encounter_type TEXT);
                CREATE TABLE observations (patient_id TEXT, encounter_id TEXT, observation_subtype TEXT,
                    observation_code TEXT, effective_date_time TEXT, value TEXT);
                INSERT INTO patients VALUES ('PAT-1', '1980-01-01', 'female');
                INSERT INTO observations VALUES ('PAT-1', NULL, 'Test', 'test', '2024-01-01', '10');
                """
            )
            database.close()
            import sys

            previous_arguments = sys.argv
            try:
                sys.argv = ["fhir_analyse", "--obs-value", "Test", "--output-dir", temporary_directory]
                self.assertEqual(main(), 0)
            finally:
                sys.argv = previous_arguments

            self.assertIn('"count": 1', (Path(temporary_directory) / "analysis.txt").read_text())

    def test_calculates_statistics_by_encounter_type_via_encounter_id(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "fhir.sqlite3"
            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE patients (patient_id TEXT PRIMARY KEY, birth_date TEXT, gender TEXT);
                CREATE TABLE encounters (encounter_id TEXT PRIMARY KEY, encounter_type TEXT);
                CREATE TABLE observations (patient_id TEXT, encounter_id TEXT, observation_subtype TEXT,
                    observation_code TEXT, effective_date_time TEXT, value TEXT);
                INSERT INTO patients VALUES ('PAT-1', '1980-01-01', 'female');
                INSERT INTO encounters VALUES ('ENC-1', 'ambulatory');
                INSERT INTO observations VALUES ('PAT-1', 'ENC-1', 'Alanine Aminotransferase', '1742-6',
                    '2024-01-01', '10');
                INSERT INTO observations VALUES ('PAT-1', 'ENC-1', 'Alanine Aminotransferase', '1742-6',
                    '2024-01-02', '20');
                """
            )
            database.close()
            result = analyse(database_path, "1742-6", group_by="encounter-type")

        self.assertEqual(result["ambulatory"]["count"], 2)
        self.assertEqual(result["ambulatory"]["mean"], 15.0)