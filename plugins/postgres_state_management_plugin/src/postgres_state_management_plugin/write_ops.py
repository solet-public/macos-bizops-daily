"""Write operation helpers for PostgreSQL state plugin."""

from ananta.core.domain.types import ActionResult

from postgres_state_management_plugin.postgres_backend.provider import PostgresProvider

from .result_helpers import create_error_result, create_success_result


def write_single_record(
    provider: PostgresProvider, namespace: str, table: str, record: object
) -> ActionResult:
    if not isinstance(record, dict):
        return create_error_result(
            "Missing or invalid 'record' in data",
            error_code="write.invalid_record",
        )
    record_id = provider.insert(namespace=namespace, table=table, data=record)
    return create_success_result(
        {
            "namespace": namespace,
            "result": {"generated_id": record_id, "inserted": 1},
        }
    )


def write_multiple_records(
    provider: PostgresProvider, namespace: str, table: str, records: object
) -> ActionResult:
    if not isinstance(records, list):
        return create_error_result(
            "Invalid 'records' in data - must be a list",
            error_code="write.invalid_records",
        )
    inserted_ids = []
    for record in records:
        if isinstance(record, dict):
            record_id = provider.insert(namespace=namespace, table=table, data=record)
            inserted_ids.append(record_id)
    return create_success_result(
        {
            "namespace": namespace,
            "result": {"generated_ids": inserted_ids, "inserted": len(inserted_ids)},
        }
    )
