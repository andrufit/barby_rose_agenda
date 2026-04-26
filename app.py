import os
import json
import sqlite3
from datetime import datetime, timedelta, date
from pathlib import Path
from urllib.parse import quote

from flask import Flask, render_template, request, redirect, url_for, session, g, jsonify

try:
    from twilio.rest import Client
    TWILIO_AVAILABLE = True
except Exception:
    Client = None
    TWILIO_AVAILABLE = False

try:
    import psycopg2
    from psycopg2.extras import DictCursor
    POSTGRES_AVAILABLE = True
except Exception:
    psycopg2 = None
    DictCursor = None
    POSTGRES_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_BACKEND = "postgres" if DATABASE_URL.startswith("postgres") else "sqlite"

DIRECCION_NEGOCIO = os.environ.get(
    "DIRECCION_NEGOCIO",
    "Barby Rose Nail Spa, Armenia, Quindio, Cr 15 4N # 55 (Barrio Nueva Cecilia)",
)
MAPS_URL = os.environ.get(
    "MAPS_URL",
    "https://maps.app.goo.gl/W8E5RV7LpcQbHDa2A",
)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()
TWILIO_CONTENT_SID_RECORDATORIO = os.environ.get("TWILIO_CONTENT_SID_RECORDATORIO", "").strip()


class DBConnectionWrapper:
    def __init__(self, conn, backend: str):
        self._conn = conn
        self.backend = backend

    def cursor(self):
        return DBCursorWrapper(self._conn.cursor(), self.backend)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


class DBCursorWrapper:
    def __init__(self, cursor, backend: str):
        self._cursor = cursor
        self.backend = backend

    def _adapt(self, query: str) -> str:
        if self.backend == "postgres":
            return query.replace("?", "%s")
        return query

    def execute(self, query: str, params=None):
        query = self._adapt(query)
        if params is None:
            return self._cursor.execute(query)
        return self._cursor.execute(query, params)

    def executemany(self, query: str, seq):
        query = self._adapt(query)
        return self._cursor.executemany(query, seq)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)


def choose_database() -> Path:
    database_db = BASE_DIR / "database.db"
    citas_db = BASE_DIR / "citas.db"

    if database_db.exists() and citas_db.exists():
        def counts(path: Path) -> dict:
            data = {"empleadas": 0, "citas": 0, "servicios": 0, "clientas": 0}
            try:
                con = sqlite3.connect(path)
                cur = con.cursor()
                for table in data:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        data[table] = cur.fetchone()[0]
                    except sqlite3.Error:
                        data[table] = 0
                con.close()
            except sqlite3.Error:
                pass
            return data

        db_counts = counts(database_db)
        citas_counts = counts(citas_db)

        if db_counts["citas"] == 0 and citas_counts["citas"] > 0:
            return citas_db
        if db_counts["empleadas"] <= 1 and citas_counts["empleadas"] > 1:
            return citas_db
        return database_db

    if database_db.exists():
        return database_db
    if citas_db.exists():
        return citas_db
    return database_db


SQLITE_DATABASE = choose_database()


def get_db():
    if "db" not in g:
        if DB_BACKEND == "postgres":
            if not POSTGRES_AVAILABLE:
                raise RuntimeError("psycopg2-binary no esta instalado")
            raw = psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)
            g.db = DBConnectionWrapper(raw, "postgres")
        else:
            raw = sqlite3.connect(SQLITE_DATABASE)
            raw.row_factory = sqlite3.Row
            g.db = DBConnectionWrapper(raw, "sqlite")
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def login_required():
    if "empleada" not in session:
        return redirect(url_for("login"))
    return None


def admin_required():
    if "rol" not in session or session["rol"] != "admin":
        return redirect(url_for("login"))
    return None


def normalize_employee_options(rows):
    return [(row["nombre"], row["nombre"]) for row in rows]


def limpiar_numero_whatsapp(numero: str) -> str:
    if not numero:
        return ""
    numero = "".join(ch for ch in str(numero).strip() if ch.isdigit())
    if len(numero) == 10:
        numero = "57" + numero
    return numero


