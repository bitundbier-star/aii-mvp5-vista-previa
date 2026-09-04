"""Modelos de datos del MVP: Herramienta 2 (vecinos que se auto-administran).

Cada Conjunto es un cliente (el conjunto habitacional en su totalidad, no el
vecino administrador). El administrador en turno es el único usuario con
acceso, autenticado con correo y contraseña.
"""
import datetime as dt

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


def hoy():
    return dt.date.today()


def ahora():
    return dt.datetime.utcnow()


# ---------------------------------------------------------------------------
# Catálogos compartidos (se usan en los formularios y en los cálculos)
# ---------------------------------------------------------------------------

TIPOS_PROPIEDAD = [
    ("casa", "Casa"),
    ("departamento", "Departamento"),
    ("local", "Local comercial"),
    ("bodega", "Bodega"),
    ("estacionamiento", "Estacionamiento"),
    ("terreno", "Terreno"),
    ("otro", "Otro"),
]
TIPOS_PROPIEDAD_DICT = dict(TIPOS_PROPIEDAD)

CONCEPTOS_PAGO = [
    ("mantenimiento", "Mantenimiento"),
    ("gas", "Gas"),
    ("agua", "Agua"),
    ("otros", "Otros"),
    ("proyecto", "Proyecto"),
]
CONCEPTOS_PAGO_DICT = dict(CONCEPTOS_PAGO)

CONCEPTOS_QUE_ABONAN = {"mantenimiento", "proyecto"}

METODOS_PAGO = [
    ("efectivo", "Efectivo"),
    ("deposito", "Depósito"),
    ("cheque", "Cheque"),
    ("otro", "Otro"),
]
METODOS_PAGO_DICT = dict(METODOS_PAGO)


class Conjunto(Base):
    """El cliente: el conjunto habitacional en su totalidad."""

    __tablename__ = "conjuntos"

    id = Column(Integer, primary_key=True)
    nombre = Column(String(200), nullable=False)
    direccion = Column(String(300), nullable=False, default="")

    admin_nombre = Column(String(200), nullable=False)
    login_email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)

    cuenta_email = Column(String(200), nullable=False)

    correo_recuperacion = Column(String(200), nullable=True)

    reset_token = Column(String(64), nullable=True)
    reset_token_expira = Column(DateTime, nullable=True)

    saldo_inicial = Column(Float, nullable=False, default=0.0)

    fecha_limite_pago = Column(Integer, nullable=False, default=11)

    monto_mensual = Column(Float, nullable=False, default=0.0)
    monto_revision_meses = Column(Integer, nullable=False, default=12)
    monto_confirmado_en = Column(Date, nullable=False, default=hoy)

    fecha_inicio_cobros = Column(Date, nullable=False, default=hoy)
    creado_en = Column(DateTime, nullable=False, default=ahora)
    ultimo_folio = Column(Integer, nullable=False, default=0)

    stripe_customer_id = Column(String(120), nullable=True)

    propiedades = relationship(
        "Propiedad", back_populates="conjunto", cascade="all, delete-orphan"
    )
    pagos = relationship("Pago", back_populates="conjunto", cascade="all, delete-orphan")
    egresos = relationship("Egreso", back_populates="conjunto", cascade="all, delete-orphan")
    proyectos = relationship(
        "Proyecto", back_populates="conjunto", cascade="all, delete-orphan"
    )
    cambios_admin = relationship(
        "CambioAdministrador", back_populates="conjunto", cascade="all, delete-orphan"
    )
    historial_montos = relationship(
        "MontoMensual",
        back_populates="conjunto",
        cascade="all, delete-orphan",
        order_by="MontoMensual.vigente_desde",
    )

    def siguiente_folio(self):
        self.ultimo_folio += 1
        return f"AII-{self.id:04d}-{self.ultimo_folio:05d}"

    def revision_monto_pendiente(self):
        if not self.monto_revision_meses:
            return False
        limite = self.monto_confirmado_en + dt.timedelta(
            days=30 * self.monto_revision_meses
        )
        return hoy() >= limite

    def monto_vigente_en(self, fecha: dt.date) -> float:
        vigente = self.monto_mensual
        for h in self.historial_montos:
            if h.vigente_desde <= fecha:
                vigente = h.monto
            else:
                break
        return vigente

    def cargo_del_mes_es_exigible(self, anio: int, mes: int, al_dia=None) -> bool:
        al_dia = al_dia or hoy()
        if (anio, mes) < (al_dia.year, al_dia.month):
            return True
        if (anio, mes) > (al_dia.year, al_dia.month):
            return False
        return al_dia.day > (self.fecha_limite_pago or 0)

    def reset_token_valido(self, token: str) -> bool:
        """El enlace de «Olvidé mi contraseña» vence a la hora de haberse
        pedido. Un token viejo o de otra solicitud no sirve."""
        if not token or not self.reset_token or not self.reset_token_expira:
            return False
        return token == self.reset_token and ahora() <= self.reset_token_expira


