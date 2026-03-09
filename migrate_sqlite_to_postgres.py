import os
import sqlite3

import psycopg2
from psycopg2.extras import execute_batch

SQLITE_PATH = os.environ.get("SQLITE_PATH", "database.db")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise SystemExit("Falta DATABASE_URL")

sqlite_conn = sqlite3.connect(SQLITE_PATH)
sqlite_conn.row_factory = sqlite3.Row
sqlite_cur = sqlite_conn.cursor()

pg_conn = psycopg2.connect(DATABASE_URL)
pg_cur = pg_conn.cursor()


def rows(table):
    sqlite_cur.execute(f"SELECT * FROM {table} ORDER BY id")
    return sqlite_cur.fetchall()


def reset_identity(table):
    pg_cur.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1), true) FROM {table}"
    )

empleadas = [(r["id"], r["nombre"], r["password"], r["rol"]) for r in rows("empleadas")]
if empleadas:
    execute_batch(
        pg_cur,
        """
        INSERT INTO empleadas (id, nombre, password, rol)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (nombre) DO UPDATE SET
            password = EXCLUDED.password,
            rol = EXCLUDED.rol
        """,
        empleadas,
    )
    reset_identity("empleadas")

pg_cur.execute("DELETE FROM servicios")
servicios = [(r["id"], r["nombre"], r["duracion"]) for r in rows("servicios")]
if servicios:
    execute_batch(
        pg_cur,
        "INSERT INTO servicios (id, nombre, duracion) VALUES (%s, %s, %s)",
        servicios,
    )
    reset_identity("servicios")

pg_cur.execute("DELETE FROM clientas")
clientas = [(r["id"], r["nombre"], r["telefono"]) for r in rows("clientas")]
if clientas:
    execute_batch(
        pg_cur,
        "INSERT INTO clientas (id, nombre, telefono) VALUES (%s, %s, %s)",
        clientas,
    )
    reset_identity("clientas")

pg_cur.execute("DELETE FROM citas")
try:
    sqlite_cur.execute("SELECT id, nombre, servicio, fecha, horario, empleada, COALESCE(duracion, 60) AS duracion FROM citas ORDER BY id")
except sqlite3.Error:
    sqlite_cur.execute("SELECT id, nombre, servicio, fecha, horario, empleada, 60 AS duracion FROM citas ORDER BY id")

citas = [(r["id"], r["nombre"], r["servicio"], r["fecha"], r["horario"], r["empleada"], r["duracion"]) for r in sqlite_cur.fetchall()]
if citas:
    execute_batch(
        pg_cur,
        "INSERT INTO citas (id, nombre, servicio, fecha, horario, empleada, duracion) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        citas,
    )
    reset_identity("citas")

pg_conn.commit()
pg_cur.close()
pg_conn.close()
sqlite_conn.close()

print("Migracion terminada")
