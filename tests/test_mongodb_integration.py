import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main


MONGO_TEST_URI = os.getenv("MONGO_TEST_URI")
pytestmark = pytest.mark.skipif(
    not MONGO_TEST_URI,
    reason="Set MONGO_TEST_URI to run MongoDB integration and explain-plan tests",
)
UTC = timezone.utc


@pytest.fixture
def mongo_api():
    client = MongoClient(MONGO_TEST_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    database_name = f"hrone_test_{uuid4().hex}"
    database = client[database_name]
    main.ensure_indexes(database)
    main.app.dependency_overrides[main.get_db] = lambda: database
    main.app.state.database = database
    main.app.state.indexes_ready = True
    test_client = TestClient(main.app)

    employees = [
        {
            "emp_code": "EMP1001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
            "created_at": datetime(2026, 7, 1, tzinfo=UTC),
        },
        {
            "emp_code": "EMP1002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-15",
            "created_at": datetime(2026, 7, 15, tzinfo=UTC),
        },
        {
            "emp_code": "EMP1003",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-06-01",
            "created_at": datetime(2026, 6, 1, tzinfo=UTC),
        },
        {
            "emp_code": "EMP1004",
            "name": "Rohit Das",
            "email": "rohit@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ]
    database.employees.insert_many(employees)
    database.attendance_logs.insert_many(
        [
            {
                "emp_code": "EMP1001",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 6, 4, tzinfo=UTC),
                "punch_out": datetime(2026, 7, 6, 13, tzinfo=UTC),
                "work_hours": 8.0,
                "late_minutes": 30,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            },
            {
                "emp_code": "EMP1001",
                "date": "2026-07-11",
                "status": "WFH",
                "punch_in": datetime(2026, 7, 11, 4, tzinfo=UTC),
                "punch_out": datetime(2026, 7, 11, 8, 29, tzinfo=UTC),
                "work_hours": 4.49,
                "late_minutes": 5,
                "overtime_minutes": 0,
                "half_day": True,
                "history": [],
            },
            {
                "emp_code": "EMP1002",
                "date": "2026-07-15",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 15, 4, tzinfo=UTC),
                "punch_out": datetime(2026, 7, 15, 8, 30, tzinfo=UTC),
                "work_hours": 4.5,
                "late_minutes": 35,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            },
            {
                "emp_code": "EMP1002",
                "date": "2026-07-16",
                "status": "LEAVE",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            },
            {
                "emp_code": "EMP1004",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 6, 4, tzinfo=UTC),
                "punch_out": datetime(2026, 7, 6, 13, tzinfo=UTC),
                "work_hours": 8.0,
                "late_minutes": 10,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            },
        ]
    )
    try:
        yield test_client, database
    finally:
        main.app.dependency_overrides.clear()
        main.app.state.database = None
        client.drop_database(database_name)
        client.close()


def test_monthly_summary_honors_midmonth_join_and_weekend_rules(mongo_api):
    client, _ = mongo_api
    response = client.get(
        "/analytics/employees/EMP1002/monthly?month=2026-07"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["working_days"] == 13
    assert body["present_days"] == 1
    assert body["leave_days"] == 1
    assert body["late_count"] == 1
    assert body["total_late_minutes"] == 35
    assert body["attendance_pct"] == 7.69

    no_logs = client.get(
        "/analytics/employees/EMP1003/monthly?month=2026-07"
    ).json()
    assert no_logs["working_days"] == 23
    assert no_logs["present_days"] == 0
    assert no_logs["attendance_pct"] == 0


def test_health_returns_ready_when_mongodb_answers(mongo_api):
    client, _ = mongo_api
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_department_summary_counts_no_log_employees_and_averages_records(mongo_api):
    client, _ = mongo_api
    response = client.get(
        "/analytics/departments/summary?month=2026-07&department=Engineering"
    )
    assert response.status_code == 200
    department = response.json()["items"][0]
    assert department["headcount"] == 3
    assert department["present_days"] == 2
    assert department["avg_work_hours"] == 5.66
    assert department["late_count"] == 3
    assert department["total_late_minutes"] == 70
    assert department["leave_count"] == 1


def test_leaderboard_ranks_ties_before_applying_limit(mongo_api):
    client, _ = mongo_api
    response = client.get(
        "/analytics/leaderboard/late?month=2026-07&limit=1"
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["emp_code"] for item in items] == ["EMP1001", "EMP1002"]
    assert [item["rank"] for item in items] == [1, 1]

    full_leaderboard = client.get(
        "/analytics/leaderboard/late?month=2026-07&limit=3"
    ).json()["items"]
    assert [item["rank"] for item in full_leaderboard] == [1, 1, 3]


def test_trend_fills_missing_days_and_weekends(mongo_api):
    client, _ = mongo_api
    response = client.get(
        "/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12"
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 7
    assert [item["date"] for item in items] == [
        f"2026-07-{day:02d}" for day in range(6, 13)
    ]
    assert items[0]["headcount"] == 2
    assert items[0]["present_count"] == 1
    assert items[0]["attendance_rate"] == 0.5
    assert items[1]["present_count"] == 0
    assert items[1]["attendance_rate"] == 0
    assert items[5]["is_working_day"] is False
    assert items[5]["attendance_rate"] is None
    assert items[5]["moving_avg_7d"] == 0.1


@pytest.mark.parametrize(
    "path,query",
    [
        ("/admin/explain/attendance_list", "?date_from=2026-07-01"),
        (
            "/admin/explain/employee_monthly",
            "?emp_code=EMP1001&month=2026-07",
        ),
        (
            "/admin/explain/department_summary",
            "?month=2026-07&department=Engineering",
        ),
        (
            "/admin/explain/late_leaderboard",
            "?month=2026-07&limit=1",
        ),
        (
            "/admin/explain/department_trend",
            "?department=Engineering&from=2026-07-06&to=2026-07-12",
        ),
    ],
)
def test_explain_plans_use_indexes_without_collection_scans(mongo_api, path, query):
    client, _ = mongo_api
    response = client.get(path + query)
    assert response.status_code == 200
    explain = response.json()["explain"]
    serialized = str(explain)
    assert "IXSCAN" in serialized
    assert "COLLSCAN" not in serialized
