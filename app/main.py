"""Employee Attendance & Analytics API."""

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.responses import JSONResponse

load_dotenv()

logger = logging.getLogger(__name__)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
WEEKDAYS = (2, 3, 4, 5, 6)
EMP_CODE_PATTERN = re.compile(r"^EMP\d{4,6}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

EpochMillis = Annotated[
    int,
    Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS),
]


def _now_utc_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _datetime_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate_seconds(value: datetime) -> datetime:
    return _datetime_utc(value).replace(microsecond=0)


def _epoch_to_datetime(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=value // 1000)


def _datetime_to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(_datetime_utc(value).timestamp()) * 1000


def _validate_calendar_date(value: str, label: str) -> date:
    if not DATE_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"{label} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label} must be a valid date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{label} must be YYYY-MM-DD")
    return parsed


def _month_bounds(month: str) -> tuple[str, str, date, date]:
    if not MONTH_PATTERN.fullmatch(month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, month_number = map(int, month.split("-"))
    try:
        start_day = date(year, month_number, 1)
        if month_number == 12:
            next_day = date(year + 1, 1, 1)
        else:
            next_day = date(year, month_number + 1, 1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be a valid calendar month") from exc
    return start_day.isoformat(), next_day.isoformat(), start_day, next_day - timedelta(days=1)


def _attendance_date(punch_in: datetime, employee: dict[str, Any]) -> date:
    local = _datetime_utc(punch_in).astimezone(IST)
    shift_start = time.fromisoformat(employee["shift_start"])
    shift_end = time.fromisoformat(employee["shift_end"])
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(
    punch_in: datetime, shift_start: str, attendance_day: Optional[str] = None
) -> int:
    local_punch = _datetime_utc(punch_in).astimezone(IST)
    if attendance_day is None:
        attendance_day = local_punch.date().isoformat()
    shift_time = time.fromisoformat(shift_start)
    shift_start_at = datetime.combine(date.fromisoformat(attendance_day), shift_time, IST)
    elapsed_seconds = int((local_punch - shift_start_at).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return max(0, elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed_seconds = int((_datetime_utc(punch_out) - _datetime_utc(punch_in)).total_seconds())
    hours = (Decimal(elapsed_seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    attendance_day: str,
    shift_start: Optional[str] = None,
) -> int:
    end_time = time.fromisoformat(shift_end)
    end_day = date.fromisoformat(attendance_day)
    if shift_start is not None and end_time <= time.fromisoformat(shift_start):
        end_day += timedelta(days=1)
    shift_end_at = datetime.combine(end_day, end_time, IST)
    elapsed_seconds = int(
        (_datetime_utc(punch_out) - shift_end_at.astimezone(UTC)).total_seconds()
    )
    overtime_minutes = max(0, elapsed_seconds // 60)
    return overtime_minutes if overtime_minutes >= 30 else 0


def _employee_response(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": _datetime_to_epoch(document["created_at"]),
    }


def _history_time(value: Any) -> Any:
    if isinstance(value, datetime):
        return _datetime_to_epoch(value)
    return value


def _attendance_response(document: dict[str, Any]) -> dict[str, Any]:
    history = []
    for entry in document.get("history") or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": _history_time(change.get("from")),
                "to": _history_time(change.get("to")),
            }
        history.append(
            {
                "at": _datetime_to_epoch(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": _datetime_to_epoch(document.get("punch_in")),
        "punch_out": _datetime_to_epoch(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": history,
    }


def ensure_indexes(database: Any) -> None:
    database.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code"
    )
    database.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_emp_code",
    )
    database.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employees_department_joined",
    )
    database.employees.create_index(
        [("joined_on", ASCENDING)], name="ix_employees_joined"
    )
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    database.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp",
    )
    database.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_emp",
    )
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_emp_punch_in",
    )
    database.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_late_emp",
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    client = None
    application.state.database = None
    application.state.indexes_ready = False
    try:
        client = MongoClient(
            os.getenv("MONGO_URI", "mongodb://localhost:27017"),
            tz_aware=True,
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
        )
        database = client[os.getenv("MONGO_DB", "attendance_db")]
        application.state.mongo_client = client
        application.state.database = database
        client.admin.command("ping")
        ensure_indexes(database)
        application.state.indexes_ready = True
    except PyMongoError:
        logger.exception("MongoDB startup check or index creation failed")
    try:
        yield
    finally:
        if client is not None:
            client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


def get_db(request: Request) -> Any:
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    return database


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, __: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "MongoDB is unavailable"})


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        if not DATE_PATTERN.fullmatch(value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("joined_on must be YYYY-MM-DD")
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def timestamp_cannot_be_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def timestamp_cannot_be_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def timestamps_cannot_be_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("timestamp must be an epoch-millisecond integer")
        return value

    @field_validator("status", mode="before")
    @classmethod
    def status_cannot_be_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("status must be a supported status")
        return value


def _half_up_expression(expression: Any, digits: int) -> dict[str, Any]:
    scale = 10**digits
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}},
            scale,
        ]
    }


