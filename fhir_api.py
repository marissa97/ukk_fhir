"""HTTP API for the de-identified FHIR SQLite database."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from fhir_analyse import analyse


DATABASE_PATH = Path(os.environ.get("FHIR_DATABASE_PATH", "fhir_output/fhir_resources.sqlite3"))
app = FastAPI(title="De-identified FHIR API", version="1.0.0")


def connection() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DATABASE_PATH)
    database.row_factory = sqlite3.Row
    return database


def collection(table: str, limit: int, offset: int) -> dict:
    database = connection()
    try:
        total = database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        rows = database.execute(f"SELECT * FROM {table} LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"total": total, "limit": limit, "offset": offset, "items": [dict(row) for row in rows]}
    except sqlite3.OperationalError as error:
        raise HTTPException(status_code=503, detail="Database is not ready") from error
    finally:
        database.close()


def _without_names(row: dict) -> dict:
    """Drop private Patient fields from an API response."""
    row.pop("family_name", None)
    row.pop("given_name", None)
    row.pop("date_shift_days", None)
    return row


@app.get("/health")
def health() -> dict:
    database = connection()
    try:
        database.execute("SELECT 1")
        return {"status": "ok"}
    finally:
        database.close()


@app.get("/patients")
def patients(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    result = collection("patients", limit, offset)
    result["items"] = [_without_names(item) for item in result["items"]]
    return result


@app.get("/patients/{patient_id}")
def patient(patient_id: str) -> dict:
    database = connection()
    try:
        row = database.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Patient not found")
        return _without_names(dict(row))
    except sqlite3.OperationalError as error:
        raise HTTPException(status_code=503, detail="Database is not ready") from error
    finally:
        database.close()


@app.get("/observations")
def observations(
    patient_id: str | None = None,
    observation_code: str | None = None,
    encounter_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    filters, parameters = [], []
    for column, value in (("patient_id", patient_id), ("observation_code", observation_code), ("encounter_id", encounter_id)):
        if value:
            filters.append(f"{column} = ?")
            parameters.append(value)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    database = connection()
    try:
        total = database.execute(f"SELECT COUNT(*) FROM observations{where}", parameters).fetchone()[0]
        rows = database.execute(
            f"SELECT * FROM observations{where} LIMIT ? OFFSET ?", [*parameters, limit, offset]
        ).fetchall()
        return {"total": total, "limit": limit, "offset": offset, "items": [dict(row) for row in rows]}
    except sqlite3.OperationalError as error:
        raise HTTPException(status_code=503, detail="Database is not ready") from error
    finally:
        database.close()


@app.get("/analysis/observations")
def observation_analysis(
    observation: str = Query(min_length=1),
    group_by: str = Query("age-band", pattern="^(age-band|sex|encounter-type)$"),
    patient_id: str | None = None,
) -> dict:
    try:
        return analyse(DATABASE_PATH, observation, patient_id, group_by)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error