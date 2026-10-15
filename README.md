# Employee Attendance and Analytics API

FastAPI and MongoDB implementation of the HROne attendance API contract. All application code is in `app/main.py`.

## Requirements

- Python 3.11+
- MongoDB 6.0+

## Setup

From the project root, create and activate a virtual environment on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Install and start MongoDB on Windows

Check whether MongoDB is installed as a Windows service from PowerShell:

```powershell
Get-Service MongoDB -ErrorAction SilentlyContinue
```

If it exists but is stopped, start PowerShell as Administrator and run:

```powershell
Start-Service MongoDB
Get-Service MongoDB
Test-NetConnection localhost -Port 27017
```

If the service is missing, install **MongoDB Community Server 6.0 or newer** using the official Windows MSI:
[MongoDB Community Server download](https://www.mongodb.com/try/download/community). Select the Windows x64 MSI and
choose the option to install MongoDB as a Windows service. Then verify it with the commands above. Do not install a
database server from unofficial download sites.

Alternatively, use a MongoDB Atlas database. Create a disposable test database and a database user with permission to
create and drop databases. Allowlist your IP address. Keep the connection string private.

### Configure connection strings safely

The application reads `MONGO_URI` and `MONGO_DB`. These PowerShell values last only for the current terminal session:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

The integration tests use `MONGO_TEST_URI`. Point it only at a disposable test database/server; the tests create and
drop uniquely named databases. For a local server:

```powershell
$env:MONGO_TEST_URI = "mongodb://localhost:27017"
```

For Atlas, assign the private connection string in the PowerShell session without putting it in source code or chat:

```powershell
$uriSecret = Read-Host "Paste the MongoDB connection string" -AsSecureString
$uriPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($uriSecret)
try {
    $env:MONGO_URI = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($uriPointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($uriPointer)
    Remove-Variable uriSecret
}
$env:MONGO_TEST_URI = $env:MONGO_URI
```

The URI is hidden while you type; it is available in the current process environment while these commands run. Do not
use the application's important or production database as `MONGO_TEST_URI`: the integration tests drop their temporary
databases. The application loads a local `.env` if present, but real environment variables take precedence. Never commit
`.env`, credentials, or passwords. Indexes are created idempotently at application startup.

To load the provided sample records into the configured database:

```powershell
python sample_seed.py
```

The seed script replaces the two collections' contents. Use it only with a disposable local/test database.

## Run

```powershell
python -m uvicorn app.main:app --port 8000
```

The readiness endpoint is `http://127.0.0.1:8000/health`. Interactive API documentation is at
`http://127.0.0.1:8000/docs`.

## Test

```powershell
python -m pytest -q
```

The suite includes in-memory endpoint, validation, concurrency, time-calculation, and pipeline-shape tests. Those do not
prove server-side aggregation results or index usage. With MongoDB 6.0+ reachable and `MONGO_TEST_URI` configured, the
same command also runs the integration and explain-plan tests. Check the pytest summary: MongoDB tests are skipped when
`MONGO_TEST_URI` is unset.

### Latest verification status

On 2026-10-15, `python -m pytest -q` reported **29 passed and 10 skipped**. The 10 skipped tests are the MongoDB
integration tests; they were skipped because `MONGO_TEST_URI` was not configured. MongoDB-backed analytics, actual index
usage/explain plans, and a live `/health` response of HTTP 200 have **not** been verified. Do not interpret this test
run as a successful MongoDB integration run.

After MongoDB is running, start the app and verify readiness:

```powershell
python -m uvicorn app.main:app --port 8000
```

In another terminal, run `Invoke-WebRequest http://127.0.0.1:8000/health`. A 200 response means MongoDB answered the
readiness ping; a 503 means the configured MongoDB server is still unavailable.

Analytics use stored attendance-derived values, while punch operations and regularization compute them from times.
Review `openapi.yaml` for the full contract and `DECISIONS.md` for the implementation choices.