def _working_days_expression(start_expression: Any, end_expression: Any) -> dict[str, Any]:
    day_count = {
        "$add": [
            {
                "$dateDiff": {
                    "startDate": start_expression,
                    "endDate": end_expression,
                    "unit": "day",
                }
            },
            1,
        ]
    }
    dates = {
        "$map": {
            "input": {"$range": [0, day_count]},
            "as": "offset",
            "in": {
                "$dateAdd": {
                    "startDate": start_expression,
                    "unit": "day",
                    "amount": "$$offset",
                }
            },
        }
    }
    return {
        "$size": {
            "$filter": {
                "input": dates,
                "as": "calendar_day",
                "cond": {"$in": [{"$dayOfWeek": "$$calendar_day"}, list(WEEKDAYS)]},
            }
        }
    }


def _sum_expression(values: Any, expression: Any) -> dict[str, Any]:
    return {
        "$reduce": {
            "input": values,
            "initialValue": 0,
            "in": {"$add": ["$$value", expression]},
        }
    }


def _month_log_lookup(start: str, next_start: str) -> dict[str, Any]:
    return {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$employee_code"]},
                                {"$gte": ["$date", start]},
                                {"$lt": ["$date", next_start]},
                            ]
                        }
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "date": 1,
                        "status": 1,
                        "half_day": 1,
                        "work_hours": 1,
                        "late_minutes": 1,
                        "overtime_minutes": 1,
                    }
                },
            ],
            "as": "_monthly_logs",
        }
    }


def _monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, next_start, _, end_date = _month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        _month_log_lookup(start, next_start),
        {
            "$set": {
                "_joined_date": {"$dateFromString": {"dateString": "$joined_on"}},
                "_month_start": {"$dateFromString": {"dateString": start}},
                "_month_end": {
                    "$dateFromString": {"dateString": end_date.isoformat()}
                },
            }
        },
        {
            "$set": {
                "_working_start": {
                    "$cond": [
                        {"$gt": ["$_joined_date", "$_month_start"]},
                        "$_joined_date",
                        "$_month_start",
                    ]
                }
            }
        },
        {
            "$set": {
                "working_days": {
                    "$cond": [
                        {"$lte": ["$_working_start", "$_month_end"]},
                        _working_days_expression("$_working_start", "$_month_end"),
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "_present_logs": {
                    "$filter": {
                        "input": "$_monthly_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                {
                                    "$in": [
                                        {
                                            "$dayOfWeek": {
                                                "$dateFromString": {
                                                    "dateString": "$$log.date"
                                                }
                                            }
                                        },
                                        list(WEEKDAYS),
                                    ]
                                },
                            ]
                        },
                    }
                },
                "_leave_logs": {
                    "$filter": {
                        "input": "$_monthly_logs",
                        "as": "log",
                        "cond": {"$eq": ["$$log.status", "LEAVE"]},
                    }
                },
                "_late_logs": {
                    "$filter": {
                        "input": "$_monthly_logs",
                        "as": "log",
                        "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "present_days": _sum_expression(
                    "$_present_logs",
                    {
                        "$cond": [
                            {"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]},
                            0.5,
                            1,
                        ]
                    },
                ),
                "leave_days": {"$size": "$_leave_logs"},
                "late_count": {"$size": "$_late_logs"},
                "total_late_minutes": _sum_expression(
                    "$_monthly_logs", {"$ifNull": ["$$this.late_minutes", 0]}
                ),
                "total_overtime_minutes": _sum_expression(
                    "$_monthly_logs", {"$ifNull": ["$$this.overtime_minutes", 0]}
                ),
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$eq": ["$working_days", 0]},
                        None,
                        _half_up_expression(
                            {
                                "$multiply": [
                                    {"$divide": ["$present_days", "$working_days"]},
                                    100,
                                ]
                            },
                            2,
                        ),
                    ]
                },
            }
        },
    ]