def normalize_phone(numero: str) -> str:
    return "".join(ch for ch in str(numero or "").strip() if ch.isdigit())


def buscar_clienta_por_telefono(cursor, telefono: str):
    objetivo = normalize_phone(telefono)
    if not objetivo:
        return None

    cursor.execute("SELECT id, nombre, telefono FROM clientas ORDER BY id DESC")
    for row in cursor.fetchall():
        if normalize_phone(row["telefono"]) == objetivo:
            return row
    return None


def guardar_o_actualizar_clienta_por_telefono(cursor, nombre: str, telefono: str):
    nombre = (nombre or "").strip()
    telefono = (telefono or "").strip()

    if not telefono:
        return None

    existente = buscar_clienta_por_telefono(cursor, telefono)
    if existente:
        if nombre and nombre != existente["nombre"]:
            cursor.execute(
                "UPDATE clientas SET nombre=?, telefono=? WHERE id=?",
                (nombre, telefono, existente["id"]),
            )
            cursor.execute("SELECT id, nombre, telefono FROM clientas WHERE id=?", (existente["id"],))
            return cursor.fetchone()

        if telefono != (existente["telefono"] or ""):
            cursor.execute(
                "UPDATE clientas SET telefono=? WHERE id=?",
                (telefono, existente["id"]),
            )
            cursor.execute("SELECT id, nombre, telefono FROM clientas WHERE id=?", (existente["id"],))
            return cursor.fetchone()
        return existente

    if not nombre:
        return None

    cursor.execute(
        "INSERT INTO clientas (nombre, telefono) VALUES (?, ?)",
        (nombre, telefono),
    )
    cursor.execute(
        "SELECT id, nombre, telefono FROM clientas WHERE telefono=? ORDER BY id DESC LIMIT 1",
        (telefono,),
    )
    return cursor.fetchone()


def get_service_duration(cursor, service_name: str) -> int:
    cursor.execute("SELECT duracion FROM servicios WHERE nombre=?", (service_name,))
    row = cursor.fetchone()
    if row and row[0]:
        return int(row[0])
    return 60


def has_schedule_conflict(cursor, fecha: str, horario: str, duracion: int, empleada: str, cita_id=None) -> bool:
    inicio = datetime.strptime(horario, "%H:%M")
    fin = inicio + timedelta(minutes=duracion)

    query = """
        SELECT id, horario, COALESCE(duracion, 60) AS duracion
        FROM citas
        WHERE fecha=? AND empleada=?
    """
    params = [fecha, empleada]

    if cita_id is not None:
        query += " AND id != ?"
        params.append(cita_id)

    cursor.execute(query, params)
    citas = cursor.fetchall()

    for cita in citas:
        inicio_cita = datetime.strptime(cita[1], "%H:%M")
        fin_cita = inicio_cita + timedelta(minutes=int(cita[2] or 60))
        if (inicio < fin_cita) and (fin > inicio_cita):
            return True
    return False


def build_whatsapp_url(nombre: str, servicio: str, fecha_cita: str, horario: str, empleada: str, telefono: str) -> str:
    telefono = limpiar_numero_whatsapp(telefono)
    mensaje = (
        f"Hola {nombre}, te recordamos tu cita en Barby Rose.\n\n"
        f"Servicio: {servicio}\n"
        f"Fecha: {fecha_cita}\n"
        f"Hora: {horario}\n"
        f"Atiende: {empleada}\n\n"
        f"Direccion: {DIRECCION_NEGOCIO}\n"
        f"Ubicacion en Maps: {MAPS_URL}\n\n"
        "Por favor confirma tu asistencia. 💅"
    )
    return f"https://web.whatsapp.com/send?phone={telefono}&text={quote(mensaje)}"


