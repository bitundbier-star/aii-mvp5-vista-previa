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

# Forma en que el conjunto le pagó a un proveedor (egresos). Es distinta de
# METODOS_PAGO (cómo pagó un vecino): aquí sí existe la tarjeta y no el
# depósito en ventanilla.
FORMAS_PAGO_EGRESO = [
    ("transferencia", "Transferencia"),
    ("tarjeta", "Pago con tarjeta"),
    ("efectivo", "Efectivo"),
    ("cheque", "Cheque"),
]
FORMAS_PAGO_EGRESO_DICT = dict(FORMAS_PAGO_EGRESO)


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

    # Monto por pago posterior al día límite. Reemplaza al monto normal para
    # todo mes que ya haya pasado su fecha límite — no se suma, se reemplaza:
    # es "el monto final a cobrar ese mes", como lo pidió Sofía. Al no llevar
    # historial propio (a diferencia de monto_mensual), cambiarlo aquí se
    # aplica de inmediato a toda la cartera pendiente, igual que ya pasa hoy
    # al cambiar fecha_limite_pago.
    aplica_recargo_tardio = Column(Boolean, nullable=False, default=False)
    monto_mensual_tardio = Column(Float, nullable=True)

    # Recordatorios automáticos por correo (inicio de mes, y un día antes de
    # la fecha límite a quien no ha pagado). Activado por default, como el
    # reporte mensual automático; se puede apagar desde Configuración.
    recordatorios_activos = Column(Boolean, nullable=False, default=True)

    monto_mensual = Column(Float, nullable=False, default=0.0)
    monto_revision_meses = Column(Integer, nullable=False, default=12)
    monto_confirmado_en = Column(Date, nullable=False, default=hoy)

    fecha_inicio_cobros = Column(Date, nullable=False, default=hoy)
    creado_en = Column(DateTime, nullable=False, default=ahora)
    ultimo_folio = Column(Integer, nullable=False, default=0)

    stripe_customer_id = Column(String(120), nullable=True)
    stripe_subscription_id = Column(String(120), nullable=True)
    stripe_status = Column(String(30), nullable=True)   # "trialing"|"active"|"past_due"|"canceled"

    # Plan contratado: "basico" | "medio" | "alto"
    plan_nombre = Column(String(20), nullable=False, default="basico")

    # Límite de propiedades según el plan
    LIMITE_PROPIEDADES = {"basico": 30, "medio": 80, "alto": None}

    # Modo de interfaz elegido por el administrador
    # "simplificado" = solo lo esencial | "completo" = todo lo del plan
    modo_interfaz = Column(String(20), nullable=False, default="simplificado")

    # Si ya se le mostró la pantalla de bienvenida con la elección de modo
    bienvenida_vista = Column(Boolean, nullable=False, default=False)

    prueba_hasta = Column(Date, nullable=True)           # fin del periodo de prueba gratuito
    cupon_usado = Column(String(60), nullable=True)      # código de cupón que se aplicó al registrarse

    # Mensajes de recordatorio personalizables (uno por cada momento)
    recordatorio_msg_10d = Column(Text, nullable=True)
    recordatorio_msg_3d  = Column(Text, nullable=True)
    recordatorio_msg_dia = Column(Text, nullable=True)
    recordatorio_msg_vencido = Column(Text, nullable=True)


    # A quién se le mandó el reporte la última vez (JSON con el modo elegido,
    # los correos marcados uno por uno y los correos extra). Solo sirve para
    # dejar preseleccionado lo mismo el mes siguiente.
    reporte_destinatarios = Column(Text, nullable=True)

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

    # Cotizaciones (hasta 3, todas opcionales)
    cot1_path   = Column(String(300), nullable=True)
    cot1_proveedor = Column(String(200), nullable=True)
    cot1_monto  = Column(Float, nullable=True)
    cot2_path   = Column(String(300), nullable=True)
    cot2_proveedor = Column(String(200), nullable=True)
    cot2_monto  = Column(Float, nullable=True)
    cot3_path   = Column(String(300), nullable=True)
    cot3_proveedor = Column(String(200), nullable=True)
    cot3_monto  = Column(Float, nullable=True)

    # Descripción estructurada del proyecto
    descripcion_detalle = Column(Text, nullable=True)   # características y descripción
    compromisos_proveedor = Column(Text, nullable=True)  # a qué se compromete el proveedor

    estado = Column(String(30), nullable=False, default="por_iniciar")
    comentario_estado = Column(Text, nullable=True)

    # Cómo se va a financiar el proyecto
    # "fondo" = 100% del fondo, "mixto" = fondo + cuota extraordinaria,
    # "cuota" = 100% cuota extraordinaria
    financiamiento = Column(String(20), nullable=True)
    financiamiento_pct_fondo = Column(Float, nullable=True)  # % del fondo en modo mixto (0-100)

    # Fecha en que se marcó como terminado
    terminado_en = Column(DateTime, nullable=True)

    # Cancelación
    cancelado = Column(Boolean, nullable=False, default=False)
    cancelado_en = Column(DateTime, nullable=True)
    cancelado_motivo = Column(Text, nullable=True)
    cancelado_sin_recuperar = Column(Float, nullable=True)  # monto que el proveedor se quedó
    cancelado_reparto_perdida = Column(String(30), nullable=True)  # "pagaron" | "todas"
    cancelado_credito_modo = Column(String(30), nullable=True)     # "cubrir_deuda" | "todo_favor"

    conjunto = relationship("Conjunto", back_populates="proyectos")
    pagos = relationship("Pago", back_populates="proyecto")

    @property
    def tiene_pagos(self) -> bool:
        return len(self.pagos) > 0

    @property
    def limite_propiedades(self):
        limites = {"basico": 30, "medio": 80, "alto": None}
        return limites.get(self.plan_nombre or "basico")

    @property
    def puede_agregar_propiedad(self) -> bool:
        limite = self.limite_propiedades
        if limite is None:
            return True
        activas = sum(1 for p in self.propiedades if p.activo)
        return activas < limite

    @property
    def plan_es(self):
        """Devuelve un objeto con booleanos para comparar el plan en plantillas.
        Uso: {{ conjunto.plan_es.medio }} en Jinja2."""
        class _Plan:
            def __init__(self, nombre):
                self.basico = nombre == "basico"
                self.medio  = nombre in ("medio", "alto")
                self.alto   = nombre == "alto"
        return _Plan(self.plan_nombre or "basico")

    @property
    def modo_completo(self) -> bool:
        return self.modo_interfaz == "completo"

    @property
    def en_curso(self) -> bool:
        return self.estado in ("por_iniciar", "en_recaudacion", "en_proceso") and not self.cancelado

    @property
    def total_recaudado(self) -> float:
        return round(sum(p.monto for p in self.pagos if not p.cancelado), 2)