def _department_summary_pipeline(
    month: str, department: Optional[str]
) -> list[dict[str, Any]]:
    start, next_start, _, month_end = _month_bounds(month)
    employee_filter: dict[str, Any] = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_filter["department"] = department
    pipeline: list[dict[str, Any]] = [
        {"$match": employee_filter},
        _month_log_lookup(start, next_start),
        {
            "$set": {
                "_joined_date": {"$dateFromString": {"dateString": "$joined_on"}},
                "_month_start": {"$dateFromString": {"dateString": start}},
                "_month_end": {
                    "$dateFromString": {"dateString": month_end.isoformat()}
                },
            }
        },
        {
            "$set": {
                "_working_start": {
                    "$cond": [
                        {"$gt": ["$_joined_date", "$_month_start"]},
                        "$_joined_date",
                        "$_month_start",
                    ]
                }
            }
        },
        {
            "$set": {
                "_working_days": _working_days_expression(
                    "$_working_start", "$_month_end"
                )
            }
        },
        {
            "$set": {
                "_present_logs": {
                    "$filter": {
                        "input": "$_monthly_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                {
                                    "$in": [
                                        {
                                            "$dayOfWeek": {
                                                "$dateFromString": {
                                                    "dateString": "$$log.date"
                                                }
                                            }
                                        },
                                        list(WEEKDAYS),
                                    ]
                                },
                            ]
                        },
                    }
                },
                "_present_worked_logs": {
                    "$filter": {
                        "input": "$_monthly_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                {
                                    "$ne": [
                                        {"$ifNull": ["$$log.work_hours", None]},
                                        None,
                                    ]
                                },
                            ]
                        },
                    }
                },
            }
        },
        {
            "$set": {
                "_present_days": _sum_expression(
                    "$_present_logs",
                    {
                        "$cond": [
                            {"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]},
                            0.5,
                            1,
                        ]
                    },
                ),
                "_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_monthly_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
                "_late_minutes": _sum_expression(
                    "$_monthly_logs", {"$ifNull": ["$$this.late_minutes", 0]}
                ),
                "_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_monthly_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_monthly_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
                "_work_hours_sum": _sum_expression(
                    "$_present_worked_logs",
                    {"$ifNull": ["$$this.work_hours", 0]},
                ),
                "_work_hours_count": {"$size": "$_present_worked_logs"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$_present_days"},
                "_work_hours_sum": {"$sum": "$_work_hours_sum"},
                "_work_hours_count": {"$sum": "$_work_hours_count"},
                "late_count": {"$sum": "$_late_count"},
                "total_late_minutes": {"$sum": "$_late_minutes"},
                "leave_count": {"$sum": "$_leave_count"},
                "on_duty_count": {"$sum": "$_on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$_work_hours_count", 0]},
                        None,
                        _half_up_expression(
                            {
                                "$divide": [
                                    "$_work_hours_sum",
                                    "$_work_hours_count",
                                ]
                            },
                            2,
                        ),
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    return pipeline


def _late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str]
) -> list[dict[str, Any]]:
    start, next_start, _, _ = _month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start, "$lt": next_start},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$_employee.name",
                    "department": "$_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def _department_trend_pipeline(
    department: str, start_day: date, end_day: date
) -> list[dict[str, Any]]:
    start_text = start_day.isoformat()
    end_text = end_day.isoformat()
    day_count = (end_day - start_day).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None}},
        {
            "$set": {
                "_days": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": {
                                    "$dateFromString": {"dateString": start_text}
                                },
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_days"},
        {
            "$set": {
                "_date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$_days"}
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"calendar_date": "$_date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$calendar_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"calendar_date": "$_date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$calendar_date"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "_employee",
                        }
                    },
                    {"$unwind": "$_employee"},
                    {"$match": {"_employee.department": department}},
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "_daily_logs",
            }
        },
        {
            "$set": {
                "date": "$_date",
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$_days"}, list(WEEKDAYS)]
                },
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]
                },
                "present_count": _sum_expression(
                    {
                        "$filter": {
                            "input": "$_daily_logs",
                            "as": "log",
                            "cond": {
                                "$in": ["$$log.status", list(PRESENCE_STATUSES)]
                            },
                        }
                    },
                    {
                        "$cond": [
                            {"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]},
                            0.5,
                            1,
                        ]
                    },
                ),
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_daily_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        _half_up_expression(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_days": 1},
                "output": {
                    "_raw_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_raw_moving_avg", None]},
                        None,
                        _half_up_expression("$_raw_moving_avg", 4),
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def _missing_query(name: str) -> None:
    raise HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["query", name],
                "msg": "Field required",
                "type": "missing",
            }
        ],
    )