def enviar_template_whatsapp(telefono: str, variables: dict):
    telefono = limpiar_numero_whatsapp(telefono)

    if not TWILIO_AVAILABLE or Client is None:
        return {"ok": False, "error": "La libreria twilio no esta instalada"}

    if not telefono:
        return {"ok": False, "error": "Telefono invalido"}

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"ok": False, "error": "Faltan credenciales de Twilio"}

    if not TWILIO_WHATSAPP_FROM:
        return {"ok": False, "error": "Falta TWILIO_WHATSAPP_FROM"}

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:+{telefono}",
            body=f"Hola 👋 tu cita es el {variables.get('fecha')} a las {variables.get('hora')} 💅",
        )

        return {"ok": True, "sid": msg.sid, "status": msg.status}

    except Exception as e:
        return {"ok": False, "error": str(e)}

def init_db():
    db = get_db()
    cursor = db.cursor()

    if DB_BACKEND == "postgres":
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS empleadas (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                nombre TEXT UNIQUE,
                password TEXT,
                rol TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS servicios (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                nombre TEXT,
                duracion INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS citas (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                nombre TEXT,
                servicio TEXT,
                fecha TEXT,
                horario TEXT,
                empleada TEXT,
                duracion INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS clientas (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                nombre TEXT NOT NULL,
                telefono TEXT
            )
            """
        )
        cursor.execute("ALTER TABLE citas ADD COLUMN IF NOT EXISTS duracion INTEGER DEFAULT 60")
    else:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS empleadas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT UNIQUE,
                password TEXT,
                rol TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS servicios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT,
                duracion INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS citas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT,
                servicio TEXT,
                fecha TEXT,
                horario TEXT,
                empleada TEXT,
                duracion INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS clientas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                telefono TEXT
            )
            """
        )
        cursor.execute("PRAGMA table_info(citas)")
        columnas_citas = [row[1] for row in cursor.fetchall()]
        if "duracion" not in columnas_citas:
            cursor.execute("ALTER TABLE citas ADD COLUMN duracion INTEGER DEFAULT 60")

    cursor.execute("SELECT * FROM empleadas WHERE nombre=?", ("admin",))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO empleadas (nombre,password,rol) VALUES (?,?,?)",
            ("admin", "1234", "admin"),
        )

    cursor.execute("SELECT COUNT(*) FROM servicios")
    total_servicios = cursor.fetchone()[0]
    if total_servicios == 0:
        servicios = [
            ("Semi decorado (2 uñas)", 90),
            ("Semi ojo de gato", 90),
            ("Pies semi", 90),
            ("Manos tradicionales", 60),
            ("Pies tradicionales", 60),
            ("Pedi spa tradicional", 90),
            ("Pedi spa semi", 90),
            ("Montaje press", 120),
            ("Retoque press", 120),
            ("Montaje acrilico", 120),
            ("Retoque acrilico", 120),
            ("Montaje poly gel", 120),
            ("Retoque poly gel", 120),
            ("Dual sistem", 120),
            ("Forrado acrilico", 120),
            ("Forrado base rubber", 120),
            ("Dip power", 120),
            ("Manos caballero", 60),
            ("Pies caballero", 60),
            ("Retiro acrilico Barby Rose", 30),
            ("Retiro acrilico otro lugar", 30),
        ]
        cursor.executemany(
            "INSERT INTO servicios (nombre,duracion) VALUES (?,?)",
            servicios,
        )

    cursor.execute("UPDATE citas SET duracion = 60 WHERE duracion IS NULL")
    db.commit()


@app.before_request
def setup_database_once():
    if not app.config.get("DB_READY"):
        init_db()
        app.config["DB_READY"] = True


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        password = request.form.get("password", "").strip()

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM empleadas WHERE nombre=?", (nombre,))
        user = cursor.fetchone()

        if user and user["password"] == password:
            session["empleada"] = user["nombre"]
            session["rol"] = user["rol"]
            if user["rol"] == "admin":
                return redirect(url_for("admin_panel"))
            return redirect(url_for("mis_citas"))

        return "Credenciales incorrectas", 401

    return render_template("login.html")


@app.route("/admin")
def admin_panel():
    guard = admin_required()
    if guard:
        return guard

    fecha = request.args.get("fecha", "").strip() or date.today().isoformat()
    empleada = request.args.get("empleada", "").strip()
    db = get_db()
    cursor = db.cursor()

    if empleada:
        cursor.execute("""
        SELECT id, nombre, servicio, fecha, horario, empleada
        FROM citas
        WHERE fecha=? AND empleada=?
        ORDER BY horario
    """, (fecha, empleada))
    else:
     cursor.execute("""
        SELECT id, nombre, servicio, fecha, horario, empleada
        FROM citas
        WHERE fecha=?
        ORDER BY horario
    """, (fecha,))
    citas = cursor.fetchall()

    cursor.execute("SELECT id, nombre, rol FROM empleadas ORDER BY nombre")
    empleadas = cursor.fetchall()

    return render_template("admin.html", citas=citas, empleadas=empleadas, fecha_filtro=fecha,empleada_filtro=empleada, hoy=date.today().isoformat())


@app.route("/mis_citas")
def mis_citas():
    guard = login_required()
    if guard:
        return guard

    fecha = request.args.get("fecha", "").strip() or date.today().isoformat()
    db = get_db()
    cursor = db.cursor()

    cursor.execute(
        """
        SELECT id, nombre, servicio, fecha, horario
        FROM citas
        WHERE empleada=? AND fecha=?
        ORDER BY horario
        """,
        (session["empleada"], fecha),
    )

    citas = cursor.fetchall()
    return render_template(
        "mis_citas.html",
        citas=citas,
        fecha_filtro=fecha,
        hoy=date.today().isoformat(),
        empleada=session.get("empleada", ""),
    )


@app.route("/clientas", methods=["GET", "POST"])
def clientas():
    guard = admin_required()
    if guard:
        return guard

    db = get_db()
    cursor = db.cursor()

    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        telefono = request.form.get("telefono", "").strip()

        if not nombre:
            return "El nombre es obligatorio", 400

        cursor.execute(
            "INSERT INTO clientas (nombre, telefono) VALUES (?, ?)",
            (nombre, telefono),
        )
        db.commit()
        return redirect(url_for("clientas"))

    telefono_filtro = request.args.get("telefono", "").strip()
    edit_id = request.args.get("edit_id", "").strip()
    edit_clienta = None

    cursor.execute("SELECT * FROM clientas ORDER BY nombre")
    todas = cursor.fetchall()

    if telefono_filtro:
        telefono_normalizado = normalize_phone(telefono_filtro)
        clientas_lista = [
            c for c in todas
            if telefono_normalizado in normalize_phone(c["telefono"] or "")
        ]
    else:
        clientas_lista = todas

    if edit_id:
        cursor.execute("SELECT * FROM clientas WHERE id=?", (edit_id,))
        edit_clienta = cursor.fetchone()

    return render_template(
        "clientas.html",
        clientas=clientas_lista,
        telefono_filtro=telefono_filtro,
        edit_clienta=edit_clienta,
    )


@app.route("/actualizar_clienta/<int:clienta_id>", methods=["POST"])
def actualizar_clienta(clienta_id):
    guard = admin_required()
    if guard:
        return guard

    nombre = request.form.get("nombre", "").strip()
    telefono = request.form.get("telefono", "").strip()

    if not nombre:
        return "El nombre es obligatorio", 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "UPDATE clientas SET nombre=?, telefono=? WHERE id=?",
        (nombre, telefono, clienta_id),
    )
    db.commit()
    return redirect(url_for("clientas", telefono=request.args.get("telefono", "").strip()))


