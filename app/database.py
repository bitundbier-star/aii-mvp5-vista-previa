"""Configuración de base de datos (SQLite por defecto para el MVP)."""
import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'aii.db')}"
)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def asegurar_columnas_nuevas():
    """`create_all` crea tablas que no existen, pero nunca agrega columnas a
    una tabla que ya existe. Sin esto, una base que ya estaba en uso truena
    en cuanto el código espera una columna nueva. Aquí se agregan las que
    falten (todas las columnas nuevas son opcionales, así que basta con
    agregarlas vacías). No borra ni modifica nada de lo que ya existe."""
    inspector = inspect(engine)
    tablas = set(inspector.get_table_names())
    with engine.begin() as conn:
        for tabla in Base.metadata.sorted_tables:
            if tabla.name not in tablas:
                continue
            existentes = {c["name"] for c in inspector.get_columns(tabla.name)}
            for col in tabla.columns:
                if col.name in existentes:
                    continue
                tipo = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {tabla.name} ADD COLUMN {col.name} {tipo}'))