@app.get("/health")
def health(request: Request):
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    try:
        database.client.admin.command("ping")
        if not getattr(request.app.state, "indexes_ready", False):
            ensure_indexes(database)
            request.app.state.indexes_ready = True
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn, database: Any = Depends(get_db)):
    document = body.model_dump()
    document["created_at"] = _now_utc_truncated()
    try:
        database.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return _employee_response(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    database: Any = Depends(get_db),
):
    query = {"department": department} if department is not None else {}
    total = database.employees.count_documents(query)
    cursor = (
        database.employees.find(query, {"_id": 0})
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_employee_response(document) for document in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn, database: Any = Depends(get_db)):
    employee = database.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = (
        _epoch_to_datetime(body.punched_at)
        if body.punched_at is not None
        else _now_utc_truncated()
    )
    attendance_day = _attendance_date(punched_at, employee).isoformat()
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at, employee["shift_start"], attendance_day
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        database.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        ) from exc
    return _attendance_response(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn, database: Any = Depends(get_db)):
    employee = database.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = (
        _epoch_to_datetime(body.punched_at)
        if body.punched_at is not None
        else _now_utc_truncated()
    )
    record = database.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": list(PRESENCE_STATUSES)},
            "punch_in": {
                "$type": "date",
                "$lt": punched_at + timedelta(seconds=1),
            },
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        latest_record = database.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "status": {"$in": list(PRESENCE_STATUSES)},
                "punch_in": {"$type": "date"},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if latest_record is not None and punched_at <= _truncate_seconds(
            latest_record["punch_in"]
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be after punch_in and within 24 hours",
            )
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    punch_in_at = _truncate_seconds(record["punch_in"])
    elapsed = punched_at - punch_in_at
    if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )
    work_hours = compute_work_hours(punch_in_at, punched_at)
    updates = {
        "punch_out": punched_at,
        "punch_in": punch_in_at,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(
            punched_at,
            employee["shift_end"],
            record["date"],
            employee["shift_start"],
        ),
        "half_day": work_hours < 4.5,
    }
    updated = database.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_in": record["punch_in"],
            "punch_out": None,
        },
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return _attendance_response(updated)