class Propiedad(Base):
    """Una propiedad del conjunto que debe pagar mantenimiento."""

    __tablename__ = "propiedades"

    id = Column(Integer, primary_key=True)
    conjunto_id = Column(Integer, ForeignKey("conjuntos.id"), nullable=False)

    numero = Column(String(20), nullable=False)

    tipo = Column(String(30), nullable=False, default="casa")

    nombre_dueno = Column(String(200), nullable=True)
    celular_dueno = Column(String(40), nullable=True)
    email_dueno = Column(String(200), nullable=True)

    nombre_residente = Column(String(200), nullable=True)
    celular_residente = Column(String(40), nullable=True)
    email_residente = Column(String(200), nullable=True)

    notas = Column(Text, nullable=False, default="N/A")

    saldo_inicial = Column(Float, nullable=False, default=0.0)
    activo = Column(Boolean, nullable=False, default=True)

    conjunto = relationship("Conjunto", back_populates="propiedades")
    pagos = relationship("Pago", back_populates="propiedad", cascade="all, delete-orphan")

    @property
    def etiqueta(self) -> str:
        return (self.numero or "").strip() or "Sin número"

    @property
    def tipo_legible(self) -> str:
        return TIPOS_PROPIEDAD_DICT.get(self.tipo, "Otro")

    @property
    def ficha_completa(self) -> bool:
        return bool(self.nombre_dueno and self.nombre_dueno.strip())


class Proyecto(Base):
    """Cuota extraordinaria ligada a un proyecto específico."""

    __tablename__ = "proyectos"

    id = Column(Integer, primary_key=True)
    conjunto_id = Column(Integer, ForeignKey("conjuntos.id"), nullable=False)
    concepto = Column(String(200), nullable=False)
    descripcion = Column(Text, nullable=True)
    monto_total = Column(Float, nullable=False)
    monto_por_propiedad = Column(Float, nullable=False)
    fecha_alta = Column(Date, nullable=False, default=hoy)
    fecha_limite_pago = Column(Date, nullable=True)

    estado = Column(String(30), nullable=False, default="por_iniciar")
    comentario_estado = Column(Text, nullable=True)

    conjunto = relationship("Conjunto", back_populates="proyectos")
    pagos = relationship("Pago", back_populates="proyecto")

    @property
    def tiene_pagos(self) -> bool:
        return len(self.pagos) > 0

    @property
    def en_curso(self) -> bool:
        return self.estado in ("por_iniciar", "en_recaudacion", "en_proceso")

    @property
    def total_recaudado(self) -> float:
        return round(sum(p.monto for p in self.pagos), 2)


class Pago(Base):
    """Un pago en efectivo recibido y registrado por el administrador."""

    __tablename__ = "pagos"

    id = Column(Integer, primary_key=True)
    conjunto_id = Column(Integer, ForeignKey("conjuntos.id"), nullable=False)
    propiedad_id = Column(Integer, ForeignKey("propiedades.id"), nullable=False)
    proyecto_id = Column(Integer, ForeignKey("proyectos.id"), nullable=True)

    folio = Column(String(30), unique=True, nullable=False)
    fecha_recepcion = Column(Date, nullable=False, default=hoy)
    monto = Column(Float, nullable=False)

    concepto = Column(String(30), nullable=False, default="mantenimiento")

    metodo_pago = Column(String(30), nullable=False, default="efectivo")

    comprobante_path = Column(String(300), nullable=True)
    creado_en = Column(DateTime, nullable=False, default=ahora)

    conjunto = relationship("Conjunto", back_populates="pagos")
    propiedad = relationship("Propiedad", back_populates="pagos")
    proyecto = relationship("Proyecto", back_populates="pagos")

    @property
    def concepto_legible(self) -> str:
        if self.concepto == "proyecto" and self.proyecto:
            return f"Proyecto: {self.proyecto.concepto}"
        return CONCEPTOS_PAGO_DICT.get(self.concepto, "Otros")

    @property
    def metodo_pago_legible(self) -> str:
        return METODOS_PAGO_DICT.get(self.metodo_pago, "Otro")

    @property
    def abona_a_cartera(self) -> bool:
        return self.concepto in CONCEPTOS_QUE_ABONAN


class Egreso(Base):
    """Gasto del conjunto, con comprobante opcional como respaldo."""

    __tablename__ = "egresos"

    id = Column(Integer, primary_key=True)
    conjunto_id = Column(Integer, ForeignKey("conjuntos.id"), nullable=False)
    concepto = Column(String(200), nullable=False)
    monto = Column(Float, nullable=False)
    fecha = Column(Date, nullable=False, default=hoy)
    comprobante_path = Column(String(300), nullable=True)
    creado_en = Column(DateTime, nullable=False, default=ahora)

    conjunto = relationship("Conjunto", back_populates="egresos")


class MontoMensual(Base):
    """Historial de cambios al monto mensual de mantenimiento. Cada cambio
    aplica solo desde `vigente_desde` en adelante — los meses anteriores se
    siguen calculando con el monto que estaba vigente en su momento (ver
    Conjunto.monto_vigente_en)."""

    __tablename__ = "montos_mensuales"

    id = Column(Integer, primary_key=True)
    conjunto_id = Column(Integer, ForeignKey("conjuntos.id"), nullable=False)
    monto = Column(Float, nullable=False)
    vigente_desde = Column(Date, nullable=False, default=hoy)
    creado_en = Column(DateTime, nullable=False, default=ahora)

    conjunto = relationship("Conjunto", back_populates="historial_montos")


class CambioAdministrador(Base):
    """Bitácora de traspasos de liderazgo (Fase 3)."""

    __tablename__ = "cambios_administrador"

    id = Column(Integer, primary_key=True)
    conjunto_id = Column(Integer, ForeignKey("conjuntos.id"), nullable=False)
    admin_anterior = Column(String(200), nullable=False)
    admin_nuevo = Column(String(200), nullable=False)
    fecha = Column(DateTime, nullable=False, default=ahora)
    snapshot_path = Column(String(300), nullable=True)

    conjunto = relationship("Conjunto", back_populates="cambios_admin")
