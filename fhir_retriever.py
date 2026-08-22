"""Retrieve Patients and their Conditions and Observations from a FHIR R4 server."""

from __future__ import annotations

import argparse
import csv
import copy
import hashlib
import hmac
import gzip
import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LOGGER = logging.getLogger(__name__)
DEFAULT_ENDPOINT = "http://hapi.fhir.org/baseR4"
PATIENT_QUERY = "Patient"
PSEUDONYMIZATION_KEY_ENV = "FHIR_PSEUDONYMIZATION_KEY"


class Pseudonymizer:
    """Create stable, non-reversible HMAC identifiers and date offsets."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError(f"Set {PSEUDONYMIZATION_KEY_ENV} before running the retriever")
        self.key = key.encode("utf-8")

    def identifier(self, resource_type: str, raw_id: str) -> str:
        digest = hmac.new(self.key, f"{resource_type}:{raw_id}".encode(), hashlib.sha256).hexdigest()
        return f"{resource_type[:3].upper()}-{digest[:20]}"

    def patient_offset(self, patient_id: str) -> int:
        digest = hmac.new(self.key, f"date:{patient_id}".encode(), hashlib.sha256).digest()
        return int.from_bytes(digest[:2], "big") % 731 - 365

    def shift_date(self, value: str, offset_days: int) -> str:
        try:
            if "T" in value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return (parsed + timedelta(days=offset_days)).isoformat().replace("+00:00", "Z")
            return (date.fromisoformat(value) + timedelta(days=offset_days)).isoformat()
        except (ValueError, OverflowError):
            # Some real-world dates (e.g. near year 1 or 9999 on public test servers)
            # cannot be shifted without leaving the valid date range; keep as-is.
            return value


def _reference_id(resource: dict[str, Any], field_name: str, resource_type: str) -> str | None:
    reference = resource.get(field_name, {}).get("reference")
    if not isinstance(reference, str):
        return None
    parts = reference.rstrip("/").split("/")
    try:
        return parts[parts.index(resource_type) + 1]
    except (ValueError, IndexError):
        return None


@dataclass
class RetrievalFailure:
    query: str
    error: str


@dataclass
class RetrievalReport:
    resources: dict[str, int] = field(
        default_factory=lambda: {"Patient": 0, "Condition": 0, "Observation": 0, "Encounter": 0}
    )
    failures: list[RetrievalFailure] = field(default_factory=list)
    # Maps each explicitly requested raw Patient ID to its stored pseudonym, e.g. sindhu-syn-000004 -> PAT-....
    patient_pseudonyms: dict[str, str] = field(default_factory=dict)


class FHIRDatabase:
    """SQLite projection of the cached patient-related FHIR resources."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self._encounter_id_map: dict[str, str] = {}
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS patients (
                patient_id TEXT PRIMARY KEY,
                family_name TEXT,
                given_name TEXT,
                gender TEXT,
                birth_date TEXT,
                date_shift_days INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conditions (
                condition_id TEXT PRIMARY KEY,
                clinicalStatus TEXT,
                verificationStatus TEXT,
                category TEXT,
                condition TEXT,
                condition_code TEXT,
                patient_id TEXT REFERENCES patients(patient_id),
                encounter_id TEXT,
                onsetDateTime TEXT,
                recorded_Date TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                observation_id TEXT PRIMARY KEY,
                patient_id TEXT REFERENCES patients(patient_id),
                encounter_id TEXT,
                observation_type TEXT,
                observation_code TEXT,
                observation_subtype TEXT,
                effective_date_time TEXT,
                issued TEXT,
                value TEXT,
                unit TEXT,
                value_code TEXT
            );
            CREATE TABLE IF NOT EXISTS encounters (
                encounter_type TEXT,
                encounter_id TEXT PRIMARY KEY,
                start TEXT,
                end TEXT,
                patient_id TEXT REFERENCES patients(patient_id)
            );
            CREATE INDEX IF NOT EXISTS conditions_patient_encounter_idx
                ON conditions(patient_id, encounter_id);
            CREATE INDEX IF NOT EXISTS observations_patient_encounter_idx
                ON observations(patient_id, encounter_id);
            CREATE INDEX IF NOT EXISTS encounters_patient_idx
                ON encounters(patient_id);
            """
        )
        self._migrate_patients_table()
        self._migrate_patient_columns()
        self._migrate_conditions_table()
        self._migrate_encounters_table()
        self._migrate_observations_table()
        self._translate_existing_observation_encounter_ids()

    @staticmethod
    def _patient_columns(resource: dict[str, Any]) -> tuple[str, str | None, str | None, str | None, str | None, int]:
        return (
            resource["id"],
            resource.get("familyName"),
            resource.get("givenName"),
            resource.get("gender"),
            resource.get("birthDate"),
            resource["dateShiftDays"],
        )

    def _migrate_patients_table(self) -> None:
        columns = {column[1] for column in self.connection.execute("PRAGMA table_info(patients)")}
        if "resource_json" not in columns:
            return
        legacy_patients = self.connection.execute(
            "SELECT patient_id, resource_json FROM patients"
        ).fetchall()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.connection:
                self.connection.execute("DROP TABLE patients")
                self.connection.execute(
                    "CREATE TABLE patients ("
                    "patient_id TEXT PRIMARY KEY, family_name TEXT, given_name TEXT, "
                    "gender TEXT, birth_date TEXT, date_shift_days INTEGER NOT NULL)"
                )
                for patient_id, resource_json in legacy_patients:
                    try:
                        patient = json.loads(resource_json)
                    except json.JSONDecodeError:
                        LOGGER.warning("Skipping malformed legacy Patient JSON for %s", patient_id)
                        continue
                    names = patient.get("name", [])
                    name = names[0] if isinstance(names, list) and names and isinstance(names[0], dict) else {}
                    given_names = name.get("given", [])
                    family_name = name.get("family")
                    given_name = " ".join(given_names) if isinstance(given_names, list) else None
                    self.connection.execute(
                        "INSERT INTO patients(patient_id, family_name, given_name, gender, birth_date, date_shift_days) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (patient_id, family_name, given_name, patient.get("gender"), None, 0),
                    )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_patient_columns(self) -> None:
        columns = {column[1] for column in self.connection.execute("PRAGMA table_info(patients)")}
        if "date_shift_days" not in columns:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE patients ADD COLUMN date_shift_days INTEGER NOT NULL DEFAULT 0"
                )

    @classmethod
    def _condition_columns(cls, resource: dict[str, Any]) -> tuple[Any, ...]:
        clinical_status = cls._first_coding(resource.get("clinicalStatus", {})).get("code")
        verification_status = cls._first_coding(resource.get("verificationStatus", {})).get("code")
        categories = resource.get("category", [])
        category = cls._first_coding(categories[0] if isinstance(categories, list) and categories else {}).get("code")
        code = resource.get("code", {})
        coding = cls._first_coding(code)
        return (
            resource["id"],
            clinical_status,
            verification_status,
            category,
            coding.get("display") or code.get("text") if isinstance(code, dict) else None,
            coding.get("code"),
            _reference_id(resource, "subject", "Patient"),
            _reference_id(resource, "encounter", "Encounter"),
            resource.get("onsetDateTime"),
            resource.get("recordedDate"),
        )

    def _migrate_conditions_table(self) -> None:
        columns = {column[1] for column in self.connection.execute("PRAGMA table_info(conditions)")}
        if "resource_json" not in columns:
            return
        legacy_conditions = self.connection.execute("SELECT resource_json FROM conditions").fetchall()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.connection:
                self.connection.execute("DROP TABLE conditions")
                self.connection.execute(
                    "CREATE TABLE conditions (condition_id TEXT PRIMARY KEY, clinicalStatus TEXT, verificationStatus TEXT, category TEXT, "
                    "condition TEXT, condition_code TEXT, patient_id TEXT REFERENCES patients(patient_id), "
                    "encounter_id TEXT, onsetDateTime TEXT, recorded_Date TEXT)"
                )
                for (resource_json,) in legacy_conditions:
                    try:
                        resource = json.loads(resource_json)
                    except json.JSONDecodeError:
                        continue
                    self.connection.execute("INSERT INTO conditions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", self._condition_columns(resource))
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _first_coding(value: Any) -> dict[str, Any]:
        codings = value.get("coding", []) if isinstance(value, dict) else []
        return codings[0] if isinstance(codings, list) and codings and isinstance(codings[0], dict) else {}

    @classmethod
    def _observation_columns(cls, resource: dict[str, Any]) -> tuple[Any, ...]:
        categories = resource.get("category", [])
        category = categories[0] if isinstance(categories, list) and categories else {}
        category_coding = cls._first_coding(category)
        code = resource.get("code", {})
        code_coding = cls._first_coding(code)
        quantity = resource.get("valueQuantity", {})
        if isinstance(quantity, dict):
            value = quantity.get("value")
            unit = quantity.get("unit")
            value_code = quantity.get("code")
        else:
            value = next((resource[key] for key in resource if key.startswith("value") and key != "valueQuantity"), None)
            unit = None
            value_code = None
        return (
            resource["id"],
            _reference_id(resource, "subject", "Patient"),
            _reference_id(resource, "encounter", "Encounter"),
            category_coding.get("code"),
            code_coding.get("code"),
            code_coding.get("display") or code.get("text") if isinstance(code, dict) else None,
            resource.get("effectiveDateTime"),
            resource.get("issued"),
            str(value) if value is not None else None,
            unit,
            value_code,
        )

    def _migrate_observations_table(self) -> None:
        columns = {column[1] for column in self.connection.execute("PRAGMA table_info(observations)")}
        if "resource_json" not in columns:
            return
        legacy_observations = self.connection.execute(
            "SELECT observation_id, resource_json FROM observations"
        ).fetchall()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.connection:
                self.connection.execute("DROP TABLE observations")
                self.connection.execute(
                    "CREATE TABLE observations (observation_id TEXT PRIMARY KEY, "
                    "patient_id TEXT REFERENCES patients(patient_id), encounter_id TEXT, "
                    "observation_type TEXT, observation_code TEXT, observation_subtype TEXT, "
                    "effective_date_time TEXT, issued TEXT, value TEXT, unit TEXT, value_code TEXT)"
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS observations_patient_encounter_idx "
                    "ON observations(patient_id, encounter_id)"
                )
                for observation_id, resource_json in legacy_observations:
                    try:
                        observation = json.loads(resource_json)
                    except json.JSONDecodeError:
                        LOGGER.warning("Skipping malformed legacy Observation JSON for %s", observation_id)
                        continue
                    observation_columns = list(self._observation_columns(observation))
                    observation_columns[2] = self._encounter_id_map.get(
                        observation_columns[2], observation_columns[2]
                    )
                    self.connection.execute(
                        "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        observation_columns,
                    )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _encounter_columns(resource: dict[str, Any]) -> tuple[str | None, str, str | None, str | None, str | None]:
        identifiers = resource.get("identifier", [])
        identifier = identifiers[0] if isinstance(identifiers, list) and identifiers else {}
        encounter_id = identifier.get("value") if isinstance(identifier, dict) else None
        if not encounter_id:
            encounter_id = resource["id"]
        encounter_class = resource.get("class", {})
        period = resource.get("period", {})
        return (
            encounter_class.get("display") or encounter_class.get("code")
            if isinstance(encounter_class, dict)
            else None,
            encounter_id,
            period.get("start") if isinstance(period, dict) else None,
            period.get("end") if isinstance(period, dict) else None,
            _reference_id(resource, "subject", "Patient"),
        )

    def _migrate_encounters_table(self) -> None:
        columns = {column[1] for column in self.connection.execute("PRAGMA table_info(encounters)")}
        if "resource_json" not in columns:
            return
        legacy_encounters = self.connection.execute(
            "SELECT encounter_id, resource_json FROM encounters"
        ).fetchall()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.connection:
                self.connection.execute("DROP TABLE encounters")
                self.connection.execute(
                    "CREATE TABLE encounters (encounter_type TEXT, encounter_id TEXT PRIMARY KEY, "
                    "start TEXT, end TEXT, patient_id TEXT REFERENCES patients(patient_id))"
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS encounters_patient_idx ON encounters(patient_id)"
                )
                for encounter_id, resource_json in legacy_encounters:
                    try:
                        encounter = json.loads(resource_json)
                    except json.JSONDecodeError:
                        LOGGER.warning("Skipping malformed legacy Encounter JSON for %s", encounter_id)
                        continue
                    encounter_columns = self._encounter_columns(encounter)
                    self._encounter_id_map[encounter["id"]] = encounter_columns[1]
                    self.connection.execute(
                        "INSERT INTO encounters(encounter_type, encounter_id, start, end, patient_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        encounter_columns,
                    )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def _translate_existing_observation_encounter_ids(self) -> None:
        if not self._encounter_id_map:
            return
        with self.connection:
            for fhir_encounter_id, encounter_id in self._encounter_id_map.items():
                self.connection.execute(
                    "UPDATE observations SET encounter_id = ? WHERE encounter_id = ?",
                    (encounter_id, fhir_encounter_id),
                )

    def sync(self, resources: Iterable[dict[str, Any]]) -> None:
        grouped = {"Patient": [], "Condition": [], "Observation": [], "Encounter": []}
        for resource in resources:
            resource_type = resource.get("resourceType")
            if resource_type in grouped and resource.get("id"):
                grouped[resource_type].append(resource)

        with self.connection:
            for resource in grouped["Patient"]:
                self.connection.execute(
                    "INSERT INTO patients(patient_id, family_name, given_name, gender, birth_date, date_shift_days) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(patient_id) DO UPDATE SET "
                    "family_name = excluded.family_name, given_name = excluded.given_name, "
                    "gender = excluded.gender, birth_date = excluded.birth_date, "
                    "date_shift_days = excluded.date_shift_days",
                    self._patient_columns(resource),
                )
            for resource_type, table_name, id_column in (
                ("Encounter", "encounters", "encounter_id"),
                ("Condition", "conditions", "condition_id"),
                ("Observation", "observations", "observation_id"),
            ):
                for resource in grouped[resource_type]:
                    patient_id = _reference_id(resource, "subject", "Patient")
                    resource_json = json.dumps(resource, separators=(",", ":"), sort_keys=True)
                    if resource_type == "Encounter":
                        self.connection.execute(
                            "INSERT INTO encounters(encounter_type, encounter_id, start, end, patient_id) "
                            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(encounter_id) DO UPDATE SET "
                            "encounter_type = excluded.encounter_type, start = excluded.start, "
                            "end = excluded.end, patient_id = excluded.patient_id",
                            self._encounter_columns(resource),
                        )
                        self.connection.execute(
                            "UPDATE observations SET encounter_id = ? WHERE encounter_id = ?",
                            (self._encounter_columns(resource)[1], resource["id"]),
                        )
                    else:
                        if resource_type == "Observation":
                            self.connection.execute(
                                "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                                "ON CONFLICT(observation_id) DO UPDATE SET "
                                "patient_id = excluded.patient_id, encounter_id = excluded.encounter_id, "
                                "observation_type = excluded.observation_type, "
                                "observation_code = excluded.observation_code, "
                                "observation_subtype = excluded.observation_subtype, "
                                "effective_date_time = excluded.effective_date_time, issued = excluded.issued, "
                                "value = excluded.value, unit = excluded.unit, value_code = excluded.value_code",
                                self._observation_columns(resource),
                            )
                            continue
                        if resource_type == "Condition":
                            self.connection.execute(
                                "INSERT INTO conditions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                                "ON CONFLICT(condition_id) DO UPDATE SET "
                                "clinicalStatus = excluded.clinicalStatus, "
                                "verificationStatus = excluded.verificationStatus, category = excluded.category, "
                                "condition = excluded.condition, condition_code = excluded.condition_code, "
                                "patient_id = excluded.patient_id, encounter_id = excluded.encounter_id, "
                                "onsetDateTime = excluded.onsetDateTime, recorded_Date = excluded.recorded_Date",
                                self._condition_columns(resource),
                            )
                            continue
                        self.connection.execute(
                            f"INSERT INTO {table_name}({id_column}, patient_id, encounter_id, resource_json) "
                            f"VALUES (?, ?, ?, ?) ON CONFLICT({id_column}) DO UPDATE SET "
                            "patient_id = excluded.patient_id, encounter_id = excluded.encounter_id, "
                            "resource_json = excluded.resource_json",
                            (
                                resource["id"],
                                patient_id,
                                _reference_id(resource, "encounter", "Encounter"),
                                resource_json,
                            ),
                        )

    def close(self) -> None:
        self.connection.close()

    def export_csv(self, output_dir: str | Path) -> None:
        """Overwrite CSV exports from the current SQLite tables (atomic, always fully rewritten)."""
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        for table_name in ("patients", "conditions", "observations", "encounters"):
            columns = [column[1] for column in self.connection.execute(f"PRAGMA table_info({table_name})")]
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            rows = self.connection.execute(f"SELECT {quoted_columns} FROM {table_name}")
            target_path = destination / f"{table_name}.csv"
            temporary_path = target_path.with_suffix(".csv.tmp")
            with temporary_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(columns)
                writer.writerows(rows)
            temporary_path.replace(target_path)


class FHIRRetriever:
    """Download patient data while retaining completed work across process runs.

    Resources are deduplicated in the compact ``resources.json.gz`` cache. Every
    completed search is checkpointed, so a later run uses local data and retries
    only incomplete searches. Pass ``refresh=True`` to intentionally re-query.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        output_dir: str | Path = "fhir_output",
        timeout: tuple[float, float] = (5.0, 30.0),
        retries: int = 2,
        page_size: int = 50,
        patient_limit: int | None = None,
        refresh: bool = False,
        database_path: str | Path | None = None,
        patient_id: str | None = None,
        pseudonymization_key: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if retries < 0:
            raise ValueError("retries must be non-negative")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if patient_limit is not None and patient_limit < 1:
            raise ValueError("patient_limit must be positive")
        self.endpoint = endpoint.rstrip("/") + "/"
        self.output_dir = Path(output_dir)
        self.timeout = timeout
        self.page_size = page_size
        self.patient_limit = patient_limit
        self.refresh = refresh
        self.patient_id = patient_id
        self.pseudonymizer = Pseudonymizer(
            pseudonymization_key or os.environ.get(PSEUDONYMIZATION_KEY_ENV, "")
        )
        self._raw_patient_ids: dict[str, str] = {}
        self.session = session or self._make_session(retries)
        self.resources_path = self.output_dir / "resources.json.gz"
        self.checkpoint_path = self.output_dir / "checkpoint.json"
        self.database_path = Path(database_path) if database_path else self.output_dir / "fhir_resources.sqlite3"
        loaded_resources = self._load_json(self.resources_path, {})
        self._resources = {
            key: value if value.get("_deidentified") else self._pseudonymize_resource(value)
            for key, value in loaded_resources.items()
        }
        checkpoint = self._load_json(self.checkpoint_path, {"endpoint": self.endpoint, "completed": []})
        if checkpoint["endpoint"] != self.endpoint:
            raise ValueError(f"Cache belongs to {checkpoint['endpoint']}; choose another output directory")
        completed = set() if refresh else set(checkpoint["completed"])
        self._completed = {
            query
            for query in completed
            if query == PATIENT_QUERY or "?patient=PAT-" in query or "?_id=PAT-" in query
        }
        self.database = FHIRDatabase(self.database_path)
        self.database.sync(self._resources.values())
        self._repair_encounter_links(loaded_resources)
        if loaded_resources and loaded_resources != self._resources:
            self._save_resources()
        if self._completed != completed:
            self._save_checkpoint()

    def _patient_pseudonym(self, raw_patient_id: str) -> str:
        return self.pseudonymizer.identifier("Patient", raw_patient_id)

    def _sync_single_patient_scope(self, patient_id: str) -> None:
        """Restrict the SQLite/CSV output to only this Patient's resources."""
        target = self._patient_pseudonym(patient_id)
        with self.database.connection:
            # Delete children before the parent Patient row to satisfy foreign keys.
            for table in ("conditions", "observations", "encounters"):
                self.database.connection.execute(f"DELETE FROM {table} WHERE patient_id != ?", (target,))
            self.database.connection.execute("DELETE FROM patients WHERE patient_id != ?", (target,))
        scoped_resources = [
            resource
            for resource in self._resources.values()
            if resource.get("id") == target
            or (resource.get("subject") or {}).get("reference") == f"Patient/{target}"
        ]
        self.database.sync(scoped_resources)

    def _repair_encounter_links(self, resources: dict[str, dict[str, Any]]) -> None:
        """Migrate old Observation links using Encounter IDs in the local cache."""
        for encounter in resources.values():
            if encounter.get("resourceType") != "Encounter" or not encounter.get("id"):
                continue
            identifiers = encounter.get("identifier", [])
            identifier = identifiers[0] if isinstance(identifiers, list) and identifiers else {}
            stored_encounter_id = identifier.get("value") if isinstance(identifier, dict) else None
            if stored_encounter_id:
                for legacy_encounter_id in (encounter["id"], f"Encounter/{encounter['id']}"):
                    self.database.connection.execute(
                        "UPDATE observations SET encounter_id = ? WHERE encounter_id = ?",
                        (stored_encounter_id, legacy_encounter_id),
                    )
        self.database.connection.commit()

    def _pseudonymize_resource(self, resource: dict[str, Any]) -> dict[str, Any]:
        if resource.get("_deidentified"):
            return resource
        result = copy.deepcopy(resource)
        resource_type = result["resourceType"]
        raw_id = result["id"]
        patient_raw_id = raw_id if resource_type == "Patient" else _reference_id(result, "subject", "Patient")
        patient_offset = self.pseudonymizer.patient_offset(patient_raw_id) if patient_raw_id else 0
        result["id"] = self.pseudonymizer.identifier(resource_type, raw_id)
        if resource_type == "Patient":
            self._raw_patient_ids[result["id"]] = raw_id
            names = result.get("name", [])
            name = names[0] if isinstance(names, list) and names and isinstance(names[0], dict) else {}
            given_names = name.get("given", [])
            result["familyName"] = name.get("family")
            result["givenName"] = " ".join(given_names) if isinstance(given_names, list) else None
            result.pop("name", None)
            result.pop("identifier", None)
            result.pop("telecom", None)
            result.pop("address", None)
            result.pop("contact", None)
            result.pop("communication", None)
            result["dateShiftDays"] = patient_offset
            if isinstance(result.get("birthDate"), str):
                result["birthDate"] = self.pseudonymizer.shift_date(result["birthDate"], patient_offset)
        elif patient_raw_id:
            result["subject"] = {"reference": f"Patient/{self._patient_pseudonym(patient_raw_id)}"}
        if "encounter" in result:
            raw_encounter_id = _reference_id(result, "encounter", "Encounter")
            if raw_encounter_id:
                result["encounter"] = {
                    "reference": f"Encounter/{self.pseudonymizer.identifier('Encounter', raw_encounter_id)}"
                }
        for field_name in ("effectiveDateTime", "issued"):
            if isinstance(result.get(field_name), str):
                result[field_name] = self.pseudonymizer.shift_date(result[field_name], patient_offset)
        if resource_type == "Encounter":
            result.pop("identifier", None)
            if isinstance(result.get("period"), dict):
                for field_name in ("start", "end"):
                    if isinstance(result["period"].get(field_name), str):
                        result["period"][field_name] = self.pseudonymizer.shift_date(
                            result["period"][field_name], patient_offset
                        )
        if resource_type == "Condition":
            result.pop("note", None)
            for field_name in ("onsetDateTime", "recordedDate"):
                if isinstance(result.get(field_name), str):
                    result[field_name] = self.pseudonymizer.shift_date(result[field_name], patient_offset)
        result["_deidentified"] = True
        return result

    @staticmethod
    def _make_session(retries: int) -> requests.Session:
        retry_policy = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=1.0,
            backoff_max=60,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        session = requests.Session()
        session.headers["User-Agent"] = "fhir-resource-retriever/1.0 (polite cache-first client)"
        adapter = HTTPAdapter(max_retries=retry_policy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        if path.suffix == ".gz":
            file_context = gzip.open(path, "rt", encoding="utf-8")
        else:
            file_context = path.open(encoding="utf-8")
        with file_context as file:
            return json.load(file)

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        if path.suffix == ".gz":
            file_context = gzip.open(temporary_path, "wt", encoding="utf-8")
        else:
            file_context = temporary_path.open("w", encoding="utf-8")
        with file_context as file:
            json.dump(value, file, separators=(",", ":"), sort_keys=True)
            file.write("\n")
        temporary_path.replace(path)

    def _save_resources(self) -> None:
        self._write_json(self.resources_path, self._resources)

    def _save_checkpoint(self) -> None:
        self._write_json(
            self.checkpoint_path,
            {"endpoint": self.endpoint, "completed": sorted(self._completed)},
        )

    def _remember(self, resources: Iterable[dict[str, Any]], report: RetrievalReport) -> None:
        changed = False
        valid_resources = []
        for resource in resources:
            resource = self._pseudonymize_resource(resource)
            resource_type = resource.get("resourceType")
            resource_id = resource.get("id")
            if not resource_type or not resource_id:
                LOGGER.warning("Skipping resource without resourceType and id: %r", resource)
                continue
            valid_resources.append(resource)
            key = f"{resource_type}/{resource_id}"
            if key not in self._resources:
                report.resources.setdefault(resource_type, 0)
                report.resources[resource_type] += 1
                changed = True
            elif self._resources[key] != resource:
                changed = True
            self._resources[key] = resource
        self.database.sync(valid_resources)
        if changed:
            self._save_resources()
        if valid_resources:
            self.database.export_csv(self.output_dir)

    def _fetch_pages(
        self,
        path_or_url: str,
        params: dict[str, str] | None,
        report: RetrievalReport,
        query_name: str | None = None,
        max_resources: int | None = None,
    ) -> bool:
        url = urljoin(self.endpoint, path_or_url)
        request_params = params
        received_resources = 0
        while url:
            try:
                response = self.session.get(url, params=request_params, timeout=self.timeout)
                response.raise_for_status()
                bundle = response.json()
                if bundle.get("resourceType") != "Bundle":
                    raise ValueError("FHIR search response was not a Bundle")
                entries = bundle.get("entry", [])
                if max_resources is not None:
                    remaining = max_resources - received_resources
                    entries = entries[:remaining]
                self._remember(
                    (entry["resource"] for entry in entries if "resource" in entry), report
                )
                received_resources += len(entries)
                if max_resources is not None and received_resources >= max_resources:
                    return True
                url = next(
                    (link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"),
                    None,
                )
                request_params = None
            except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                failed_query = query_name or path_or_url
                report.failures.append(RetrievalFailure(query=failed_query, error=str(error)))
                LOGGER.warning("FHIR request failed for %s: %s", failed_query, error)
                return False
        return True

    def _load_patients(
        self, report: RetrievalReport, patient_id: str | None, limit: int | None = None
    ) -> bool:
        query = (
            f"Patient?limit={limit}"
            if patient_id is None and limit is not None
            else PATIENT_QUERY if patient_id is None else f"Patient?_id={self._patient_pseudonym(patient_id)}"
        )
        count = min(self.page_size, limit) if limit is not None else self.page_size
        params = {"_count": str(count)}
        if patient_id is not None:
            params["_id"] = patient_id
        if query in self._completed:
            if patient_id is None or f"Patient/{self._patient_pseudonym(patient_id)}" in self._resources:
                if patient_id is not None:
                    report.patient_pseudonyms[patient_id] = self._patient_pseudonym(patient_id)
                    self._sync_single_patient_scope(patient_id)
                return True
            # A checkpoint without the requested cached Patient cannot satisfy a rerun.
            self._completed.remove(query)
            self._save_checkpoint()
        if not self._fetch_pages(PATIENT_QUERY, params, report, query, limit):
            return False
        self._completed.add(query)
        self._save_checkpoint()
        if patient_id is not None:
            report.patient_pseudonyms[patient_id] = self._patient_pseudonym(patient_id)
            self._sync_single_patient_scope(patient_id)
        return True

    def _patient_ids(self, patient_id: str | None) -> list[str]:
        if patient_id is not None:
            return [patient_id] if f"Patient/{self._patient_pseudonym(patient_id)}" in self._resources else []
        return sorted(self._raw_patient_ids.values())

    def _load_related(
        self, patient_ids: Iterable[str], resource_types: Iterable[str], report: RetrievalReport
    ) -> None:
        for current_patient_id in patient_ids:
            for resource_type in resource_types:
                query = f"{resource_type}?patient={self._patient_pseudonym(current_patient_id)}"
                if query in self._completed:
                    continue
                if self._fetch_pages(
                    resource_type,
                    {"patient": current_patient_id, "_count": str(self.page_size)},
                    report,
                    query,
                ):
                    self._completed.add(query)
                    self._save_checkpoint()

    def get_all_patients(self, limit: int | None = None) -> RetrievalReport:
        """Retrieve all Patient resources and store them in the local cache and database."""
        report = RetrievalReport()
        self._load_patients(report, None, limit or self.patient_limit)
        return report

    def get_patient(self, patient_id: str) -> RetrievalReport:
        """Retrieve one Patient by ID, even when it is not yet in the local database."""
        report = RetrievalReport()
        self._load_patients(report, patient_id)
        return report

    def get_all_observations_and_encounters(self) -> RetrievalReport:
        """Retrieve all Patients, then all of their Observation and Encounter resources."""
        report = RetrievalReport()
        if self._load_patients(report, None):
            self._load_related(self._patient_ids(None), ("Observation", "Encounter"), report)
        return report

    def get_all_observations(self) -> RetrievalReport:
        """Retrieve all Patients, then all of their Observation resources only."""
        report = RetrievalReport()
        if self._load_patients(report, None):
            self._load_related(self._patient_ids(None), ("Observation",), report)
        return report

    def get_all_encounters(self) -> RetrievalReport:
        """Retrieve all Patients, then all of their Encounter resources only."""
        report = RetrievalReport()
        if self._load_patients(report, None):
            self._load_related(self._patient_ids(None), ("Encounter",), report)
        return report

    def get_all_conditions(self) -> RetrievalReport:
        """Retrieve all Patients, then all of their Condition resources only."""
        report = RetrievalReport()
        if self._load_patients(report, None):
            self._load_related(self._patient_ids(None), ("Condition",), report)
        return report

    def get_related_for_all_patients(self, resource_types: Iterable[str]) -> RetrievalReport:
        """Retrieve all Patients and the selected related resource types."""
        report = RetrievalReport()
        if self._load_patients(report, None):
            self._load_related(self._patient_ids(None), resource_types, report)
        return report

    def get_observations_and_encounters_for_patient(self, patient_id: str) -> RetrievalReport:
        """Retrieve one Patient and only that Patient's Observation and Encounter resources."""
        report = RetrievalReport()
        if self._load_patients(report, patient_id):
            self._load_related(self._patient_ids(patient_id), ("Observation", "Encounter"), report)
        return report

    def get_observations_for_patient(self, patient_id: str) -> RetrievalReport:
        """Retrieve one Patient and only that Patient's Observation resources."""
        report = RetrievalReport()
        if self._load_patients(report, patient_id):
            self._load_related(self._patient_ids(patient_id), ("Observation",), report)
        return report

    def get_encounters_for_patient(self, patient_id: str) -> RetrievalReport:
        """Retrieve one Patient and only that Patient's Encounter resources."""
        report = RetrievalReport()
        if self._load_patients(report, patient_id):
            self._load_related(self._patient_ids(patient_id), ("Encounter",), report)
        return report

    def get_conditions_for_patient(self, patient_id: str) -> RetrievalReport:
        """Retrieve one Patient and only that Patient's Condition resources."""
        report = RetrievalReport()
        if self._load_patients(report, patient_id):
            self._load_related(self._patient_ids(patient_id), ("Condition",), report)
        return report

    def get_related_for_patient(
        self, patient_id: str, resource_types: Iterable[str]
    ) -> RetrievalReport:
        """Retrieve one Patient and the selected related resource types."""
        report = RetrievalReport()
        if self._load_patients(report, patient_id):
            self._load_related(self._patient_ids(patient_id), resource_types, report)
        return report

    def retrieve(self) -> RetrievalReport:
        """Fetch Patients, then their missing Condition and Observation searches."""
        report = RetrievalReport()
        try:
            if self._load_patients(report, self.patient_id):
                self._load_related(self._patient_ids(self.patient_id), ("Condition", "Observation"), report)
            return report
        finally:
            self.database.export_csv(self.output_dir)
            self.database.close()


def _run_operation(operation: str, *operation_args: str, **retriever_options: Any) -> RetrievalReport:
    retriever = FHIRRetriever(**retriever_options)
    try:
        return getattr(retriever, operation)(*operation_args)
    finally:
        retriever.database.export_csv(retriever.output_dir)
        retriever.database.close()


def get_all_patients(limit: int | None = None, **retriever_options: Any) -> RetrievalReport:
    """Retrieve every Patient resource and store it in the local SQLite database."""
    return _run_operation("get_all_patients", limit, **retriever_options)


def get_patient(patient_id: str, **retriever_options: Any) -> RetrievalReport:
    """Retrieve one Patient by ID and store it even if it is not already cached."""
    return _run_operation("get_patient", patient_id, **retriever_options)


def get_all_observations_and_encounters(**retriever_options: Any) -> RetrievalReport:
    """Retrieve all Patients and each Patient's Observation and Encounter resources."""
    return _run_operation("get_all_observations_and_encounters", **retriever_options)


def get_all_observations(**retriever_options: Any) -> RetrievalReport:
    """Retrieve all Patients and each Patient's Observation resources only."""
    return _run_operation("get_all_observations", **retriever_options)


def get_all_encounters(**retriever_options: Any) -> RetrievalReport:
    """Retrieve all Patients and each Patient's Encounter resources only."""
    return _run_operation("get_all_encounters", **retriever_options)


def get_all_conditions(**retriever_options: Any) -> RetrievalReport:
    """Retrieve all Patients and each Patient's Condition resources only."""
    return _run_operation("get_all_conditions", **retriever_options)


def get_related_for_all_patients(
    resource_types: Iterable[str], **retriever_options: Any
) -> RetrievalReport:
    """Retrieve all Patients and selected Condition, Observation, and Encounter resources."""
    return _run_operation("get_related_for_all_patients", resource_types, **retriever_options)


def get_observations_and_encounters_for_patient(
    patient_id: str, **retriever_options: Any
) -> RetrievalReport:
    """Retrieve one Patient's Observation and Encounter resources."""
    return _run_operation("get_observations_and_encounters_for_patient", patient_id, **retriever_options)


def get_observations_for_patient(patient_id: str, **retriever_options: Any) -> RetrievalReport:
    """Retrieve one Patient's Observation resources only."""
    return _run_operation("get_observations_for_patient", patient_id, **retriever_options)


def get_encounters_for_patient(patient_id: str, **retriever_options: Any) -> RetrievalReport:
    """Retrieve one Patient's Encounter resources only."""
    return _run_operation("get_encounters_for_patient", patient_id, **retriever_options)


def get_conditions_for_patient(patient_id: str, **retriever_options: Any) -> RetrievalReport:
    """Retrieve one Patient's Condition resources only."""
    return _run_operation("get_conditions_for_patient", patient_id, **retriever_options)


def get_related_for_patient(
    patient_id: str, resource_types: Iterable[str], **retriever_options: Any
) -> RetrievalReport:
    """Retrieve one Patient and selected Condition, Observation, and Encounter resources."""
    return _run_operation("get_related_for_patient", patient_id, resource_types, **retriever_options)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", default="fhir_output")
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--read-timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--limit", type=int, help="Maximum number of Patients to retrieve with --all-patients")
    parser.add_argument("--refresh", action="store_true", help="Deliberately refresh the local cache")
    parser.add_argument("--database", help="SQLite database path (default: OUTPUT_DIR/fhir_resources.sqlite3)")
    parser.add_argument(
        "--observation",
        action="store_true",
        help="Retrieve Observations only, without Encounter resources",
    )
    parser.add_argument(
        "--encounter",
        action="store_true",
        help="Retrieve Encounters only, without Observation resources",
    )
    parser.add_argument("--condition", action="store_true", help="Retrieve Conditions only")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--all-patients", action="store_true", help="Retrieve and store all Patients")
    actions.add_argument("--patient-id", help="Retrieve and store one Patient by FHIR ID")
    actions.add_argument(
        "--all-observations-encounters",
        action="store_true",
        help="Retrieve all Patients plus their Observations and Encounters",
    )
    actions.add_argument(
        "--patient-observations-encounters",
        metavar="PATIENT_ID",
        help="Retrieve one Patient plus its Observations and Encounters",
    )
    arguments = parser.parse_args()

    options = {
        "endpoint": arguments.endpoint,
        "output_dir": arguments.output_dir,
        "timeout": (arguments.connect_timeout, arguments.read_timeout),
        "retries": arguments.retries,
        "page_size": arguments.page_size,
        "patient_limit": arguments.limit,
        "refresh": arguments.refresh,
        "database_path": arguments.database,
    }
    resource_types = tuple(
        resource_type
        for enabled, resource_type in (
            (arguments.condition, "Condition"),
            (arguments.observation, "Observation"),
            (arguments.encounter, "Encounter"),
        )
        if enabled
    )
    if arguments.all_patients:
        if resource_types:
            report = get_related_for_all_patients(resource_types, **options)
        else:
            report = get_all_patients(arguments.limit, **options)
    elif arguments.patient_id:
        if resource_types:
            report = get_related_for_patient(arguments.patient_id, resource_types, **options)
        else:
            report = get_patient(arguments.patient_id, **options)
    elif arguments.all_observations_encounters:
        if arguments.observation or arguments.encounter:
            parser.error("resource modifiers cannot be combined with --all-observations-encounters")
        report = get_all_observations_and_encounters(**options)
    elif arguments.patient_observations_encounters:
        if resource_types:
            report = get_related_for_patient(
                arguments.patient_observations_encounters, resource_types, **options
            )
        else:
            report = get_observations_and_encounters_for_patient(
                arguments.patient_observations_encounters, **options
            )
    else:
        report = FHIRRetriever(**options).retrieve()
    print(json.dumps(asdict(report), indent=2))
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())