def _attendance_query(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict[str, Any]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_filter: dict[str, str] = {}
        if date_from is not None:
            date_filter["$gte"] = date_from.isoformat()
        if date_to is not None:
            date_filter["$lte"] = date_to.isoformat()
        query["date"] = date_filter
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    database: Any = Depends(get_db),
):
    query = _attendance_query(emp_code, date_from, date_to, status)
    total = database.attendance_logs.count_documents(query)
    cursor = (
        database.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_attendance_response(document) for document in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: date,
    body: RegularizeIn,
    database: Any = Depends(get_db),
):
    record_date = date.isoformat()
    employee = database.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    record = database.attendance_logs.find_one(
        {"emp_code": emp_code, "date": record_date}
    )
    if employee is None or record is None:
        raise HTTPException(status_code=404, detail="employee or attendance record not found")

    fields_set = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and (
        "punch_in" in fields_set or "punch_out" in fields_set
    ):
        raise HTTPException(
            status_code=422,
            detail="ABSENT and LEAVE records cannot include punch times",
        )

    next_status = body.status if "status" in fields_set else record["status"]
    next_punch_in = (
        _epoch_to_datetime(body.punch_in)
        if "punch_in" in fields_set
        else record.get("punch_in")
    )
    next_punch_out = (
        _epoch_to_datetime(body.punch_out)
        if "punch_out" in fields_set
        else record.get("punch_out")
    )

    if next_status in ("ABSENT", "LEAVE"):
        next_punch_in = None
        next_punch_out = None
    else:
        if next_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="presence statuses require a punch_in",
            )
        if _attendance_date(next_punch_in, employee).isoformat() != record_date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the record attendance date",
            )
        if next_punch_out is not None:
            elapsed_seconds = int(
                (next_punch_out - next_punch_in).total_seconds()
            )
            if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    if next_status in ("ABSENT", "LEAVE"):
        derived = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        next_punch_in = _truncate_seconds(next_punch_in)
        next_punch_out = (
            _truncate_seconds(next_punch_out) if next_punch_out is not None else None
        )
        if next_punch_out is not None:
            elapsed_seconds = int((next_punch_out - next_punch_in).total_seconds())
            if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
        derived = {
            "late_minutes": compute_late_minutes(
                next_punch_in, employee["shift_start"], record_date
            ),
            "work_hours": (
                compute_work_hours(next_punch_in, next_punch_out)
                if next_punch_out is not None
                else None
            ),
            "overtime_minutes": (
                compute_overtime(
                    next_punch_out,
                    employee["shift_end"],
                    record_date,
                    employee["shift_start"],
                )
                if next_punch_out is not None
                else 0
            ),
            "half_day": (
                compute_work_hours(next_punch_in, next_punch_out) < 4.5
                if next_punch_out is not None
                else False
            ),
        }

    next_values = {
        "status": next_status,
        "punch_in": next_punch_in,
        "punch_out": next_punch_out,
        **derived,
    }
    old_values = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "late_minutes": record.get("late_minutes", 0),
        "work_hours": record.get("work_hours"),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changes = {
        field: {"from": old_values[field], "to": value}
        for field, value in next_values.items()
        if old_values[field] != value
    }
    if not changes:
        raise HTTPException(status_code=422, detail="regularization changes nothing")

    history_entry = {
        "at": _now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    optimistic_filter: dict[str, Any] = {
        "emp_code": emp_code,
        "date": record_date,
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    if "history" in record:
        optimistic_filter["history"] = record["history"]
    else:
        optimistic_filter["history"] = {"$exists": False}
    update = {
        "$set": next_values,
        "$push": {"history": history_entry},
    }
    updated = database.attendance_logs.find_one_and_update(
        optimistic_filter,
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently")
    return _attendance_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    database: Any = Depends(get_db),
):
    _month_bounds(month)
    results = list(
        database.employees.aggregate(
            _monthly_pipeline(emp_code, month),
            hint=[("emp_code", ASCENDING)],
        )
    )
    if not results:
        raise HTTPException(status_code=404, detail="employee not found")
    return results[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    database: Any = Depends(get_db),
):
    _month_bounds(month)
    hint = [("department", ASCENDING), ("joined_on", ASCENDING)]
    if department is None:
        hint = [("joined_on", ASCENDING)]
    items = list(
        database.employees.aggregate(
            _department_summary_pipeline(month, department),
            hint=hint,
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
    database: Any = Depends(get_db),
):
    _month_bounds(month)
    items = list(
        database.attendance_logs.aggregate(
            _late_leaderboard_pipeline(month, limit, department),
            hint=[("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
    database: Any = Depends(get_db),
):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must not be before from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    pipeline = _department_trend_pipeline(department, from_date, to_date)
    items = list(
        database.employees.aggregate(
            pipeline,
            hint=[("department", ASCENDING), ("joined_on", ASCENDING)],
        )
    )
    if not items:
        exists = database.employees.find_one({"department": department}, {"_id": 1})
        if exists is None:
            raise HTTPException(status_code=404, detail="department not found")
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    database: Any = Depends(get_db),
):
    if endpoint == "attendance_list":
        query = _attendance_query(emp_code, date_from, date_to, status)
        explain_command = {
            "find": "attendance_logs",
            "filter": query,
            "projection": {"_id": 0},
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None:
            _missing_query("emp_code")
        if month is None:
            _missing_query("month")
        _month_bounds(month)
        explain_command = {
            "aggregate": "employees",
            "pipeline": _monthly_pipeline(emp_code, month),
            "cursor": {},
            "hint": {"emp_code": ASCENDING},
        }
        collection = "employees"
    elif endpoint == "department_summary":
        if month is None:
            _missing_query("month")
        _month_bounds(month)
        if department is None:
            hint = {"joined_on": ASCENDING}
        else:
            hint = {"department": ASCENDING, "joined_on": ASCENDING}
        explain_command = {
            "aggregate": "employees",
            "pipeline": _department_summary_pipeline(month, department),
            "cursor": {},
            "hint": hint,
        }
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            _missing_query("month")
        _month_bounds(month)
        explain_command = {
            "aggregate": "attendance_logs",
            "pipeline": _late_leaderboard_pipeline(month, limit, department),
            "cursor": {},
            "hint": {
                "date": ASCENDING,
                "late_minutes": ASCENDING,
                "emp_code": ASCENDING,
            },
        }
        collection = "attendance_logs"
    else:
        if department is None:
            _missing_query("department")
        if from_date is None:
            _missing_query("from")
        if to_date is None:
            _missing_query("to")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to must not be before from")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(
                status_code=422, detail="date range cannot exceed 92 days"
            )
        explain_command = {
            "aggregate": "employees",
            "pipeline": _department_trend_pipeline(department, from_date, to_date),
            "cursor": {},
            "hint": {"department": ASCENDING, "joined_on": ASCENDING},
        }
        collection = "employees"

    try:
        explain = database.command(
            "explain", explain_command, verbosity="executionStats"
        )
    except PyMongoError:
        raise
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
