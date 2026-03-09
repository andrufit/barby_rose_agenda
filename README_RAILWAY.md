# Barby Rose listo para Railway

## 1) Subir el proyecto a GitHub
Sube esta carpeta completa a un repositorio.

## 2) Crear proyecto en Railway
- New Project
- Deploy from GitHub repo
- Selecciona el repositorio

## 3) Agregar PostgreSQL
Dentro del proyecto:
- New -> Database -> PostgreSQL

Railway creara `DATABASE_URL` automaticamente.

## 4) Variables de entorno del servicio web
Agrega estas variables:
- `SECRET_KEY`
- `MAPS_URL`
- `DIRECCION_NEGOCIO`

## 5) Start command
Railway debe usar:
`gunicorn app:app`

## 6) Migrar datos desde SQLite a PostgreSQL
Despues del primer deploy:
- abre un shell del servicio web o corre local con la `DATABASE_URL` de Railway
- ejecuta:

```bash
python migrate_sqlite_to_postgres.py
```

Si quieres usar `citas.db` en vez de `database.db`:

```bash
SQLITE_PATH=citas.db python migrate_sqlite_to_postgres.py
```

## 7) Login inicial
- usuario: `admin`
- clave: `1234`

## 8) Nota importante
En Railway la app usara PostgreSQL si existe `DATABASE_URL`.
Si corres el proyecto en local sin `DATABASE_URL`, seguira usando SQLite.