@app.route("/eliminar_clienta/<int:clienta_id>", methods=["POST"])
def eliminar_clienta(clienta_id):
    guard = admin_required()
    if guard:
        return guard

    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM clientas WHERE id=?", (clienta_id,))
    db.commit()
    return redirect(url_for("clientas", telefono=request.args.get("telefono", "").strip()))


@app.route("/crear_empleada", methods=["GET", "POST"])
def crear_empleada():
    guard = admin_required()
    if guard:
        return guard

    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        password = request.form.get("password", "").strip()
        rol = request.form.get("rol", "").strip()

        if not nombre or not password or rol not in {"admin", "empleada"}:
            return "Datos invalidos", 400

        db = get_db()
        cursor = db.cursor()
        try:
            cursor.execute(
                "INSERT INTO empleadas (nombre, password, rol) VALUES (?, ?, ?)",
                (nombre, password, rol),
            )
            db.commit()
        except Exception:
            db.rollback()
            return "Ya existe una empleada con ese nombre", 400

        return redirect(url_for("admin_panel"))

    return render_template("crear_empleada.html")


@app.route("/eliminar_empleada/<nombre>")
def eliminar_empleada(nombre):
    guard = admin_required()
    if guard:
        return guard

    if nombre == "admin":
        return "No se puede eliminar la cuenta admin", 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM empleadas WHERE nombre=?", (nombre,))
    db.commit()
    return redirect(url_for("admin_panel"))


