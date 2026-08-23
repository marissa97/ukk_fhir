# FHIR Resource Retriever

Retrieves Patient, Observation, and Encounter resources from the HAPI FHIR R4 endpoint and writes de-identified SQLite and CSV outputs. The data will then be available through the provided API. 

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Install Docker
```bash
sudo dnf install podman podman-docker podman-compose
```

Save the secret key for HMAC in .env or run in commandline
```
cp .env.example .env
# Edit .env and set FHIR_PSEUDONYMIZATION_KEY to a private secret.
```
With Docker: `cp .env.example .env && podman compose up --build`. The API is then available at `http://localhost:8000`, with OpenAPI documentation at `http://localhost:8000/docs`. The retriever uses `.env` for local runs too; an exported `FHIR_PSEUDONYMIZATION_KEY` takes precedence.

## Source And Cohort

The source endpoint is `http://hapi.fhir.org/baseR4`, the public HAPI FHIR R4 server. The project retrieves and stores four main FHIR resources:

- Patient – Contains patient demographics such as pseudonymized ID, gender, and shifted birth date. Used to identify and group patient data.
- Observation – Contains clinical measurements and results such as blood pressure, heart rate, or laboratory values. Used for statistical analysis.
- Encounter – Represents a healthcare visit or interaction. Used to provide context for observations and conditions.
- Condition – Represents a patient's diagnosis or health condition, including its status, code, and dates.

## Architecture

```text
HAPI FHIR R4 -> etl (retrieve + pseudonymize + load) -> shared data volume -> Save data in SQLite + CSV 

shared data volume -> FastAPI -> http://localhost:8000/docs
```

Compose starts `db` for the persistent volume, runs `etl` to completion, then starts `api`. For a local non-Docker run, execute `python -m fhir_retriever ...` and start the API with `uvicorn fhir_api:app --port 8000`.



## Pseudonymization

`FHIR_PSEUDONYMIZATION_KEY` is used as the HMAC secret to create stable `PAT-`, `OBS-`, `ENC-`, and Condition pseudonyms. It preserves joins between Patient, Observation, Condition, and Encounter rows. Names, contact details, addresses, and source identifiers are removed; Patient birth dates and clinical dates are shifted consistently per Patient.

This reduces direct-identification risk but is not anonymization. It does not protect against a party holding the secret, external linkage attacks, or rare combinations of clinical facts. Keep the secret and stored outputs access-controlled.

Conditions are stored as `clinicalStatus`, `verificationStatus`, `category`, `condition`, `condition_code`, `patient_id`, `encounter_id`, `onsetDateTime`, and `recorded_Date`.

## Tests

```bash
python -m unittest -v test_fhir_retriever.py
```


## API

`GET /health`, `GET /patients`, `GET /patients/{patient_id}`, `GET /observations`, and `GET /analysis/observations` are available. Collection endpoints support `limit` and `offset`; Observations support `patient_id`, `observation_code`, and `encounter_id` filters.

`GET /patients` fetches live from `http://hapi.fhir.org/baseR4` whenever the local database has fewer Patients than `offset + limit` (equivalent to `fhir_retriever --all-patients --limit N`), then serves the page from SQLite. Pass `refresh=true` to force a fresh fetch even if enough Patients are already cached:

```bash
curl 'http://localhost:8000/patients?limit=10'
curl 'http://localhost:8000/patients?limit=10&refresh=true'
```

Example API request after Docker starts:

```bash
curl http://localhost:8000/health
curl 'http://localhost:8000/observations?limit=20'
```

The output of analysis includes count, mean, median, standard deviation, minimum, and maximum for each group. 