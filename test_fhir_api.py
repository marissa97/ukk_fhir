import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import fhir_api


class FHIRApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "fhir.sqlite3"
        database = sqlite3.connect(database_path)
        database.executescript(
            """
            CREATE TABLE patients (patient_id TEXT PRIMARY KEY, family_name TEXT, given_name TEXT,
                gender TEXT, birth_date TEXT);
            CREATE TABLE encounters (encounter_id TEXT PRIMARY KEY, encounter_type TEXT);
            CREATE TABLE observations (observation_id TEXT PRIMARY KEY, patient_id TEXT, encounter_id TEXT,
                observation_code TEXT, observation_subtype TEXT, effective_date_time TEXT, value TEXT);
            INSERT INTO patients VALUES ('PAT-1', 'Doe', 'Jane', 'female', '1980-01-01');
            INSERT INTO encounters VALUES ('ENC-1', 'ambulatory');
            INSERT INTO observations VALUES ('OBS-1', 'PAT-1', 'ENC-1', '1742-6',
                'Alanine Aminotransferase', '2024-01-01', '10');
            """
        )
        database.close()
        self.previous_database_path = fhir_api.DATABASE_PATH
        fhir_api.DATABASE_PATH = database_path
        self.client = TestClient(fhir_api.app)

    def tearDown(self):
        fhir_api.DATABASE_PATH = self.previous_database_path
        self.temporary_directory.cleanup()

    def test_api_endpoints_and_validation(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/patients?limit=1").json()["total"], 1)
        self.assertEqual(self.client.get("/patients/PAT-1").status_code, 200)
        self.assertEqual(self.client.get("/patients/PAT-404").status_code, 404)
        self.assertEqual(self.client.get("/observations?observation_code=1742-6").json()["total"], 1)
        self.assertEqual(self.client.get("/observations?limit=0").status_code, 422)
        self.assertIn("ambulatory", self.client.get("/analysis/observations?observation=1742-6&group_by=encounter-type").json())

    def test_patient_responses_do_not_include_names(self):
        collection_item = self.client.get("/patients?limit=1").json()["items"][0]
        single_patient = self.client.get("/patients/PAT-1").json()
        for response in (collection_item, single_patient):
            self.assertNotIn("family_name", response)
            self.assertNotIn("given_name", response)
        self.assertEqual(single_patient["patient_id"], "PAT-1")

    def test_patients_endpoint_fetches_live_when_cache_is_short(self):
        calls = []

        def fake_get_all_patients(limit=None, **retriever_options):
            calls.append((limit, retriever_options))
            from dataclasses import dataclass, field

            @dataclass
            class Report:
                resources: dict = field(default_factory=dict)
                failures: list = field(default_factory=list)

            return Report()

        original = fhir_api.get_all_patients
        fhir_api.get_all_patients = fake_get_all_patients
        try:
            response = self.client.get("/patients?limit=10")
        finally:
            fhir_api.get_all_patients = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 10)