@app.route("/editar_empleada/<int:empleada_id>", methods=["GET", "POST"])
def editar_empleada(empleada_id):
    guard = admin_required()
    if guard:
        return guard

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, nombre, password, rol FROM empleadas WHERE id=?", (empleada_id,))
    empleada = cursor.fetchone()

    if not empleada:
        return "Empleada no encontrada", 404

    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        password = request.form.get("password", "").strip()
        rol = request.form.get("rol", "").strip()

        if not nombre or rol not in {"admin", "empleada"}:
            return "Datos invalidos", 400

        if not password:
            password = empleada["password"]

        try:
            cursor.execute(
                "UPDATE empleadas SET nombre=?, password=?, rol=? WHERE id=?",
                (nombre, password, rol, empleada_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            return "Ya existe una empleada con ese nombre", 400

        return redirect(url_for("admin_panel"))

    return render_template("editar_empleada.html", empleada=empleada)


@app.route("/buscar_clienta_por_telefono")
def buscar_clienta_por_telefono_api():
    guard = admin_required()
    if guard:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    telefono = request.args.get("telefono", "").strip()
    if not telefono:
        return jsonify({"ok": False, "encontrada": False})

    db = get_db()
    cursor = db.cursor()
    clienta = buscar_clienta_por_telefono(cursor, telefono)
    if clienta:
        return jsonify({
            "ok": True,
            "encontrada": True,
            "id": clienta["id"],
            "nombre": clienta["nombre"],
            "telefono": clienta["telefono"] or "",
        })

    return jsonify({"ok": True, "encontrada": False})


@app.route("/agendar", methods=["GET", "POST"])
def agendar():
    guard = admin_required()
    if guard:
        return guard

    db = get_db()
    cursor = db.cursor()

    cursor.execute("SELECT nombre FROM empleadas WHERE rol='empleada' ORDER BY nombre")
    empleadas_rows = cursor.fetchall()
    empleadas = normalize_employee_options(empleadas_rows)

    cursor.execute("SELECT id, nombre, duracion FROM servicios ORDER BY nombre")
    servicios = cursor.fetchall()

    if request.method == "POST":
        telefono = request.form.get("telefono", "").strip()
        cliente = request.form.get("nombre", "").strip()
        servicio_id = request.form.get("servicio", "").strip()
        servicio_extra_id = request.form.get("servicio_extra", "").strip()
        empleada_extra = request.form.get("empleada_extra", "").strip()
        hora_extra = request.form.get("hora_extra", "").strip()
        fecha = request.form.get("fecha", "").strip()
        horario = request.form.get("horario", "").strip()
        empleada = request.form.get("empleada", "").strip()

        # Validación básica
        if not all([telefono, servicio_id, fecha, horario, empleada]):
            return "Telefono, servicio, fecha, hora y empleada son obligatorios", 400

        # Buscar clienta
        clienta_existente = buscar_clienta_por_telefono(cursor, telefono)
        if clienta_existente and not cliente:
            cliente = clienta_existente["nombre"]

        if not cliente:
            return "Si el telefono no existe, debes escribir el nombre de la clienta", 400

        # 🔹 Servicio principal
        cursor.execute("SELECT nombre, duracion FROM servicios WHERE id=?", (servicio_id,))
        servicio = cursor.fetchone()
        if not servicio:
            return "Servicio no encontrado", 404

        nombre_servicio = servicio["nombre"]
        duracion = int(servicio["duracion"] or 60)

        # 🔹 Servicio adicional
        crear_segunda_cita = False
        servicio_extra = None

        if servicio_extra_id:
            cursor.execute("SELECT nombre, duracion FROM servicios WHERE id=?", (servicio_extra_id,))
            servicio_extra = cursor.fetchone()

            if servicio_extra:
                # 👉 MISMA EMPLEADA → unir servicios
                if not empleada_extra or empleada_extra == empleada:
                    nombre_servicio += " + " + servicio_extra["nombre"]
                    duracion += int(servicio_extra["duracion"] or 60)

                # 👉 DIFERENTE EMPLEADA → NO tocar principal
                else:
                    crear_segunda_cita = True

        # 🔹 Validar hora principal
        try:
            datetime.strptime(horario, "%H:%M")
        except ValueError:
            return "Hora invalida", 400

        # 🔹 Validar conflicto principal
        if has_schedule_conflict(cursor, fecha, horario, duracion, empleada):
            return "Horario ocupado", 400

        # 🔹 Validar hora adicional
        if crear_segunda_cita:
            if not hora_extra:
                return "Debes ingresar hora para el servicio adicional"

            try:
                datetime.strptime(hora_extra, "%H:%M")
            except ValueError:
                return "Hora adicional invalida", 400

            # (Opcional pero recomendado) validar conflicto extra
            if has_schedule_conflict(cursor, fecha, hora_extra, int(servicio_extra["duracion"] or 60), empleada_extra):
                return "Horario adicional ocupado", 400

        # 🔹 Guardar clienta
        clienta_guardada = guardar_o_actualizar_clienta_por_telefono(cursor, cliente, telefono)
        if clienta_guardada:
            cliente = clienta_guardada["nombre"]

        # 🔹 Insert principal
        cursor.execute("""
            INSERT INTO citas (nombre, servicio, fecha, horario, empleada, duracion)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (cliente, nombre_servicio, fecha, horario, empleada, duracion))

        # 🔹 Insert adicional (SOLO si es otra empleada)
        if crear_segunda_cita:
            cursor.execute("""
                INSERT INTO citas (nombre, servicio, fecha, horario, empleada, duracion)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                cliente,
                servicio_extra["nombre"],
                fecha,
                hora_extra,
                empleada_extra,
                int(servicio_extra["duracion"] or 60)
            ))

        db.commit()

        resultado_twilio = enviar_template_whatsapp(
            telefono,
            {
                "fecha": fecha,
                "hora": horario,
            },
        )

        print("Resultado Twilio al agendar:", resultado_twilio)

        return redirect(url_for("admin_panel"))

    return render_template(
        "agendar.html",
        empleadas=empleadas,
        servicios=servicios,
        fecha_actual=date.today().isoformat(),
    )


@app.route("/editar_cita/<int:id>")
def editar_cita(id):
    guard = admin_required()
    if guard:
        return guard

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT c.id, c.nombre AS cliente, c.servicio, c.fecha, c.horario AS hora, c.empleada,
               COALESCE(cl.telefono, '') AS telefono
        FROM citas c
        LEFT JOIN clientas cl ON TRIM(LOWER(cl.nombre)) = TRIM(LOWER(c.nombre))
        WHERE c.id=?
        LIMIT 1
        """,
        (id,),
    )
    cita = cursor.fetchone()

    if not cita:
        return "Cita no encontrada", 404

    cursor.execute("SELECT nombre FROM servicios ORDER BY nombre")
    servicios_rows = cursor.fetchall()
    servicios = [(row["nombre"],) for row in servicios_rows]
    if (cita["servicio"],) not in servicios:
        servicios.insert(0, (cita["servicio"],))

    cursor.execute("SELECT nombre FROM empleadas WHERE rol='empleada' ORDER BY nombre")
    empleadas_rows = cursor.fetchall()
    empleadas = normalize_employee_options(empleadas_rows)
    if (cita["empleada"], cita["empleada"]) not in empleadas:
        empleadas.insert(0, (cita["empleada"], cita["empleada"]))

    return render_template("editar_cita.html", cita=cita, servicios=servicios, empleadas=empleadas)


@app.route("/actualizar_cita", methods=["POST"])
def actualizar_cita():
    guard = admin_required()
    if guard:
        return guard

    cita_id = request.form.get("id", "").strip()
    cliente = request.form.get("cliente", "").strip()
    telefono = request.form.get("telefono", "").strip()
    servicio = request.form.get("servicio", "").strip()
    fecha = request.form.get("fecha", "").strip()
    hora = request.form.get("hora", "").strip()
    empleada = request.form.get("empleada", "").strip()

    if not all([cita_id, cliente, servicio, fecha, hora, empleada]):
        return "Todos los campos son obligatorios", 400

    db = get_db()
    cursor = db.cursor()
    duracion = get_service_duration(cursor, servicio)

    try:
        cita_id_int = int(cita_id)
        datetime.strptime(hora, "%H:%M")
    except ValueError:
        return "Datos invalidos", 400

    if has_schedule_conflict(cursor, fecha, hora, duracion, empleada, cita_id=cita_id_int):
        return "Horario ocupado", 400

    cursor.execute(
        """
        UPDATE citas
        SET nombre=?, servicio=?, fecha=?, horario=?, empleada=?, duracion=?
        WHERE id=?
        """,
        (cliente, servicio, fecha, hora, empleada, duracion, cita_id_int),
    )

    if telefono:
        guardar_o_actualizar_clienta_por_telefono(cursor, cliente, telefono)

    db.commit()
    return redirect(url_for("admin_panel", fecha=fecha))


@app.route("/eliminar/<int:id>")
def eliminar(id):
    guard = admin_required()
    if guard:
        return guard

    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM citas WHERE id=?", (id,))
    db.commit()
    return redirect(url_for("admin_panel"))


@app.route("/calendario_pro")
def calendario_pro():
    guard = admin_required()
    if guard:
        return guard

    fecha = request.args.get("fecha", "").strip() or date.today().isoformat()
    db = get_db()
    cursor = db.cursor()

    cursor.execute("SELECT nombre FROM empleadas WHERE rol='empleada' ORDER BY nombre")
    empleadas = cursor.fetchall()

    cursor.execute(
        """
        SELECT c.nombre, c.servicio, c.horario, c.empleada,
               COALESCE(c.duracion, s.duracion, 60) AS duracion
        FROM citas c
        LEFT JOIN servicios s ON s.nombre = c.servicio
        WHERE c.fecha=?
        ORDER BY c.horario
        """,
        (fecha,),
    )
    rows = cursor.fetchall()

    citas = []
    for row in rows:
        try:
            hora = datetime.strptime(row["horario"], "%H:%M")
        except ValueError:
            continue

        minutos_desde_8 = (hora.hour - 8) * 60 + hora.minute
        top = max(0, minutos_desde_8)
        height = max(30, int(row["duracion"] or 60))

        citas.append({
            "cliente": row["nombre"],
            "servicio": row["servicio"],
            "empleada": row["empleada"],
            "top": top,
            "height": height,
        })

    return render_template("calendario_pro.html", fecha=fecha, empleadas=empleadas, citas=citas)


@app.route("/recordatorios_whatsapp")
def recordatorios_whatsapp():
    guard = admin_required()
    if guard:
        return guard

    fecha = request.args.get("fecha", "").strip() or date.today().isoformat()
    db = get_db()
    cursor = db.cursor()

    cursor.execute(
        """
        SELECT c.id, c.nombre, c.servicio, c.fecha, c.horario, c.empleada, cl.telefono
        FROM citas c
        LEFT JOIN clientas cl ON TRIM(LOWER(cl.nombre)) = TRIM(LOWER(c.nombre))
        WHERE c.fecha=?
        ORDER BY c.horario
        """,
        (fecha,),
    )
    rows = cursor.fetchall()

    recordatorios = []
    con_telefono = 0
    for row in rows:
        telefono = row["telefono"] or ""
        telefono_limpio = limpiar_numero_whatsapp(telefono)
        url = ""
        if telefono_limpio:
            con_telefono += 1
            url = build_whatsapp_url(row["nombre"], row["servicio"], row["fecha"], row["horario"], row["empleada"], telefono)

        recordatorios.append({
            "id": row["id"],
            "nombre": row["nombre"],
            "servicio": row["servicio"],
            "fecha": row["fecha"],
            "horario": row["horario"],
            "empleada": row["empleada"],
            "telefono": telefono,
            "tiene_telefono": bool(telefono_limpio),
            "whatsapp_url": url,
        })

    return render_template(
        "recordatorios_whatsapp.html",
        fecha=fecha,
        recordatorios=recordatorios,
        total=len(recordatorios),
        con_telefono=con_telefono,
        sin_telefono=len(recordatorios) - con_telefono,
        direccion_negocio=DIRECCION_NEGOCIO,
        maps_url=MAPS_URL,
    )


@app.route("/recordatorios_whatsapp_secuencial")
def recordatorios_whatsapp_secuencial():
    guard = admin_required()
    if guard:
        return guard

    fecha = request.args.get("fecha", "").strip() or date.today().isoformat()
    indice_raw = request.args.get("indice", "0").strip()
    try:
        indice = max(0, int(indice_raw))
    except ValueError:
        indice = 0

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT c.id, c.nombre, c.servicio, c.fecha, c.horario, c.empleada, cl.telefono
        FROM citas c
        LEFT JOIN clientas cl ON TRIM(LOWER(cl.nombre)) = TRIM(LOWER(c.nombre))
        WHERE c.fecha=?
        ORDER BY c.horario
        """,
        (fecha,),
    )
    rows = cursor.fetchall()

    recordatorios = []
    for row in rows:
        telefono = row["telefono"] or ""
        telefono_limpio = limpiar_numero_whatsapp(telefono)
        recordatorios.append({
            "id": row["id"],
            "nombre": row["nombre"],
            "servicio": row["servicio"],
            "fecha": row["fecha"],
            "horario": row["horario"],
            "empleada": row["empleada"],
            "telefono": telefono,
            "tiene_telefono": bool(telefono_limpio),
            "whatsapp_url": build_whatsapp_url(row["nombre"], row["servicio"], row["fecha"], row["horario"], row["empleada"], telefono) if telefono_limpio else "",
        })

    total = len(recordatorios)
    actual = recordatorios[indice] if total and indice < total else None
    anterior_indice = indice - 1 if actual and indice > 0 else None
    siguiente_indice = indice + 1 if actual and indice < total - 1 else None

    return render_template(
        "recordatorio_secuencial.html",
        fecha=fecha,
        total=total,
        indice=indice,
        actual=actual,
        anterior_indice=anterior_indice,
        siguiente_indice=siguiente_indice,
        direccion_negocio=DIRECCION_NEGOCIO,
        maps_url=MAPS_URL,
    )


