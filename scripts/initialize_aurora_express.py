from __future__ import annotations

import argparse
import os

import psycopg
from psycopg import sql


APPLICATION_DATABASE = "walking_meditation"
APPLICATION_USER = "wm_app"


def required_environment_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize the Walking Meditation role and database on Aurora Express."
    )
    parser.add_argument("--host", required=True, help="Aurora Express cluster endpoint")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--admin-user", default="postgres")
    return parser.parse_args()


def connect(*, host: str, port: int, admin_user: str, token: str, database: str):
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=admin_user,
        password=token,
        sslmode="require",
        autocommit=True,
    )


def main() -> None:
    args = parse_args()
    application_password = required_environment_value("WM_APP_PASSWORD")
    admin_token = required_environment_value("PGPASSWORD")

    with connect(
        host=args.host,
        port=args.port,
        admin_user=args.admin_user,
        token=admin_token,
        database="postgres",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APPLICATION_USER,))
            role_exists = cursor.fetchone() is not None
            role_action = sql.SQL("ALTER ROLE") if role_exists else sql.SQL("CREATE ROLE")
            cursor.execute(
                sql.SQL("{} {} LOGIN PASSWORD {}").format(
                    role_action,
                    sql.Identifier(APPLICATION_USER),
                    sql.Literal(application_password),
                )
            )

            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (APPLICATION_DATABASE,),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    # The Express administrator cannot SET ROLE to wm_app, so it
                    # must remain the database owner. wm_app receives only the
                    # connection and schema privileges it needs below.
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(APPLICATION_DATABASE)
                    )
                )

    with connect(
        host=args.host,
        port=args.port,
        admin_user=args.admin_user,
        token=admin_token,
        database=APPLICATION_DATABASE,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(APPLICATION_DATABASE),
                    sql.Identifier(APPLICATION_USER),
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(
                    sql.Identifier(APPLICATION_USER)
                )
            )

    print("Aurora Express application role and database initialized successfully.")


if __name__ == "__main__":
    main()
