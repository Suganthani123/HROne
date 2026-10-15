from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app import main


UTC = timezone.utc


def epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def matches(document, query):
    for key, expected in query.items():
        value = document.get(key)
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                if operator == "$exists":
                    if (key in document) != operand:
                        return False
                elif operator == "$type":
                    if operand == "date" and not isinstance(value, datetime):
                        return False
                elif operator == "$in":
                    if value not in operand:
                        return False
                elif operator == "$gte" and (value is None or value < operand):
                    return False
                elif operator == "$lte" and (value is None or value > operand):
                    return False
                elif operator == "$gt" and (value is None or value <= operand):
                    return False
                elif operator == "$lt" and (value is None or value >= operand):
                    return False
        elif value != expected:
            return False
    return True


class Cursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, keys):
        for field, direction in reversed(keys):
            self.documents.sort(
                key=lambda item: item.get(field) or "",
                reverse=direction < 0,
            )
        return self

    def skip(self, amount):
        self.documents = self.documents[amount:]
        return self

    def limit(self, amount):
        self.documents = self.documents[:amount]
        return self

    def __iter__(self):
        return iter(deepcopy(self.documents))


class MemoryCollection:
    def __init__(self, name):
        self.name = name
        self.documents = []
        self.lock = Lock()
        self.indexes = []
        self.aggregate_result = []
        self.read_barrier = None
        self.aggregate_calls = []

    def create_index(self, keys, **options):
        name = options.get("name", "memory_index")
        self.indexes = [
            index for index in self.indexes if index[1].get("name") != name
        ]
        self.indexes.append((keys, options))
        return name

    def insert_one(self, document):
        with self.lock:
            unique = ("emp_code",) if self.name == "employees" else ("emp_code", "date")
            if any(
                all(existing.get(field) == document.get(field) for field in unique)
                for existing in self.documents
            ):
                raise DuplicateKeyError("duplicate key")
            self.documents.append(deepcopy(document))
        return type("InsertResult", (), {"inserted_id": "memory-id"})()

    def find_one(self, query, projection=None, sort=None):
        with self.lock:
            found = [deepcopy(item) for item in self.documents if matches(item, query)]
        if sort:
            for field, direction in reversed(sort):
                found.sort(
                    key=lambda item: item.get(field) or "",
                    reverse=direction < 0,
                )
        result = found[0] if found else None
        if result is not None and self.read_barrier is not None:
            self.read_barrier.wait(timeout=5)
        return result

    def find(self, query, projection=None):
        with self.lock:
            return Cursor(
                [deepcopy(item) for item in self.documents if matches(item, query)]
            )

    def count_documents(self, query):
        with self.lock:
            return sum(matches(item, query) for item in self.documents)

    def find_one_and_update(self, query, update, return_document=None):
        with self.lock:
            for document in self.documents:
                if not matches(document, query):
                    continue
                document.update(deepcopy(update.get("$set", {})))
                for key, value in update.get("$push", {}).items():
                    document.setdefault(key, []).append(deepcopy(value))
                return deepcopy(document)
        return None

    def aggregate(self, pipeline, **options):
        self.aggregate_calls.append((deepcopy(pipeline), deepcopy(options)))
        return iter(deepcopy(self.aggregate_result))


class MemoryAdmin:
    def command(self, name, *args, **kwargs):
        if name == "ping":
            return {"ok": 1}
        return {"ok": 1}


class MemoryDatabase:
    def __init__(self):
        self.employees = MemoryCollection("employees")
        self.attendance_logs = MemoryCollection("attendance_logs")
        self.client = type("Client", (), {"admin": MemoryAdmin()})()
        self.commands = []

    def command(self, name, command, **kwargs):
        self.commands.append((name, deepcopy(command), kwargs))
        return {
            "queryPlanner": {
                "winningPlan": {"stage": "IXSCAN", "indexName": "test_index"}
            },
            "executionStats": {"nReturned": 0},
        }


@pytest.fixture
def api():
    database = MemoryDatabase()
    main.app.dependency_overrides[main.get_db] = lambda: database
    main.app.state.database = database
    main.app.state.indexes_ready = True
    client = TestClient(main.app)
    yield client, database
    main.app.dependency_overrides.clear()
    main.app.state.database = None