@app.route("/recordatorio_whatsapp/<int:cita_id>")
def recordatorio_whatsapp(cita_id):
    guard = admin_required()
    if guard:
        return guard

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT c.id, c.nombre, c.servicio, c.fecha, c.horario, c.empleada, cl.telefono
        FROM citas c
        LEFT JOIN clientas cl ON TRIM(LOWER(cl.nombre)) = TRIM(LOWER(c.nombre))
        WHERE c.id=?
        """,
        (cita_id,),
    )
    cita = cursor.fetchone()

    if not cita:
        return "Cita no encontrada", 404
    if not cita["telefono"]:
        return "La clienta no tiene telefono registrado", 404

    telefono = limpiar_numero_whatsapp(cita["telefono"])
    if not telefono:
        return "Numero de telefono invalido", 400

    return redirect(build_whatsapp_url(cita["nombre"], cita["servicio"], cita["fecha"], cita["horario"], cita["empleada"], cita["telefono"]))


@app.route("/test_whatsapp")
def test_whatsapp():
    # Ruta aislada para probar Twilio sin tocar la agenda.
    # Cambia este numero por el tuyo si vas a probar con otro telefono.
    resultado = enviar_template_whatsapp(
        "573214627686",
        {
            "fecha": "12/1",
            "hora": "3pm",
        },
    )
    return jsonify(resultado)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=8000, debug=True)