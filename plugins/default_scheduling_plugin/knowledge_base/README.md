# Scheduling Service Knowledge Base

Reference documentation for the scheduling service, which provides recurring and one-time delayed action execution.

## Contents

| File | Description |
|------|-------------|
| [scheduling_reference.md](scheduling_reference.md) | Complete API reference, action format, cron syntax, and usage patterns |

## Quick Reference

| Operation | Process Key | Use Case |
|-----------|-------------|----------|
| `create_cron_schedule` | `service_interface::scheduling_service::create_cron_schedule` | Recurring tasks (e.g., every minute, hourly) |
| `execute_in_seconds` | `service_interface::scheduling_service::execute_in_seconds` | One-time delayed execution |
| `clear_scheduled_action` | `service_interface::scheduling_service::clear_scheduled_action` | Cancel a specific schedule by ID |
| `clear_scheduled_actions_by_tag` | `service_interface::scheduling_service::clear_scheduled_actions_by_tag` | Cancel all schedules with a tag |