def employee_payload(emp_code="EMP0001", **overrides):
    payload = {
        "emp_code": emp_code,
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    payload.update(overrides)
    return payload


def insert_employee(database, **overrides):
    document = employee_payload(**overrides)
    document["created_at"] = datetime(2026, 1, 5, tzinfo=UTC)
    database.employees.insert_one(document)
    return document


def insert_record(
    database,
    emp_code="EMP0001",
    record_date="2026-07-06",
    status="PRESENT",
    punch_in="2026-07-06T04:00:00Z",
    punch_out=None,
    **overrides,
):
    document = {
        "emp_code": emp_code,
        "date": record_date,
        "status": status,
        "punch_in": (
            datetime.fromisoformat(punch_in.replace("Z", "+00:00"))
            if punch_in
            else None
        ),
        "punch_out": (
            datetime.fromisoformat(punch_out.replace("Z", "+00:00"))
            if punch_out
            else None
        ),
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    document.update(overrides)
    database.attendance_logs.insert_one(document)
    return document


def call_with_status(call, success_status=200):
    try:
        call()
        return success_status
    except main.HTTPException as exc:
        return exc.status_code


def test_health_pings_database(api):
    client, _ = api
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_employee_create_duplicate_and_server_timestamp(api):
    client, _ = api
    response = client.post("/employees", json=employee_payload())
    assert response.status_code == 201
    assert response.json()["emp_code"] == "EMP0001"
    assert isinstance(response.json()["created_at"], int)
    assert "_id" not in response.json()

    duplicate = client.post("/employees", json=employee_payload())
    assert duplicate.status_code == 409


def test_simultaneous_employee_creates_have_one_winner(api):
    _, database = api
    body = main.EmployeeIn(**employee_payload())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: call_with_status(
                    lambda: main.create_employee(body, database),
                    success_status=201,
                ),
                range(2),
            )
        )
    assert sorted(results) == [201, 409]


@pytest.mark.parametrize(
    "changes",
    [
        {"emp_code": "A1"},
        {"email": "not-an-email"},
        {"joined_on": "2026-02-30"},
        {"shift_start": "24:00"},
        {"shift_end": "09:30", "shift_start": "09:30"},
    ],
)
def test_employee_rejects_invalid_input(api, changes):
    client, _ = api
    response = client.post("/employees", json=employee_payload(**changes))
    assert response.status_code == 422


def test_employee_list_filters_sorts_and_counts_filtered_rows(api):
    client, database = api
    for code, department in (
        ("EMP0003", "Sales"),
        ("EMP0001", "Engineering"),
        ("EMP0002", "Engineering"),
    ):
        insert_employee(database, emp_code=code, department=department)

    response = client.get("/employees?department=Engineering&page=1&page_size=1")
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["items"][0]["emp_code"] == "EMP0001"
    assert response.json()["page"] == 1


def test_punch_in_truncates_milliseconds_and_applies_grace_boundary(api):
    client, database = api
    insert_employee(database)
    at_grace_boundary = epoch_ms("2026-07-06T04:10:00.900Z")
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": at_grace_boundary},
    )
    assert response.status_code == 201
    assert response.json()["date"] == "2026-07-06"
    assert response.json()["punch_in"] == epoch_ms("2026-07-06T04:10:00Z")
    assert response.json()["late_minutes"] == 0

    late = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms("2026-07-07T04:10:01Z"),
        },
    )
    assert late.status_code == 201
    assert late.json()["late_minutes"] == 10


@pytest.mark.parametrize(
    "timestamp", [None, 1, 1783312500, 1783312500000.0, "1783312500000"]
)
def test_punch_in_rejects_invalid_epoch_milliseconds(api, timestamp):
    client, database = api
    insert_employee(database)
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": timestamp},
    )
    assert response.status_code == 422


def test_punch_in_missing_employee_and_invalid_status(api):
    client, _ = api
    missing = client.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
    assert missing.status_code == 404
    insert_employee(api[1])
    invalid = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "status": "ABSENT"},
    )
    assert invalid.status_code == 422


def test_simultaneous_punch_ins_have_one_winner(api):
    _, database = api
    insert_employee(database)
    body = main.PunchInIn(
        emp_code="EMP0001", punched_at=epoch_ms("2026-07-06T04:00:00Z")
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: call_with_status(
                    lambda: main.punch_in(body, database), success_status=201
                ),
                range(2),
            )
        )
    assert sorted(results) == [201, 409]


