import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fhir_retriever import FHIRRetriever


class EncounterLinkRepairTests(unittest.TestCase):
    def test_repairs_observation_links_using_cached_encounter_identifier(self):
        os.environ.setdefault("FHIR_PSEUDONYMIZATION_KEY", "test-only-key-not-for-production")
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            database = sqlite3.connect(output_directory / "fhir_resources.sqlite3")
            database.executescript(
                """
                CREATE TABLE patients (patient_id TEXT PRIMARY KEY, family_name TEXT, given_name TEXT,
                    gender TEXT, birth_date TEXT, date_shift_days INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE observations (observation_id TEXT PRIMARY KEY, patient_id TEXT, encounter_id TEXT,
                    observation_type TEXT, observation_code TEXT, observation_subtype TEXT,
                    effective_date_time TEXT, issued TEXT, value TEXT, unit TEXT, value_code TEXT);
                CREATE TABLE encounters (encounter_type TEXT, encounter_id TEXT PRIMARY KEY, start TEXT,
                    end TEXT, patient_id TEXT);
                INSERT INTO observations VALUES ('o1', 'p1', 'Encounter/e1', NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL);
                INSERT INTO encounters VALUES ('ambulatory', 'visit-1', NULL, NULL, 'p1');
                """
            )
            database.close()
            FHIRRetriever._write_json(
                output_directory / "resources.json.gz",
                {
                    "Encounter/e1": {
                        "resourceType": "Encounter",
                        "id": "e1",
                        "identifier": [{"value": "visit-1"}],
                    }
                },
            )
            retriever = FHIRRetriever(output_dir=output_directory)
            repaired = retriever.database.connection.execute(
                "SELECT encounter_id FROM observations WHERE observation_id = 'o1'"
            ).fetchone()
            retriever.database.close()

        self.assertEqual(repaired, ("visit-1",))