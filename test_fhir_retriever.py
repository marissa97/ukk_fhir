import json
import gzip
import csv
import tempfile
import unittest
import sqlite3
import os
from datetime import date
from pathlib import Path

from fhir_retriever import (
    FHIRRetriever,
    get_observations_and_encounters_for_patient,
    get_observations_for_patient,
    get_encounters_for_patient,
    get_conditions_for_patient,
    get_all_patients,
    get_related_for_patient,
    get_patient,
)


os.environ.setdefault("FHIR_PSEUDONYMIZATION_KEY", "test-only-key-not-for-production")

class FakeResponse:
    def __init__(self, body, error=None):
        self.body = body
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        response = self.responses.get((url, tuple(sorted((params or {}).items()))))
        if response is None:
            raise AssertionError(f"Unexpected request: {url} {params}")
        return response


def bundle(*resources, next_url=None):
    result = {"resourceType": "Bundle", "entry": [{"resource": resource} for resource in resources]}
    if next_url:
        result["link"] = [{"relation": "next", "url": next_url}]
    return result


class FHIRRetrieverTests(unittest.TestCase):
    endpoint = "https://example.test/fhir"

    def response_map(self, condition_response=None):
        patient_url = f"{self.endpoint}/Patient"
        condition_url = f"{self.endpoint}/Condition"
        observation_url = f"{self.endpoint}/Observation"
        return {
            (patient_url, (("_count", "50"),)): FakeResponse(
                bundle({"resourceType": "Patient", "id": "one"})
            ),
            (condition_url, (("_count", "50"), ("patient", "one"))): condition_response
            or FakeResponse(bundle({"resourceType": "Condition", "id": "c1"})),
            (observation_url, (("_count", "50"), ("patient", "one"))): FakeResponse(
                bundle({"resourceType": "Observation", "id": "o1"})
            ),
        }

    def test_collects_paginated_patients_and_related_resources(self):
        patient_url = f"{self.endpoint}/Patient"
        page_two = f"{patient_url}?page=2"
        responses = self.response_map()
        responses[(patient_url, (("_count", "50"),))] = FakeResponse(
            bundle({"resourceType": "Patient", "id": "one"}, next_url=page_two)
        )
        responses[(page_two, ())] = FakeResponse(bundle({"resourceType": "Patient", "id": "two"}))
        responses[(f"{self.endpoint}/Condition", (("_count", "50"), ("patient", "two")))] = FakeResponse(bundle())
        responses[(f"{self.endpoint}/Observation", (("_count", "50"), ("patient", "two")))] = FakeResponse(bundle())

        with tempfile.TemporaryDirectory() as temporary_directory:
            report = FHIRRetriever(self.endpoint, temporary_directory, session=FakeSession(responses)).retrieve()
            with gzip.open(Path(temporary_directory) / "resources.json.gz", "rt") as file:
                saved = json.load(file)

        self.assertEqual(
            report.resources,
            {"Patient": 2, "Condition": 1, "Observation": 1, "Encounter": 0},
        )
        self.assertFalse(report.failures)
        self.assertEqual(set(saved), {"Patient/one", "Patient/two", "Condition/c1", "Observation/o1"})

    def test_partial_failure_is_reported_and_rerun_retries_only_missing_query(self):
        from requests import ConnectionError

        with tempfile.TemporaryDirectory() as temporary_directory:
            failed = FHIRRetriever(
                self.endpoint,
                temporary_directory,
                session=FakeSession(self.response_map(FakeResponse({}, ConnectionError("offline")))),
            ).retrieve()
            rerun_session = FakeSession(self.response_map())
            rerun = FHIRRetriever(self.endpoint, temporary_directory, session=rerun_session).retrieve()

        self.assertEqual(len(failed.failures), 1)
        self.assertEqual(failed.failures[0].query, "Condition?patient=one")
        self.assertFalse(rerun.failures)
        condition_calls = [call for call in rerun_session.calls if call[0].endswith("/Condition")]
        observation_calls = [call for call in rerun_session.calls if call[0].endswith("/Observation")]
        self.assertEqual(len(condition_calls), 1)
        self.assertEqual(observation_calls, [])

    def test_failed_later_page_keeps_cached_resources_and_rerun_completes_query(self):
        from requests import Timeout

        observation_url = f"{self.endpoint}/Observation"
        observation_page_two = f"{observation_url}?page=2"
        failed_responses = self.response_map()
        failed_responses[(observation_url, (("_count", "50"), ("patient", "one")))] = FakeResponse(
            bundle(
                {"resourceType": "Observation", "id": "o1", "subject": {"reference": "Patient/one"}},
                next_url=observation_page_two,
            )
        )
        failed_responses[(observation_page_two, ())] = FakeResponse({}, Timeout("read timed out"))

        with tempfile.TemporaryDirectory() as temporary_directory:
            failed = FHIRRetriever(
                self.endpoint, temporary_directory, session=FakeSession(failed_responses)
            ).retrieve()
            with gzip.open(Path(temporary_directory) / "resources.json.gz", "rt") as file:
                partial_cache = json.load(file)

            rerun_responses = self.response_map()
            rerun_responses[(observation_url, (("_count", "50"), ("patient", "one")))] = FakeResponse(
                bundle(
                    {"resourceType": "Observation", "id": "o1", "subject": {"reference": "Patient/one"}},
                    next_url=observation_page_two,
                )
            )
            rerun_responses[(observation_page_two, ())] = FakeResponse(
                bundle({"resourceType": "Observation", "id": "o2", "subject": {"reference": "Patient/one"}})
            )
            rerun = FHIRRetriever(
                self.endpoint, temporary_directory, session=FakeSession(rerun_responses)
            ).retrieve()

        self.assertEqual(len(failed.failures), 1)
        self.assertEqual(failed.failures[0].query, "Observation?patient=one")
        self.assertIn("Observation/o1", partial_cache)
        self.assertFalse(rerun.failures)
        self.assertEqual(rerun.resources["Observation"], 1)

    def test_completed_cache_reruns_without_network_requests(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            FHIRRetriever(
                self.endpoint, temporary_directory, session=FakeSession(self.response_map())
            ).retrieve()
            cached_session = FakeSession({})
            report = FHIRRetriever(
                self.endpoint, temporary_directory, session=cached_session
            ).retrieve()

        self.assertFalse(report.failures)
        self.assertEqual(
            report.resources,
            {"Patient": 0, "Condition": 0, "Observation": 0, "Encounter": 0},
        )
        self.assertEqual(cached_session.calls, [])

    def test_refresh_updates_cached_resources(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            FHIRRetriever(
                self.endpoint, temporary_directory, session=FakeSession(self.response_map())
            ).retrieve()
            refreshed_responses = self.response_map()
            refreshed_responses[(f"{self.endpoint}/Patient", (("_count", "50"),))] = FakeResponse(
                bundle({"resourceType": "Patient", "id": "one", "active": False})
            )
            refreshed_session = FakeSession(refreshed_responses)
            FHIRRetriever(
                self.endpoint,
                temporary_directory,
                refresh=True,
                session=refreshed_session,
            ).retrieve()
            with gzip.open(Path(temporary_directory) / "resources.json.gz", "rt") as file:
                saved = json.load(file)

        self.assertEqual(len(refreshed_session.calls), 3)
        self.assertFalse(saved["Patient/one"]["active"])

    def test_database_links_conditions_and_observations_to_patient_and_encounter(self):
        responses = self.response_map()
        responses[(f"{self.endpoint}/Condition", (("_count", "50"), ("patient", "one")))] = FakeResponse(
            bundle(
                {
                    "resourceType": "Condition",
                    "id": "c1",
                    "subject": {"reference": "Patient/one"},
                    "encounter": {"reference": "Encounter/e1"},
                }
            )
        )
        responses[(f"{self.endpoint}/Observation", (("_count", "50"), ("patient", "one")))] = FakeResponse(
            bundle(
                {
                    "resourceType": "Observation",
                    "id": "o1",
                    "subject": {"reference": "https://example.test/fhir/Patient/one"},
                    "encounter": {"reference": "Encounter/e1"},
                }
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            FHIRRetriever(
                self.endpoint, temporary_directory, session=FakeSession(responses)
            ).retrieve()
            database = sqlite3.connect(Path(temporary_directory) / "fhir_resources.sqlite3")
            condition = database.execute(
                "SELECT patient_id, encounter_id FROM conditions WHERE condition_id = 'c1'"
            ).fetchone()
            observation = database.execute(
                "SELECT patient_id, encounter_id FROM observations WHERE observation_id = 'o1'"
            ).fetchone()
            database.close()

        self.assertEqual(condition, ("one", "e1"))
        self.assertEqual(observation, ("one", "e1"))

    def test_patient_id_limits_related_searches_to_the_selected_patient(self):
        patient_url = f"{self.endpoint}/Patient?_id=one"
        responses = {
            (patient_url, (("_count", "50"), ("_id", "one"))): FakeResponse(
                bundle({"resourceType": "Patient", "id": "one"})
            ),
            (f"{self.endpoint}/Condition", (("_count", "50"), ("patient", "one"))): FakeResponse(bundle()),
            (f"{self.endpoint}/Observation", (("_count", "50"), ("patient", "one"))): FakeResponse(bundle()),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            session = FakeSession(responses)
            report = FHIRRetriever(
                self.endpoint,
                temporary_directory,
                patient_id="one",
                session=session,
            ).retrieve()

        self.assertFalse(report.failures)
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(all(call[1].get("patient", "one") == "one" for call in session.calls))

    def test_all_patients_limit_stops_after_requested_total(self):
        patient_url = f"{self.endpoint}/Patient"
        page_two = f"{patient_url}?page=2"
        responses = {
            (patient_url, (("_count", "2"),)): FakeResponse(
                bundle(
                    {"resourceType": "Patient", "id": "one"},
                    {"resourceType": "Patient", "id": "two"},
                    next_url=page_two,
                )
            ),
            (page_two, ()): FakeResponse(bundle({"resourceType": "Patient", "id": "three"})),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            session = FakeSession(responses)
            report = get_all_patients(
                2, endpoint=self.endpoint, output_dir=temporary_directory, session=session
            )

        self.assertFalse(report.failures)
        self.assertEqual(report.resources["Patient"], 2)
        self.assertEqual(len(session.calls), 1)

    def test_patient_checkpoint_without_cached_resource_refetches_patient(self):
        patient_url = f"{self.endpoint}/Patient?_id=one"
        responses = {
            (patient_url, (("_count", "50"), ("_id", "one"))): FakeResponse(
                bundle({"resourceType": "Patient", "id": "one"})
            )
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint = Path(temporary_directory) / "checkpoint.json"
            checkpoint.write_text(
                json.dumps({"endpoint": f"{self.endpoint}/", "completed": ["Patient?_id=one"]})
            )
            session = FakeSession(responses)
            retriever = FHIRRetriever(
                self.endpoint, temporary_directory, patient_id="one", session=session
            )
            report = retriever.get_patient("one")
            database = sqlite3.connect(Path(temporary_directory) / "fhir_resources.sqlite3")
            saved_patient = database.execute(
                "SELECT patient_id FROM patients WHERE patient_id = 'one'"
            ).fetchone()
            database.close()
            retriever.database.close()

        self.assertFalse(report.failures)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(saved_patient, ("one",))

    def test_patient_database_uses_structured_demographic_columns(self):
        responses = self.response_map()
        responses[(f"{self.endpoint}/Patient", (("_count", "50"),))] = FakeResponse(
            bundle(
                {
                    "resourceType": "Patient",
                    "id": "one",
                    "name": [{"family": "Doe", "given": ["Jane", "Marie"]}],
                    "gender": "female",
                    "birthDate": "1980-01-02",
                }
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            retriever = FHIRRetriever(
                self.endpoint, temporary_directory, session=FakeSession(responses)
            )
            retriever.get_all_patients()
            columns = [column[1] for column in retriever.database.connection.execute("PRAGMA table_info(patients)")]
            patient = retriever.database.connection.execute(
                "SELECT patient_id, family_name, given_name, gender, birth_date FROM patients"
            ).fetchone()
            retriever.database.close()

        self.assertEqual(
            columns, ["patient_id", "family_name", "given_name", "gender", "birth_date", "date_shift_days"]
        )
        self.assertTrue(patient[0].startswith("PAT-"))
        self.assertEqual(patient[1:4], ("Doe", "Jane Marie", "female"))
        date.fromisoformat(patient[4])  # birth_date is shifted but must remain a valid ISO date

    def test_observation_database_uses_requested_fhir_columns(self):
        responses = self.response_map()
        observation_url = f"{self.endpoint}/Observation"
        responses[(observation_url, (("_count", "50"), ("patient", "one")))] = FakeResponse(
            bundle(
                {
                    "resourceType": "Observation",
                    "id": "o1",
                    "subject": {"reference": "Patient/one"},
                    "encounter": {"reference": "Encounter/e1"},
                    "category": [{"coding": [{"code": "vital-signs"}]}],
                    "code": {"coding": [{"code": "8480-6", "display": "Systolic blood pressure"}]},
                    "effectiveDateTime": "2024-01-15T09:30:00Z",
                    "issued": "2024-01-15T09:35:00Z",
                    "valueQuantity": {"value": 120, "unit": "mmHg", "code": "mm[Hg]"},
                }
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            FHIRRetriever(self.endpoint, temporary_directory, session=FakeSession(responses)).retrieve()
            database = sqlite3.connect(Path(temporary_directory) / "fhir_resources.sqlite3")
            columns = [column[1] for column in database.execute("PRAGMA table_info(observations)")]
            observation = database.execute(
                "SELECT observation_type, observation_code, observation_subtype, encounter_id, "
                "effective_date_time, issued, value, unit, value_code FROM observations"
            ).fetchone()
            database.close()

        self.assertNotIn("resource_json", columns)
        self.assertEqual(
            observation,
            ("vital-signs", "8480-6", "Systolic blood pressure", "e1", "2024-01-15T09:30:00Z", "2024-01-15T09:35:00Z", "120", "mmHg", "mm[Hg]"),
        )

    def test_patient_observation_and_encounter_function_populates_linked_tables(self):
        patient_url = f"{self.endpoint}/Patient?_id=one"
        responses = {
            (patient_url, (("_count", "50"), ("_id", "one"))): FakeResponse(
                bundle({"resourceType": "Patient", "id": "one"})
            ),
            (f"{self.endpoint}/Observation", (("_count", "50"), ("patient", "one"))): FakeResponse(
                bundle(
                    {
                        "resourceType": "Observation",
                        "id": "o1",
                        "subject": {"reference": "Patient/one"},
                        "encounter": {"reference": "Encounter/e1"},
                    }
                )
            ),
            (f"{self.endpoint}/Encounter", (("_count", "50"), ("patient", "one"))): FakeResponse(
                bundle(
                    {
                        "resourceType": "Encounter",
                        "id": "e1",
                        "identifier": [{"value": "visit-1"}],
                        "class": {"display": "ambulatory"},
                        "period": {"start": "2024-01-01", "end": "2024-01-02"},
                        "subject": {"reference": "Patient/one"},
                    }
                )
            ),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            report = get_observations_and_encounters_for_patient(
                "one",
                endpoint=self.endpoint,
                output_dir=temporary_directory,
                session=FakeSession(responses),
            )
            database = sqlite3.connect(Path(temporary_directory) / "fhir_resources.sqlite3")
            result = database.execute(
                "SELECT o.patient_id, o.encounter_id, e.patient_id "
                "FROM observations o JOIN encounters e ON e.encounter_id = o.encounter_id"
            ).fetchone()
            encounter = database.execute(
                "SELECT encounter_type, encounter_id, start, end, patient_id FROM encounters"
            ).fetchone()
            database.close()

        self.assertFalse(report.failures)
        self.assertEqual(result, ("one", "visit-1", "one"))
        self.assertEqual(encounter, ("ambulatory", "visit-1", "2024-01-01", "2024-01-02", "one"))

    def test_patient_observation_function_does_not_request_or_store_encounters(self):
        patient_url = f"{self.endpoint}/Patient?_id=one"
        responses = {
            (patient_url, (("_count", "50"), ("_id", "one"))): FakeResponse(
                bundle({"resourceType": "Patient", "id": "one"})
            ),
            (f"{self.endpoint}/Observation", (("_count", "50"), ("patient", "one"))): FakeResponse(
                bundle(
                    {
                        "resourceType": "Observation",
                        "id": "o1",
                        "subject": {"reference": "Patient/one"},
                        "encounter": {"reference": "Encounter/e1"},
                    }
                )
            ),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            session = FakeSession(responses)
            report = get_observations_for_patient(
                "one",
                endpoint=self.endpoint,
                output_dir=temporary_directory,
                session=session,
            )
            database = sqlite3.connect(Path(temporary_directory) / "fhir_resources.sqlite3")
            observation_count = database.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            encounter_count = database.execute("SELECT COUNT(*) FROM encounters").fetchone()[0]
            database.close()

        self.assertFalse(report.failures)
        self.assertEqual(observation_count, 1)
        self.assertEqual(encounter_count, 0)
        self.assertFalse(any(call[0].endswith("/Encounter") for call in session.calls))

    def test_patient_encounter_function_does_not_request_or_store_observations(self):
        patient_url = f"{self.endpoint}/Patient?_id=one"
        responses = {
            (patient_url, (("_count", "50"), ("_id", "one"))): FakeResponse(
                bundle({"resourceType": "Patient", "id": "one"})
            ),
            (f"{self.endpoint}/Encounter", (("_count", "50"), ("patient", "one"))): FakeResponse(
                bundle(
                    {
                        "resourceType": "Encounter",
                        "id": "e1",
                        "identifier": [{"value": "visit-1"}],
                        "subject": {"reference": "Patient/one"},
                    }
                )
            ),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            session = FakeSession(responses)
            report = get_encounters_for_patient(
                "one",
                endpoint=self.endpoint,
                output_dir=temporary_directory,
                session=session,
            )
            database = sqlite3.connect(Path(temporary_directory) / "fhir_resources.sqlite3")
            observation_count = database.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            encounter_count = database.execute("SELECT COUNT(*) FROM encounters").fetchone()[0]
            database.close()

        self.assertFalse(report.failures)
        self.assertEqual(observation_count, 0)
        self.assertEqual(encounter_count, 1)
        self.assertFalse(any(call[0].endswith("/Observation") for call in session.calls))

    def test_patient_condition_function_does_not_request_observations_or_encounters(self):
        patient_url = f"{self.endpoint}/Patient"
        responses = {
            (patient_url, (("_count", "50"), ("_id", "one"))): FakeResponse(
                bundle({"resourceType": "Patient", "id": "one"})
            ),
            (f"{self.endpoint}/Condition", (("_count", "50"), ("patient", "one"))): FakeResponse(
                bundle({"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/one"}})
            ),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            session = FakeSession(responses)
            report = get_conditions_for_patient(
                "one", endpoint=self.endpoint, output_dir=temporary_directory, session=session
            )

        self.assertFalse(report.failures)
        self.assertEqual(report.resources["Condition"], 1)
        self.assertFalse(any(call[0].endswith("/Observation") for call in session.calls))
        self.assertFalse(any(call[0].endswith("/Encounter") for call in session.calls))

    def test_patient_related_function_combines_condition_observation_and_encounter(self):
        patient_url = f"{self.endpoint}/Patient"
        responses = {
            (patient_url, (("_count", "50"), ("_id", "one"))): FakeResponse(
                bundle({"resourceType": "Patient", "id": "one"})
            ),
            (f"{self.endpoint}/Condition", (("_count", "50"), ("patient", "one"))): FakeResponse(bundle()),
            (f"{self.endpoint}/Observation", (("_count", "50"), ("patient", "one"))): FakeResponse(bundle()),
            (f"{self.endpoint}/Encounter", (("_count", "50"), ("patient", "one"))): FakeResponse(bundle()),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            session = FakeSession(responses)
            report = get_related_for_patient(
                "one",
                ("Condition", "Observation", "Encounter"),
                endpoint=self.endpoint,
                output_dir=temporary_directory,
                session=session,
            )

        self.assertFalse(report.failures)
        self.assertEqual(
            {call[0].rsplit("/", 1)[-1] for call in session.calls},
            {"Patient", "Condition", "Observation", "Encounter"},
        )

    def test_condition_database_uses_requested_structured_columns(self):
        responses = self.response_map()
        responses[(f"{self.endpoint}/Condition", (("_count", "50"), ("patient", "one")))] = FakeResponse(
            bundle(
                {
                    "resourceType": "Condition",
                    "id": "c1",
                    "subject": {"reference": "Patient/one"},
                    "encounter": {"reference": "Encounter/e1"},
                    "clinicalStatus": {"coding": [{"code": "active"}]},
                    "verificationStatus": {"coding": [{"code": "confirmed"}]},
                    "category": [{"coding": [{"code": "problem-list-item"}]}],
                    "code": {"coding": [{"code": "44054006", "display": "Diabetes mellitus type 2"}]},
                    "onsetDateTime": "2024-01-01",
                    "recordedDate": "2024-01-02",
                }
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            FHIRRetriever(self.endpoint, temporary_directory, session=FakeSession(responses)).retrieve()
            database = sqlite3.connect(Path(temporary_directory) / "fhir_resources.sqlite3")
            condition = database.execute(
                "SELECT clinicalStatus, verificationStatus, category, condition, condition_code, "
                "onsetDateTime, recorded_Date FROM conditions"
            ).fetchone()
            database.close()

        self.assertEqual(
            condition[:5],
            ("active", "confirmed", "problem-list-item", "Diabetes mellitus type 2", "44054006"),
        )
        self.assertNotEqual(condition[5:], ("2024-01-01", "2024-01-02"))

    def test_database_tables_are_exported_as_schema_aligned_csv_files(self):
        patient_url = f"{self.endpoint}/Patient?_id=one"
        responses = {
            (patient_url, (("_count", "50"), ("_id", "one"))): FakeResponse(
                bundle(
                    {
                        "resourceType": "Patient",
                        "id": "one",
                        "name": [{"family": "Doe", "given": ["Jane"]}],
                        "gender": "female",
                        "birthDate": "1980-01-02",
                    }
                )
            )
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            get_patient(
                "one",
                endpoint=self.endpoint,
                output_dir=temporary_directory,
                session=FakeSession(responses),
            )
            with (Path(temporary_directory) / "patients.csv").open(newline="") as file:
                patient_rows = list(csv.reader(file))
            with (Path(temporary_directory) / "observations.csv").open(newline="") as file:
                observation_rows = list(csv.reader(file))
            with (Path(temporary_directory) / "conditions.csv").open(newline="") as file:
                condition_rows = list(csv.reader(file))
            with (Path(temporary_directory) / "encounters.csv").open(newline="") as file:
                encounter_rows = list(csv.reader(file))

        self.assertEqual(
            patient_rows[0],
            ["patient_id", "family_name", "given_name", "gender", "birth_date", "date_shift_days"],
        )
        self.assertEqual(patient_rows[1][:5], ["one", "Doe", "Jane", "female", "1980-01-02"])
        self.assertEqual(
            condition_rows,
            [["clinicalStatus", "verificationStatus", "category", "condition", "condition_code", "patient_id", "encounter_id", "onsetDateTime", "recorded_Date"]],
        )
        self.assertEqual(observation_rows, [["observation_id", "patient_id", "encounter_id", "observation_type", "observation_code", "observation_subtype", "effective_date_time", "issued", "value", "unit", "value_code"]])
        self.assertEqual(encounter_rows, [["encounter_type", "encounter_id", "start", "end", "patient_id"]])


if __name__ == "__main__":
    unittest.main()