def test_punch_out_calculates_hours_overtime_and_half_day(api):
    client, database = api
    insert_employee(database)
    start = epoch_ms("2026-07-06T04:00:00Z")
    assert (
        client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": start},
        ).status_code
        == 201
    )
    response = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms("2026-07-06T13:30:00Z"),
        },
    )
    assert response.status_code == 200
    assert response.json()["work_hours"] == 9.5
    assert response.json()["overtime_minutes"] == 30
    assert response.json()["half_day"] is False
    assert response.json()["history"] == []


def test_punch_out_half_day_rounding_and_missing_record(api):
    client, database = api
    insert_employee(database)
    insert_record(database)
    too_early = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms("2026-07-06T03:59:59Z"),
        },
    )
    assert too_early.status_code == 422

    too_late = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms("2026-07-07T04:00:01Z"),
        },
    )
    assert too_late.status_code == 422

    short = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms("2026-07-06T08:29:59Z"),
        },
    )
    assert short.status_code == 200
    assert short.json()["work_hours"] == 4.5
    assert short.json()["half_day"] is False

    insert_record(
        database,
        record_date="2026-07-07",
        punch_in="2026-07-07T04:00:00Z",
    )
    half_day = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms("2026-07-07T08:29:00Z"),
        },
    )
    assert half_day.status_code == 200
    assert half_day.json()["work_hours"] == 4.48
    assert half_day.json()["half_day"] is True

    no_record = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0002"},
    )
    assert no_record.status_code == 404


def test_overnight_punch_in_date_and_overtime(api):
    client, database = api
    insert_employee(
        database,
        emp_code="EMP0005",
        shift_start="22:00",
        shift_end="06:00",
        department="Support",
    )
    punched_in = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP0005",
            "punched_at": epoch_ms("2026-07-06T16:25:00Z"),
        },
    )
    assert punched_in.status_code == 201
    assert punched_in.json()["date"] == "2026-07-06"

    punched_out = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0005",
            "punched_at": epoch_ms("2026-07-07T01:10:00Z"),
        },
    )
    assert punched_out.status_code == 200
    assert punched_out.json()["overtime_minutes"] == 40

    insert_employee(
        database,
        emp_code="EMP0006",
        shift_start="22:00",
        shift_end="06:00",
        department="Support",
    )
    early_morning = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP0006",
            "punched_at": epoch_ms("2026-07-06T19:30:00Z"),
        },
    )
    assert early_morning.status_code == 201
    assert early_morning.json()["date"] == "2026-07-06"
    assert early_morning.json()["late_minutes"] == 180


def test_simultaneous_punch_outs_have_one_winner(api):
    _, database = api
    insert_employee(database)
    insert_record(database)
    body = main.PunchOutIn(
        emp_code="EMP0001", punched_at=epoch_ms("2026-07-06T13:00:00Z")
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: call_with_status(lambda: main.punch_out(body, database)),
                range(2),
            )
        )
    assert sorted(results) == [200, 409]


def test_regularization_recomputes_and_serializes_audit_history(api):
    client, database = api
    insert_employee(database)
    insert_record(
        database,
        punch_in="2026-07-06T04:35:00Z",
        punch_out="2026-07-06T13:00:00Z",
        work_hours=8.42,
        late_minutes=35,
    )
    response = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_in": epoch_ms("2026-07-06T03:58:00Z"),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["late_minutes"] == 0
    assert result["work_hours"] == 9.03
    assert result["history"][0]["by"] == "hr.admin"
    assert result["history"][0]["changes"]["punch_in"] == {
        "from": epoch_ms("2026-07-06T04:35:00Z"),
        "to": epoch_ms("2026-07-06T03:58:00Z"),
    }
    assert result["history"][0]["changes"]["late_minutes"] == {
        "from": 35,
        "to": 0,
    }


def test_regularization_validates_absent_nothing_and_missing_records(api):
    client, database = api
    insert_employee(database)
    insert_record(database)
    absent_with_punch = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "punch_in": epoch_ms("2026-07-06T04:00:00Z"),
            "reason": "wrong status",
            "regularized_by": "hr.admin",
        },
    )
    assert absent_with_punch.status_code == 422

    no_change = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "reviewed", "regularized_by": "hr.admin"},
    )
    assert no_change.status_code == 422

    missing = client.patch(
        "/attendance/EMP0002/2026-07-06",
        json={"status": "ABSENT", "reason": "correction", "regularized_by": "hr.admin"},
    )
    assert missing.status_code == 404