class Pago(Base):
    """Un pago en efectivo recibido y registrado por el administrador."""

    __tablename__ = "pagos"

    id = Column(Integer, primary_key=True)
    conjunto_id = Column(Integer, ForeignKey("conjuntos.id"), nullable=False)
    propiedad_id = Column(Integer, ForeignKey("propiedades.id"), nullable=False)
    proyecto_id = Column(Integer, ForeignKey("proyectos.id"), nullable=True)

    folio = Column(String(30), unique=True, nullable=False)

    # Un mismo depósito puede cubrir varios conceptos (mantenimiento + gas +
    # un proyecto). Cada concepto se guarda como su propia partida (una fila
    # de Pago), para que cada una siga su propia regla —mantenimiento y
    # proyecto bajan la deuda, gas no— sin tocar ningún cálculo. Lo que las
    # une es `recibo`: el folio único del comprobante que recibe el vecino.
    # La primera partida lleva ese mismo folio; las demás, folio-2, folio-3…
    # Los pagos viejos no tienen `recibo` y se tratan como recibo de una sola
    # partida (ver `folio_recibo`).
    recibo = Column(String(30), nullable=True, index=True)
    fecha_recepcion = Column(Date, nullable=False, default=hoy)
    monto = Column(Float, nullable=False)

    concepto = Column(String(30), nullable=False, default="mantenimiento")
    concepto_descripcion = Column(String(200), nullable=True)  # para cuando concepto == "otros"

    metodo_pago = Column(String(30), nullable=False, default="efectivo")

    # Snapshot al momento de registrar el pago, solo para que el comprobante
    # explique el monto — nunca se vuelve a leer para calcular la cartera
    # (esa siempre se recalcula en vivo). Sin esto, un comprobante ya
    # entregado cambiaría de aspecto si el conjunto se pone al corriente
    # después, que es justo lo que un comprobante no debe hacer.
    incluye_recargo_tardio = Column(Boolean, nullable=False, default=False)
    monto_base_snapshot = Column(Float, nullable=True)

    # Cancelación dentro de las primeras 48 horas: si alguien registra un
    # pago por error, se puede deshacer el daño, pero el pago NUNCA se borra
    # — se marca como cancelado y se queda visible en el historial, con la
    # fecha en que se canceló. Un pago cancelado deja de contar para la
    # cartera (la propiedad vuelve a deber ese monto), como si nunca hubiera
    # entrado dinero.
    cancelado = Column(Boolean, nullable=False, default=False)
    cancelado_en = Column(DateTime, nullable=True)

    comprobante_path = Column(String(300), nullable=True)
    creado_en = Column(DateTime, nullable=False, default=ahora)

    conjunto = relationship("Conjunto", back_populates="pagos")
    propiedad = relationship("Propiedad", back_populates="pagos")
    proyecto = relationship("Proyecto", back_populates="pagos")

    # Ventana para poder cancelar un pago. Se cuenta desde que se registró
    # (creado_en), no desde la fecha de recepción que se haya capturado —
    # así alguien no puede "reabrir" un pago viejo solo por haberle puesto
    # una fecha de recepción reciente.
    HORAS_LIMITE_CANCELACION = 48

    @property
    def folio_recibo(self) -> str:
        return self.recibo or self.folio

    @property
    def concepto_legible(self) -> str:
        if self.concepto == "otros" and self.concepto_descripcion:
            return self.concepto_descripcion
        if self.concepto == "proyecto" and self.proyecto:
            return f"Proyecto: {self.proyecto.concepto}"
        return CONCEPTOS_PAGO_DICT.get(self.concepto, "Otros")

    @property
    def metodo_pago_legible(self) -> str:
        return METODOS_PAGO_DICT.get(self.metodo_pago, "Otro")

    @property
    def abona_a_cartera(self) -> bool:
        return self.concepto in CONCEPTOS_QUE_ABONAN

    @property
    def puede_cancelarse(self) -> bool:
        if self.cancelado:
            return False
        limite = self.creado_en + dt.timedelta(hours=self.HORAS_LIMITE_CANCELACION)
        return ahora() <= limite


