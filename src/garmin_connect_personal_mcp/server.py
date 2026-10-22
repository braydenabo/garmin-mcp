"""The MCP server: tools that answer questions from the local archive.

Tools only read the archive, which sync fills. Every tool has a flat input
schema, units in its output field names, accurate annotations, and a typed
output (structuredContent plus a text copy for clients that only read text).
Errors are returned as tool results that say what to do next.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from garmin_connect_personal_mcp import __version__, db, metrics
from garmin_connect_personal_mcp.config import DataPaths

PROG = "garmin-connect-personal-mcp"

INSTRUCTIONS = (
    "Answers questions about the user's own Garmin health and activity data from a local "
    "archive. Call garmin_describe_data first to see which metrics exist, their units, and the "
    "dates covered. Dates are the user's local calendar dates (YYYY-MM-DD). Missing data is "
    "null, never zero: a null usually means the watch wasn't worn. This is consumer wearable "
    "data, not a medical device; don't diagnose."
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

NOTES = [
    "Dates are the user's local calendar dates.",
    "Missing values are null, never zero. A day without data usually means the watch wasn't worn.",
    "Values come from a consumer wearable and are not medical measurements.",
]


class DatasetCoverage(BaseModel):
    dataset: str
    days_with_data: int
    first_date: str | None
    last_date: str | None
    last_synced_date: str | None = Field(description="Latest local date checked by sync")


class MetricInfo(BaseModel):
    name: str
    dataset: str
    unit: str
    description: str
    numeric: bool = Field(description="Whether garmin_get_trend can chart it")


class DataDescription(BaseModel):
    archive_exists: bool
    last_sync_utc: str | None
    datasets: list[DatasetCoverage]
    activities_count: int
    activities_first_date: str | None
    activities_last_date: str | None
    metrics: list[MetricInfo]
    notes: list[str]


@contextmanager
def open_archive(paths: DataPaths) -> Iterator[sqlite3.Connection | None]:
    """A read-only connection, or None if there is no archive yet."""
    if not paths.db.exists():
        yield None
        return
    conn = db.connect_readonly(paths.db)
    try:
        yield conn
    finally:
        conn.close()


def describe_data(paths: DataPaths) -> DataDescription:
    metric_list = [
        MetricInfo(
            name=name, dataset=m.dataset, unit=m.unit, description=m.description, numeric=m.numeric
        )
        for name, m in metrics.METRICS.items()
    ]
    with open_archive(paths) as conn:
        if conn is None:
            return DataDescription(
                archive_exists=False,
                last_sync_utc=None,
                datasets=[],
                activities_count=0,
                activities_first_date=None,
                activities_last_date=None,
                metrics=metric_list,
                notes=[f"No data has been synced yet. The user can run `{PROG} sync`.", *NOTES],
            )
        count, first, last = conn.execute(
            "SELECT COUNT(*), MIN(calendar_date), MAX(calendar_date) FROM activities"
        ).fetchone()
        return DataDescription(
            archive_exists=True,
            last_sync_utc=db.last_sync_time(conn),
            datasets=[
                DatasetCoverage(
                    dataset=c.dataset,
                    days_with_data=c.days_with_data,
                    first_date=c.first_date,
                    last_date=c.last_date,
                    last_synced_date=c.last_synced,
                )
                for c in metrics.coverage(conn)
            ],
            activities_count=count,
            activities_first_date=first,
            activities_last_date=last,
            metrics=metric_list,
            notes=NOTES,
        )


def build_server(paths: DataPaths) -> MCPServer:
    server = MCPServer(
        "garmin",
        title="Garmin (personal data)",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @server.tool(
        name="garmin_describe_data",
        title="Describe the Garmin data",
        description=(
            "What's in the archive: each dataset's date coverage and last sync, every metric "
            "with its unit, and activity counts. Call this first."
        ),
        annotations=READ_ONLY,
    )
    def garmin_describe_data() -> DataDescription:
        return describe_data(paths)

    return server