def test_concurrent_regularizations_do_not_overwrite_history(api):
    _, database = api
    insert_employee(database)
    insert_record(
        database,
        punch_in="2026-07-06T04:35:00Z",
        punch_out="2026-07-06T13:00:00Z",
        work_hours=8.42,
        late_minutes=35,
    )
    database.attendance_logs.read_barrier = Barrier(2)

    def regularize(timestamp):
        body = main.RegularizeIn(
            punch_in=timestamp,
            reason="race correction",
            regularized_by="hr.admin",
        )
        return call_with_status(
            lambda: main.regularize_attendance(
                "EMP0001", datetime(2026, 7, 6).date(), body, database
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                regularize,
                (
                    epoch_ms("2026-07-06T03:58:00Z"),
                    epoch_ms("2026-07-06T04:00:00Z"),
                ),
            )
        )
    assert sorted(results) == [200, 409]
    assert len(database.attendance_logs.documents[0]["history"]) == 1


def test_attendance_list_filters_paginates_and_reports_filtered_total(api):
    client, database = api
    insert_employee(database)
    insert_record(database, record_date="2026-07-06")
    insert_record(
        database,
        emp_code="EMP0002",
        record_date="2026-07-07",
        punch_in="2026-07-07T04:00:00Z",
        status="WFH",
    )
    insert_record(
        database,
        emp_code="EMP0003",
        record_date="2026-07-08",
        punch_in="2026-07-08T04:00:00Z",
        status="PRESENT",
    )
    response = client.get(
        "/attendance?date_from=2026-07-06&status=PRESENT&page=1&page_size=1"
    )
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["date"] == "2026-07-08"
    assert "_id" not in response.json()["items"][0]

    invalid_range = client.get(
        "/attendance?date_from=2026-07-08&date_to=2026-07-06"
    )
    assert invalid_range.status_code == 422


def test_monthly_department_leaderboard_and_trend_pipeline_shapes():
    monthly = main._monthly_pipeline("EMP0001", "2026-07")
    department = main._department_summary_pipeline("2026-07", "Engineering")
    leaderboard = main._late_leaderboard_pipeline("2026-07", 2, None)
    trend = main._department_trend_pipeline(
        "Engineering", datetime(2026, 7, 1).date(), datetime(2026, 7, 8).date()
    )

    assert monthly[0] == {"$match": {"emp_code": "EMP0001"}}
    assert any("$lookup" in stage for stage in monthly)
    assert any("$range" in str(stage) for stage in monthly)
    assert department[0]["$match"]["joined_on"]["$lte"] == "2026-07-31"
    assert any("$group" in stage for stage in department)
    assert any("$rank" in str(stage) for stage in leaderboard)
    assert any("$setWindowFields" in stage for stage in leaderboard)
    assert any("$range" in str(stage) for stage in trend)
    assert any("$setWindowFields" in stage for stage in trend)
    assert any("$lookup" in stage for stage in trend)


def test_explain_endpoint_uses_execution_stats_and_validates_required_query(api):
    client, database = api
    response = client.get(
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"
    )
    assert response.status_code == 200
    assert response.json()["collection"] == "employees"
    assert response.json()["explain"]["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN"
    assert database.commands[0][2]["verbosity"] == "executionStats"

    missing = client.get("/admin/explain/department_summary")
    assert missing.status_code == 422


def test_indexes_are_idempotent_and_include_unique_natural_keys():
    database = MemoryDatabase()
    main.ensure_indexes(database)
    first_count = len(database.employees.indexes) + len(database.attendance_logs.indexes)
    main.ensure_indexes(database)
    second_count = len(database.employees.indexes) + len(database.attendance_logs.indexes)
    assert first_count == second_count == 9
    employee_unique = [
        options
        for _, options in database.employees.indexes
        if options.get("unique")
    ]
    attendance_unique = [
        options
        for _, options in database.attendance_logs.indexes
        if options.get("unique")
    ]
    assert employee_unique and attendance_unique


def test_decimal_rounding_and_overtime_thresholds():
    start = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
    assert main.compute_work_hours(start, start.replace(second=18)) == 0.01
    assert (
        main.compute_overtime(
            datetime(2026, 7, 6, 13, 0, tzinfo=UTC),
            "18:30",
            "2026-07-06",
            "09:30",
        )
        == 0
    )
    assert (
        main.compute_overtime(
            datetime(2026, 7, 6, 13, 0, tzinfo=UTC) + timedelta(minutes=30),
            "18:30",
            "2026-07-06",
            "09:30",
        )
        == 30
    )