class Egreso(Base):
    """Gasto del conjunto.

    Respaldo en tres archivos: el recibo o factura (imagen o PDF, obligatorio
    para egresos nuevos), el XML de la factura (opcional) y el comprobante de
    que se pagó (opcional; es lo que ya existía como `comprobante_path`).
    """

    __tablename__ = "egresos"

    id = Column(Integer, primary_key=True)
    conjunto_id = Column(Integer, ForeignKey("conjuntos.id"), nullable=False)
    concepto = Column(String(200), nullable=False)
    monto = Column(Float, nullable=False)
    fecha = Column(Date, nullable=False, default=hoy)
    forma_pago = Column(String(30), nullable=True)
    recibo_path = Column(String(300), nullable=True)
    xml_path = Column(String(300), nullable=True)
    comprobante_path = Column(String(300), nullable=True)
    creado_en = Column(DateTime, nullable=False, default=ahora)

    conjunto = relationship("Conjunto", back_populates="egresos")

    @property
    def forma_pago_legible(self) -> str:
        return FORMAS_PAGO_EGRESO_DICT.get(self.forma_pago or "", "")


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



class Cupon(Base):
    """Cupón de descuento que Sofía puede crear desde el Panel Maestro."""

    __tablename__ = "cupones"

    id = Column(Integer, primary_key=True)
    codigo = Column(String(60), unique=True, nullable=False, index=True)
    descuento_tipo = Column(String(20), nullable=False, default="porcentaje")  # "porcentaje"|"monto"
    descuento_valor = Column(Float, nullable=False)   # % o MXN según tipo
    usos_maximos = Column(Integer, nullable=True)      # None = ilimitado
    usos_actuales = Column(Integer, nullable=False, default=0)
    valido_hasta = Column(Date, nullable=True)         # None = sin vencimiento
    activo = Column(Boolean, nullable=False, default=True)
    creado_en = Column(DateTime, nullable=False, default=ahora)

    @property
    def disponible(self) -> bool:
        from datetime import date
        if not self.activo:
            return False
        if self.usos_maximos and self.usos_actuales >= self.usos_maximos:
            return False
        if self.valido_hasta and date.today() > self.valido_hasta:
            return False
        return True

    @property
    def descuento_legible(self) -> str:
        if self.descuento_tipo == "porcentaje":
            return f"{self.descuento_valor:.0f}%"
        return f"${self.descuento_valor:,.2f} MXN"

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
