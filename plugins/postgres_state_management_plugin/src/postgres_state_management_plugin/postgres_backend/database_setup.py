"""
Database Setup Utilities for start_build Wizard

Standalone database creation functions called by the solets setup wizard.
All connection parameters come from user input during setup.
"""

import psycopg
from ananta.interfaces.state_provider_interface import SetupResult


def test_connection(
    host: str,
    port: int,
    user: str,
    password: str,
    system_database: str,
) -> SetupResult:
    """
    Test PostgreSQL server connection.

    Args:
        host: PostgreSQL server host
        port: PostgreSQL server port
        user: Database user
        password: Database password
        system_database: PostgreSQL system database to connect to

    Returns:
        SetupResult with connection status and server version
    """
    try:
        conninfo = (
            f"host={host} port={port} dbname={system_database} "
            f"user={user} password={password} connect_timeout=10"
        )
        with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            row = cur.fetchone()
            version = row[0] if row else "unknown"

        return SetupResult(
            success=True,
            message="Connected to PostgreSQL",
            details={"version": version, "host": host, "port": port},
        )
    except psycopg.OperationalError as e:
        return SetupResult(
            success=False,
            message=f"Connection failed: {e}",
            details={"error": str(e), "host": host, "port": port},
        )


def database_exists(
    host: str,
    port: int,
    user: str,
    password: str,
    system_database: str,
    database: str,
) -> bool:
    """
    Check if a database exists.

    Args:
        host: PostgreSQL server host
        port: PostgreSQL server port
        user: Database user
        password: Database password
        system_database: PostgreSQL system database to connect to
        database: Database name to check

    Returns:
        True if database exists, False otherwise
    """
    try:
        conninfo = (
            f"host={host} port={port} dbname={system_database} "
            f"user={user} password={password} connect_timeout=10"
        )
        with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (database,),
            )
            return cur.fetchone() is not None
    except psycopg.Error:
        return False


def create_database(
    host: str,
    port: int,
    user: str,
    password: str,
    system_database: str,
    database: str,
) -> SetupResult:
    """
    Create a PostgreSQL database if it doesn't exist.

    Args:
        host: PostgreSQL server host
        port: PostgreSQL server port
        user: Database user
        password: Database password
        system_database: PostgreSQL system database to connect to
        database: Database name to create

    Returns:
        SetupResult with creation status
    """
    conninfo = (
        f"host={host} port={port} dbname={system_database} "
        f"user={user} password={password} connect_timeout=10"
    )

    try:
        # Need autocommit=True for CREATE DATABASE (can't run in transaction)
        with psycopg.connect(conninfo, autocommit=True) as conn, conn.cursor() as cur:
            # Check if database already exists
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (database,),
            )
            if cur.fetchone():
                return SetupResult(
                    success=True,
                    message=f"Database '{database}' already exists",
                    details={"created": False, "database": database},
                )

            # Create database
            # Use SQL identifier quoting to prevent injection
            cur.execute(
                psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(database))  # type: ignore[attr-defined]
            )

            return SetupResult(
                success=True,
                message=f"Database '{database}' created",
                details={"created": True, "database": database},
            )

    except psycopg.Error as e:
        return SetupResult(
            success=False,
            message=f"Failed to create database: {e}",
            details={"error": str(e), "database": database},
        )


def create_schema(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    schema: str,
) -> SetupResult:
    """
    Create a schema within a database if it doesn't exist.

    Args:
        host: PostgreSQL server host
        port: PostgreSQL server port
        user: Database user
        password: Database password
        database: Database name
        schema: Schema name to create

    Returns:
        SetupResult with creation status
    """
    conninfo = (
        f"host={host} port={port} dbname={database} "
        f"user={user} password={password} connect_timeout=10"
    )

    try:
        with psycopg.connect(conninfo, autocommit=True) as conn, conn.cursor() as cur:
            # Check if schema exists
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (schema,),
            )
            if cur.fetchone():
                return SetupResult(
                    success=True,
                    message=f"Schema '{schema}' already exists",
                    details={"created": False, "schema": schema, "database": database},
                )

            # Create schema
            cur.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))  # type: ignore[attr-defined]

            return SetupResult(
                success=True,
                message=f"Schema '{schema}' created in database '{database}'",
                details={"created": True, "schema": schema, "database": database},
            )

    except psycopg.Error as e:
        return SetupResult(
            success=False,
            message=f"Failed to create schema: {e}",
            details={"error": str(e), "schema": schema, "database": database},
        )


def setup_database(
    host: str,
    port: int,
    user: str,
    password: str,
    system_database: str,
    database: str,
    schema: str,
) -> SetupResult:
    """
    Full database setup: create database and schema.

    This is the main entry point for start_build wizard.

    Args:
        host: PostgreSQL server host
        port: PostgreSQL server port
        user: Database user
        password: Database password
        system_database: PostgreSQL system database to connect to
        database: Database name to create
        schema: Schema name to create

    Returns:
        SetupResult with overall status
    """
    # Test connection first
    conn_result = test_connection(host, port, user, password, system_database)
    if not conn_result.success:
        return conn_result

    # Create database
    db_result = create_database(host, port, user, password, system_database, database)
    if not db_result.success:
        return db_result

    # Create schema
    schema_result = create_schema(host, port, user, password, database, schema)
    if not schema_result.success:
        return schema_result

    return SetupResult(
        success=True,
        message=f"Database '{database}' ready with schema '{schema}'",
        details={
            "database": database,
            "schema": schema,
            "database_created": db_result.details.get("created", False),
            "schema_created": schema_result.details.get("created", False),
            "server_version": conn_result.details.get("version"),
        },
    )
