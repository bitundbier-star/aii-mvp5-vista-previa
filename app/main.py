import datetime as dt
import os
import re
import shutil
import urllib.parse
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.hash import bcrypt
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from .database import Base, engine, get_db, DATA_DIR
from . import models
from .models import (
    TIPOS_PROPIEDAD,
    TIPOS_PROPIEDAD_DICT,
    CONCEPTOS_PAGO,
    CONCEPTOS_PAGO_DICT,
    METODOS_PAGO,
    METODOS_PAGO_DICT,
)
from .services.cartera import estado_conjunto, estado_propiedad, orden_natural
from .services.comprobante import comprobante_para_adjuntar, comprobante_png
from .services.email import enviar_correo, modo_real_configurado
from .services.exportar import exportar_conjunto
from .services.formato import dinero, estado_saldo
from .services.reportes import (
    datos_reporte,
    hay_reporte_disponible,
    mes_reportable,
    meses_disponibles,
    resumen_actual,
    saldo_acumulado,
)
from .services.imagen_reporte import generar_imagen_reporte, periodo_en_espanol
from .services.pagos_stripe import iniciar_actualizacion_metodo_pago
from .demo import MODO_DEMO, DEMO_EMAIL, DEMO_PASSWORD, DEMO_NOMBRE, sembrar_si_hace_falta

Base.metadata.create_all(bind=engine)

# En modo demostración, si la base amaneció vacía (el disco de Render es
# temporal) se vuelve a crear el conjunto de ejemplo. Así el borrado es
# invisible para quien entra a ver la herramienta.
sembrar_si_hace_falta()

ESTADOS_PROYECTO = [
    ("por_iniciar", "Por iniciar"),
    ("en_recaudacion", "En recaudación de recursos"),
    ("en_proceso", "En proceso"),
    ("cerrado", "Cerrado"),
    ("otro", "Otro"),
]
ESTADOS_PROYECTO_DICT = dict(ESTADOS_PROYECTO)

# Cada cuánto recordarle al administrador que revise el monto. El 0 es la
# opción "yo hago el cambio cuando sea necesario": nunca se le recuerda.
OPCIONES_REVISION = [
    (6, "Cada 6 meses"),
    (12, "Cada año"),
    (24, "Cada 2 años"),
    (0, "Yo hago el cambio cuando sea necesario"),
]

# Cuántos meses de pagos se muestran por defecto en la lista. El historial
# completo nunca se borra ni se esconde: esto es solo la vista inicial, y
# siempre hay un botón para ver todo.
MESES_VISTA_PAGOS = 4


def monto_por_propiedad_calculado(conjunto, monto_total: float) -> float:
    activas = [p for p in conjunto.propiedades if p.activo]
    n = max(len(activas), 1)
    return round(monto_total / n, 2)


def siguiente_numero_propiedad(conjunto) -> str:
    """Un número que no choque con los que ya existen. Como el número ahora es
    texto libre (101, A1, PB-2), se cuentan las propiedades y se busca el
    primer entero libre en vez de asumir que el último es numérico."""
    usados = {(p.numero or "").strip().lower() for p in conjunto.propiedades}
    n = len(conjunto.propiedades) + 1
    while str(n) in usados:
        n += 1
    return str(n)


def _parece_correo(valor: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$", (valor or "").strip()))


def restar_meses(fecha: dt.date, meses: int) -> dt.date:
    """Primer día del mes que está `meses` meses antes que `fecha`."""
    total = fecha.year * 12 + (fecha.month - 1) - meses
    return dt.date(total // 12, total % 12 + 1, 1)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_KEY = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")

app = FastAPI(title="AII - Vecinos que se auto-administran (MVP)")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# El formato del dinero se registra una sola vez, aquí, para que ninguna
# plantilla pueda imprimir un saldo a favor con signo negativo por descuido.
# `dinero` siempre sale en positivo; quien dice de qué se trata es la palabra
# y el color que devuelve `estado_saldo`.
templates.env.filters["dinero"] = dinero
templates.env.globals["estado_saldo"] = estado_saldo
templates.env.globals["modo_demo"] = MODO_DEMO

EGRESOS_DIR = os.path.join(BASE_DIR, "static", "egresos")
os.makedirs(EGRESOS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def conjunto_actual(request: Request, db: Session = Depends(get_db)):
    conjunto_id = request.session.get("conjunto_id")
    if not conjunto_id:
        return None
    return db.query(models.Conjunto).get(conjunto_id)


def requerir_login(request: Request, db: Session = Depends(get_db)):
    conjunto = conjunto_actual(request, db)
    if not conjunto:
        return None
    return conjunto


def tpl(request: Request, nombre: str, **contexto):
    contexto["request"] = request
    contexto.setdefault("conjunto", None)
    return templates.TemplateResponse(nombre, contexto)


def avisar_a_la_cuenta(conjunto, asunto: str, plantilla: str, adjuntos=None, **datos):
    """Manda un correo al correo de la cuenta del conjunto (el que sobrevive
    los cambios de administrador). Si SMTP no está configurado, el correo se
    guarda como vista previa y se dice claramente que no salió — no se
    simula un envío que no ocurrió."""
    cuerpo = templates.get_template(plantilla).render(conjunto=conjunto, **datos)
    return enviar_correo(conjunto.cuenta_email, asunto, cuerpo, adjuntos or [])


# ---------------------------------------------------------------------------
# Home / auth
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    conjunto = conjunto_actual(request, db)
    if conjunto:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/registro", response_class=HTMLResponse)
def registro_form(request: Request):
    return tpl(request, "registro.html", error=None, opciones_revision=OPCIONES_REVISION)


@app.post("/registro", response_class=HTMLResponse)
def registro_submit(
    request: Request,
    nombre_conjunto: str = Form(...),
    direccion: str = Form(""),
    admin_nombre: str = Form(...),
    login_email: str = Form(...),
    password: str = Form(...),
    cuenta_email: str = Form(...),
    monto_mensual: float = Form(...),
    monto_revision_meses: int = Form(12),
    fecha_limite_pago: int = Form(11),
    saldo_inicial: float = Form(0.0),
    num_propiedades: int = Form(...),
    db: Session = Depends(get_db),
):
    existente = db.query(models.Conjunto).filter_by(login_email=login_email).first()
    if existente:
        return tpl(
            request,
            "registro.html",
            error="Ya existe una cuenta con ese correo.",
            opciones_revision=OPCIONES_REVISION,
        )

    conjunto = models.Conjunto(
        nombre=nombre_conjunto,
        direccion=direccion.strip(),
        admin_nombre=admin_nombre,
        login_email=login_email,
        password_hash=bcrypt.hash(password),
        cuenta_email=cuenta_email,
        monto_mensual=monto_mensual,
        monto_revision_meses=monto_revision_meses,
        fecha_limite_pago=min(max(int(fecha_limite_pago or 11), 1), 31),
        saldo_inicial=round(max(saldo_inicial or 0.0, 0.0), 2),
        fecha_inicio_cobros=dt.date.today(),
        monto_confirmado_en=dt.date.today(),
    )
    db.add(conjunto)
    db.flush()  # asigna conjunto.id

    # Las propiedades se generan numeradas 1, 2, 3… El número es solo el punto
    # de partida: durante el alta se puede cambiar por lo que de verdad usan
    # (101, A1, PB-2). Después de guardada la ficha se congela, porque aparece
    # en comprobantes que ya se entregaron.
    for i in range(1, num_propiedades + 1):
        db.add(
            models.Propiedad(
                conjunto_id=conjunto.id,
                numero=str(i),
                tipo="casa",
                notas="N/A",
                saldo_inicial=0.0,
            )
        )

    db.add(
        models.MontoMensual(
            conjunto_id=conjunto.id,
            monto=monto_mensual,
            vigente_desde=conjunto.fecha_inicio_cobros,
        )
    )

    db.commit()

    request.session["conjunto_id"] = conjunto.id
    return RedirectResponse("/propiedades?bienvenida=1", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return tpl(
        request, "login.html", error=None,
        demo_email=DEMO_EMAIL, demo_password=DEMO_PASSWORD, demo_nombre=DEMO_NOMBRE,
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    login_email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    conjunto = db.query(models.Conjunto).filter_by(login_email=login_email).first()
    if not conjunto or not bcrypt.verify(password, conjunto.password_hash):
        return tpl(request, "login.html", error="Correo o contraseña incorrectos.")
    request.session["conjunto_id"] = conjunto.id
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# "Olvidé mi contraseña"
#
# El correo de login puede quedar fuera de alcance —el celular donde se
# administra se pierde, y con él el correo también— así que el enlace de
# recuperación NUNCA se manda ahí. Se manda al correo de la cuenta y, si está
# capturado, también al correo de recuperación (idealmente de otra persona
# responsable de la administración). Cualquiera de los dos alcanza para
# recuperar el acceso.
# ---------------------------------------------------------------------------

MINUTOS_VIGENCIA_RESTABLECIMIENTO = 60


@app.get("/recuperar", response_class=HTMLResponse)
def recuperar_form(request: Request):
    return tpl(request, "recuperar.html", enviado=False, error=None)


@app.post("/recuperar", response_class=HTMLResponse)
def recuperar_submit(
    request: Request,
    login_email: str = Form(...),
    db: Session = Depends(get_db),
):
    conjunto = db.query(models.Conjunto).filter_by(login_email=login_email.strip()).first()
    if conjunto:
        conjunto.reset_token = uuid.uuid4().hex
        conjunto.reset_token_expira = dt.datetime.utcnow() + dt.timedelta(
            minutes=MINUTOS_VIGENCIA_RESTABLECIMIENTO
        )
        db.commit()

        enlace = str(request.base_url).rstrip("/") + f"/restablecer?token={conjunto.reset_token}"
        cuerpo = templates.get_template("correo_recuperacion.html").render(
            conjunto=conjunto, enlace=enlace, minutos=MINUTOS_VIGENCIA_RESTABLECIMIENTO,
        )
        destinos = [conjunto.cuenta_email]
        if conjunto.correo_recuperacion:
            destinos.append(conjunto.correo_recuperacion)
        for destino in destinos:
            enviar_correo(destino, f"Recuperar el acceso a {conjunto.nombre} - AII", cuerpo)

    # El mismo mensaje exista o no la cuenta: así nadie puede usar este
    # formulario para averiguar qué correos de login están registrados.
    return tpl(request, "recuperar.html", enviado=True, error=None)


@app.get("/restablecer", response_class=HTMLResponse)
def restablecer_form(request: Request, db: Session = Depends(get_db)):
    token = request.query_params.get("token", "")
    conjunto = db.query(models.Conjunto).filter_by(reset_token=token).first() if token else None
    if not conjunto or not conjunto.reset_token_valido(token):
        return tpl(
            request, "restablecer.html", token=None,
            error="Este enlace ya no es válido: puede que haya vencido o que ya se haya usado. "
                  "Pide uno nuevo desde «Olvidé mi contraseña».",
        )
    return tpl(request, "restablecer.html", token=token, error=None)


@app.post("/restablecer", response_class=HTMLResponse)
def restablecer_submit(
    request: Request,
    token: str = Form(...),
    password_nueva: str = Form(...),
    db: Session = Depends(get_db),
):
    conjunto = db.query(models.Conjunto).filter_by(reset_token=token).first() if token else None
    if not conjunto or not conjunto.reset_token_valido(token):
        return tpl(
            request, "restablecer.html", token=None,
            error="Este enlace ya no es válido: puede que haya vencido o que ya se haya usado. "
                  "Pide uno nuevo desde «Olvidé mi contraseña».",
        )

    conjunto.password_hash = bcrypt.hash(password_nueva)
    conjunto.reset_token = None
    conjunto.reset_token_expira = None
    db.commit()

    request.session.clear()
    return RedirectResponse("/login?restablecida=1", status_code=302)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    # Inicio muestra el estado de HOY, con el mes en curso todavía abierto.
    # El reporte es otra cosa: un mes ya cerrado. Mezclarlos era lo que hacía
    # que las cifras de las dos pantallas no coincidieran.
    resumen = resumen_actual(conjunto)
    fichas_incompletas = [
        p for p in conjunto.propiedades if p.activo and not p.ficha_completa
    ]
    return tpl(
        request,
        "dashboard.html",
        conjunto=conjunto,
        resumen=resumen,
        hay_reporte=hay_reporte_disponible(conjunto),
        revision_pendiente=conjunto.revision_monto_pendiente(),
        smtp_real=modo_real_configurado(),
        fichas_incompletas=fichas_incompletas,
    )


@app.post("/monto/confirmar")
def confirmar_monto(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    conjunto.monto_confirmado_en = dt.date.today()
    db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@app.post("/monto/actualizar")
def actualizar_monto(
    request: Request,
    nuevo_monto: float = Form(...),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    conjunto.monto_mensual = nuevo_monto
    conjunto.monto_confirmado_en = dt.date.today()
    db.add(
        models.MontoMensual(
            conjunto_id=conjunto.id, monto=nuevo_monto, vigente_desde=dt.date.today()
        )
    )
    db.commit()
    return RedirectResponse("/dashboard", status_code=302)


# ---------------------------------------------------------------------------
# Propiedades
#
# Ya no es una pestaña permanente del menú: aparece durante el alta inicial
# del conjunto y después se consulta y edita desde Configuración.
# ---------------------------------------------------------------------------

@app.get("/propiedades", response_class=HTMLResponse)
def propiedades_lista(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    bienvenida = request.query_params.get("bienvenida") == "1"
    return tpl(
        request,
        "propiedades.html",
        conjunto=conjunto,
        bienvenida=bienvenida,
        # En el alta el número todavía se puede escribir; después se congela.
        # `en_alta` es lo que decide si el campo va abierto o de solo lectura.
        en_alta=True,
        propiedades=sorted(conjunto.propiedades, key=orden_natural),
        tipos=TIPOS_PROPIEDAD,
        error=request.query_params.get("error"),
        guardada=request.query_params.get("guardada"),
    )


def _guardar_ficha_propiedad(propiedad, datos: dict, permitir_numero: bool, conjunto):
    """Escribe la ficha de una propiedad. Devuelve un mensaje de error, o None
    si todo quedó bien.

    Vive aparte porque los mismos campos se capturan en dos lados —el alta y
    Configuración— y la única diferencia entre ambos es si el número se puede
    tocar. Duplicar esto es la forma segura de que las dos pantallas se
    vayan separando con el tiempo.
    """
    if not datos["nombre_dueno"].strip():
        return "El nombre del propietario es obligatorio."

    if permitir_numero:
        numero = datos["numero"].strip()
        if not numero:
            return "El número de la propiedad es obligatorio."
        repetido = any(
            p.id != propiedad.id and (p.numero or "").strip().lower() == numero.lower()
            for p in conjunto.propiedades
        )
        if repetido:
            return f"Ya hay otra propiedad con el número «{numero}»."
        propiedad.numero = numero

    for campo in ("email_dueno", "email_residente"):
        valor = datos[campo].strip()
        if valor and not _parece_correo(valor):
            return f"«{valor}» no parece un correo electrónico."

    propiedad.tipo = datos["tipo"] if datos["tipo"] in TIPOS_PROPIEDAD_DICT else "otro"
    propiedad.nombre_dueno = datos["nombre_dueno"].strip()
    propiedad.celular_dueno = datos["celular_dueno"].strip() or None
    propiedad.email_dueno = datos["email_dueno"].strip() or None
    propiedad.nombre_residente = datos["nombre_residente"].strip() or None
    propiedad.celular_residente = datos["celular_residente"].strip() or None
    propiedad.email_residente = datos["email_residente"].strip() or None
    propiedad.notas = datos["notas"].strip() or "N/A"
    return None


def _saldo_desde_opciones(tipo_saldo: str, monto: float) -> float:
    """Traduce las tres opciones de la pantalla al número con signo que se
    guarda. El administrador elige en palabras —no debe nada / debe esto /
    tiene esto a favor— y el signo lo pone el sistema. Antes se capturaba con
    signo y era fácil equivocarse en la dirección."""
    monto = abs(round(monto or 0.0, 2))
    if tipo_saldo == "debe":
        return monto
    if tipo_saldo == "favor":
        return -monto
    return 0.0


@app.post("/propiedades/{propiedad_id}/actualizar")
def propiedad_actualizar(
    propiedad_id: int,
    request: Request,
    numero: str = Form(""),
    tipo: str = Form("casa"),
    nombre_dueno: str = Form(""),
    celular_dueno: str = Form(""),
    email_dueno: str = Form(""),
    nombre_residente: str = Form(""),
    celular_residente: str = Form(""),
    email_residente: str = Form(""),
    notas: str = Form(""),
    tipo_saldo: str = Form("cero"),
    monto_saldo: float = Form(0.0),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    propiedad = db.query(models.Propiedad).filter_by(id=propiedad_id, conjunto_id=conjunto.id).first()
    if not propiedad:
        return RedirectResponse("/propiedades", status_code=302)

    error = _guardar_ficha_propiedad(
        propiedad,
        {
            "numero": numero, "tipo": tipo,
            "nombre_dueno": nombre_dueno, "celular_dueno": celular_dueno,
            "email_dueno": email_dueno, "nombre_residente": nombre_residente,
            "celular_residente": celular_residente, "email_residente": email_residente,
            "notas": notas,
        },
        permitir_numero=True,   # esta pantalla es la del alta
        conjunto=conjunto,
    )
    if error:
        db.rollback()
        return RedirectResponse(
            f"/propiedades?error={urllib.parse.quote(error)}#propiedad-{propiedad.id}",
            status_code=302,
        )

    propiedad.saldo_inicial = _saldo_desde_opciones(tipo_saldo, monto_saldo)
    db.commit()

    return RedirectResponse(
        f"/propiedades?guardada={propiedad.id}#propiedad-{propiedad.id}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Pagos (ingresos) + comprobante
# ---------------------------------------------------------------------------

@app.get("/pagos", response_class=HTMLResponse)
def pagos_lista(
    request: Request,
    folio: str = "",
    propiedad_id: str = "",
    desde: str = "",
    hasta: str = "",
    concepto: str = "",
    todos: str = "",
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    pagos = sorted(conjunto.pagos, key=lambda p: (p.fecha_recepcion, p.id), reverse=True)

    # Vista por defecto: los últimos 4 meses. No es un corte de acceso — el
    # historial completo sigue ahí y se ve con "Ver todo el historial".
    ver_todo = todos == "1"
    desde_efectivo = desde
    if not ver_todo and not desde:
        desde_efectivo = restar_meses(dt.date.today(), MESES_VISTA_PAGOS - 1).isoformat()

    # Todos los filtros son combinables entre sí: se aplican uno tras otro.
    if folio.strip():
        aguja = folio.strip().lower()
        pagos = [p for p in pagos if aguja in p.folio.lower()]
    if propiedad_id:
        pagos = [p for p in pagos if str(p.propiedad_id) == propiedad_id]
    if concepto:
        pagos = [p for p in pagos if p.concepto == concepto]
    if desde_efectivo:
        limite = dt.date.fromisoformat(desde_efectivo)
        pagos = [p for p in pagos if p.fecha_recepcion >= limite]
    if hasta:
        limite = dt.date.fromisoformat(hasta)
        pagos = [p for p in pagos if p.fecha_recepcion <= limite]

    filtros_activos = bool(folio.strip() or propiedad_id or concepto or desde or hasta)

    return tpl(
        request,
        "pagos.html",
        conjunto=conjunto,
        pagos=pagos,
        propiedades=sorted(conjunto.propiedades, key=orden_natural),
        conceptos=CONCEPTOS_PAGO,
        filtros={
            "folio": folio,
            "propiedad_id": propiedad_id,
            "desde": desde,
            "hasta": hasta,
            "concepto": concepto,
        },
        filtros_activos=filtros_activos,
        ver_todo=ver_todo,
        meses_vista=MESES_VISTA_PAGOS,
        total_pagos=len(conjunto.pagos),
    )


@app.get("/pagos/nuevo", response_class=HTMLResponse)
def pago_nuevo_form(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    return tpl(
        request,
        "pago_nuevo.html",
        conjunto=conjunto,
        propiedades=sorted(conjunto.propiedades, key=orden_natural),
        proyectos=[p for p in conjunto.proyectos if p.en_curso],
        conceptos=CONCEPTOS_PAGO,
        metodos=METODOS_PAGO,
        hoy=dt.date.today().isoformat(),
        error=None,
    )


@app.post("/pagos/nuevo")
def pago_nuevo_submit(
    request: Request,
    propiedad_id: int = Form(...),
    fecha_recepcion: str = Form(...),
    monto: float = Form(...),
    concepto: str = Form(...),
    proyecto_id: str = Form(""),
    metodo_pago: str = Form(...),
    correo_destino: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    propiedad = db.query(models.Propiedad).filter_by(id=propiedad_id, conjunto_id=conjunto.id).first()
    if not propiedad:
        return RedirectResponse("/pagos/nuevo", status_code=302)

    if concepto not in CONCEPTOS_PAGO_DICT:
        concepto = "otros"
    if metodo_pago not in METODOS_PAGO_DICT:
        metodo_pago = "otro"

    # El proyecto solo tiene sentido cuando el concepto es "Proyecto".
    proyecto_ref = None
    if concepto == "proyecto" and proyecto_id:
        proyecto_ref = (
            db.query(models.Proyecto).filter_by(id=int(proyecto_id), conjunto_id=conjunto.id).first()
        )

    folio = conjunto.siguiente_folio()

    pago = models.Pago(
        conjunto_id=conjunto.id,
        propiedad_id=propiedad.id,
        proyecto_id=proyecto_ref.id if proyecto_ref else None,
        folio=folio,
        fecha_recepcion=dt.datetime.strptime(fecha_recepcion, "%Y-%m-%d").date(),
        monto=monto,
        concepto=concepto,
        metodo_pago=metodo_pago,
    )
    db.add(pago)
    db.commit()
    db.refresh(pago)

    # El comprobante se dibuja al vuelo. Solo se escribe un archivo temporal
    # para poder adjuntarlo al correo; no se guarda entre los archivos de la
    # aplicación porque ese disco es temporal y los comprobantes guardados
    # desaparecían en cada despliegue, dejando los enlaces rotos.
    ruta_absoluta = comprobante_para_adjuntar(pago)

    # El comprobante siempre se manda al correo de la cuenta, sin que el
    # administrador tenga que acordarse.
    avisar_a_la_cuenta(
        conjunto,
        f"Comprobante de pago - {conjunto.nombre} - Folio {folio}",
        "correo_comprobante.html",
        adjuntos=[ruta_absoluta],
        pago=pago,
    )

    # Mandárselo también al vecino que pagó es opcional.
    destino = correo_destino.strip()
    if destino:
        cuerpo = templates.get_template("correo_comprobante.html").render(pago=pago, conjunto=conjunto)
        enviar_correo(
            destino,
            f"Comprobante de pago - {conjunto.nombre} - Folio {folio}",
            cuerpo,
            [ruta_absoluta],
        )

    return RedirectResponse(
        f"/comprobantes/{folio}?vecino={'1' if destino else '0'}", status_code=302
    )


@app.get("/comprobantes/{folio}/imagen")
def comprobante_imagen(folio: str, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    """La imagen del comprobante, dibujada en el momento a partir del pago.

    El folio siempre produce la misma imagen, así que no hace falta guardarla
    —y guardarla era peor: el disco es temporal y se perdían.
    """
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    pago = db.query(models.Pago).filter_by(folio=folio, conjunto_id=conjunto.id).first()
    if not pago:
        return RedirectResponse("/pagos", status_code=302)
    return Response(
        content=comprobante_png(pago),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{folio}.png"'},
    )


@app.get("/comprobantes/{folio}", response_class=HTMLResponse)
def ver_comprobante(folio: str, request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    pago = db.query(models.Pago).filter_by(folio=folio, conjunto_id=conjunto.id).first()
    if not pago:
        return RedirectResponse("/pagos", status_code=302)
    return tpl(
        request,
        "comprobante.html",
        conjunto=conjunto,
        pago=pago,
        smtp_real=modo_real_configurado(),
        enviado_al_vecino=request.query_params.get("vecino") == "1",
    )


# ---------------------------------------------------------------------------
# Proyectos (cuotas extraordinarias)
# ---------------------------------------------------------------------------

@app.get("/proyectos", response_class=HTMLResponse)
def proyectos_lista(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    return tpl(
        request,
        "proyectos.html",
        conjunto=conjunto,
        proyectos=sorted(conjunto.proyectos, key=lambda p: (p.fecha_alta, p.id), reverse=True),
        estados=ESTADOS_PROYECTO,
        estados_dict=ESTADOS_PROYECTO_DICT,
        error=request.query_params.get("error"),
    )


@app.post("/proyectos/nuevo")
def proyecto_nuevo(
    request: Request,
    concepto: str = Form(...),
    descripcion: str = Form(""),
    monto_total: float = Form(...),
    fecha_limite_pago: str = Form(""),
    estado: str = Form("por_iniciar"),
    comentario_estado: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    db.add(
        models.Proyecto(
            conjunto_id=conjunto.id,
            concepto=concepto,
            descripcion=descripcion or None,
            monto_total=monto_total,
            monto_por_propiedad=monto_por_propiedad_calculado(conjunto, monto_total),
            fecha_limite_pago=dt.datetime.strptime(fecha_limite_pago, "%Y-%m-%d").date()
            if fecha_limite_pago
            else None,
            estado=estado,
            comentario_estado=comentario_estado or None,
        )
    )
    db.commit()
    return RedirectResponse("/proyectos", status_code=302)


@app.get("/proyectos/{proyecto_id}/editar", response_class=HTMLResponse)
def proyecto_editar_form(
    proyecto_id: int, request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if not proyecto:
        return RedirectResponse("/proyectos", status_code=302)
    return tpl(request, "proyecto_editar.html", conjunto=conjunto, proyecto=proyecto, estados=ESTADOS_PROYECTO)


@app.post("/proyectos/{proyecto_id}/editar")
def proyecto_editar_submit(
    proyecto_id: int,
    request: Request,
    concepto: str = Form(...),
    descripcion: str = Form(""),
    monto_total: float = Form(...),
    fecha_limite_pago: str = Form(""),
    estado: str = Form("por_iniciar"),
    comentario_estado: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if proyecto:
        proyecto.concepto = concepto
        proyecto.descripcion = descripcion or None
        proyecto.monto_total = monto_total
        proyecto.monto_por_propiedad = monto_por_propiedad_calculado(conjunto, monto_total)
        proyecto.fecha_limite_pago = (
            dt.datetime.strptime(fecha_limite_pago, "%Y-%m-%d").date() if fecha_limite_pago else None
        )
        proyecto.estado = estado
        proyecto.comentario_estado = comentario_estado or None
        db.commit()
    return RedirectResponse("/proyectos", status_code=302)


@app.post("/proyectos/{proyecto_id}/eliminar")
def proyecto_eliminar(
    proyecto_id: int,
    request: Request,
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if not proyecto:
        return RedirectResponse("/proyectos", status_code=302)

    # Un proyecto con pagos encima no se borra: los comprobantes que ya se le
    # entregaron a los vecinos quedarían huérfanos. En ese caso se marca como
    # "Cerrado" en vez de eliminarlo.
    if proyecto.tiene_pagos:
        return RedirectResponse(f"/proyectos?error={proyecto.id}", status_code=302)

    db.delete(proyecto)
    db.commit()
    return RedirectResponse("/proyectos", status_code=302)


# ---------------------------------------------------------------------------
# Egresos
# ---------------------------------------------------------------------------

@app.get("/egresos", response_class=HTMLResponse)
def egresos_lista(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    egresos = sorted(conjunto.egresos, key=lambda e: (e.fecha, e.id), reverse=True)
    hoy = dt.date.today()
    inicio_mes = hoy.replace(day=1)
    return tpl(
        request,
        "egresos.html",
        conjunto=conjunto,
        egresos=egresos,
        hoy=hoy.isoformat(),
        # El número grande de esta pantalla es lo que ha salido este mes, que
        # es lo que se está viendo aquí. El saldo acumulado del conjunto vive
        # en Inicio y en el reporte.
        egresos_del_mes=round(
            sum(e.monto for e in conjunto.egresos if e.fecha >= inicio_mes), 2
        ),
        saldo_acumulado=saldo_acumulado(conjunto, hoy),
        editar_id=request.query_params.get("editar"),
    )


@app.post("/egresos/nuevo")
async def egreso_nuevo(
    request: Request,
    concepto: str = Form(...),
    monto: float = Form(...),
    fecha: str = Form(...),
    comprobante: UploadFile | None = File(None),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    ruta_relativa = None
    if comprobante is not None and comprobante.filename:
        ext = os.path.splitext(comprobante.filename)[1]
        nombre_archivo = f"{conjunto.id}_{uuid.uuid4().hex}{ext}"
        ruta_absoluta = os.path.join(EGRESOS_DIR, nombre_archivo)
        with open(ruta_absoluta, "wb") as f:
            shutil.copyfileobj(comprobante.file, f)
        ruta_relativa = f"egresos/{nombre_archivo}"

    egreso = models.Egreso(
        conjunto_id=conjunto.id,
        concepto=concepto,
        monto=monto,
        fecha=dt.datetime.strptime(fecha, "%Y-%m-%d").date(),
        comprobante_path=ruta_relativa,
    )
    db.add(egreso)
    db.commit()
    db.refresh(egreso)

    # Cada egreso se avisa automáticamente al correo de la cuenta: es el
    # control de que nadie saca dinero sin que quede registro.
    avisar_a_la_cuenta(
        conjunto,
        f"Egreso registrado - {conjunto.nombre} - {egreso.concepto}",
        "correo_egreso.html",
        egreso=egreso,
        accion="registrado",
    )

    return RedirectResponse("/egresos", status_code=302)


@app.post("/egresos/{egreso_id}/editar")
async def egreso_editar(
    egreso_id: int,
    request: Request,
    concepto: str = Form(...),
    monto: float = Form(...),
    fecha: str = Form(...),
    comprobante: UploadFile | None = File(None),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    egreso = db.query(models.Egreso).filter_by(id=egreso_id, conjunto_id=conjunto.id).first()
    if not egreso:
        return RedirectResponse("/egresos", status_code=302)

    egreso.concepto = concepto
    egreso.monto = monto
    egreso.fecha = dt.datetime.strptime(fecha, "%Y-%m-%d").date()

    if comprobante is not None and comprobante.filename:
        ext = os.path.splitext(comprobante.filename)[1]
        nombre_archivo = f"{conjunto.id}_{uuid.uuid4().hex}{ext}"
        with open(os.path.join(EGRESOS_DIR, nombre_archivo), "wb") as f:
            shutil.copyfileobj(comprobante.file, f)
        egreso.comprobante_path = f"egresos/{nombre_archivo}"

    db.commit()
    db.refresh(egreso)

    avisar_a_la_cuenta(
        conjunto,
        f"Egreso modificado - {conjunto.nombre} - {egreso.concepto}",
        "correo_egreso.html",
        egreso=egreso,
        accion="modificado",
    )

    return RedirectResponse("/egresos", status_code=302)


@app.post("/egresos/{egreso_id}/eliminar")
def egreso_eliminar(
    egreso_id: int,
    request: Request,
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    egreso = db.query(models.Egreso).filter_by(id=egreso_id, conjunto_id=conjunto.id).first()
    if not egreso:
        return RedirectResponse("/egresos", status_code=302)

    # La caja chica se calcula en vivo (ingresos acumulados − egresos
    # acumulados), no es un total guardado aparte. Al cancelar el egreso el
    # dinero disponible sube solo: no hace falta lógica extra.
    datos = {"concepto": egreso.concepto, "monto": egreso.monto, "fecha": egreso.fecha}
    db.delete(egreso)
    db.commit()

    avisar_a_la_cuenta(
        conjunto,
        f"Egreso cancelado - {conjunto.nombre} - {datos['concepto']}",
        "correo_egreso.html",
        egreso=datos,
        accion="cancelado",
    )

    return RedirectResponse("/egresos", status_code=302)


# ---------------------------------------------------------------------------
# Cartera
# ---------------------------------------------------------------------------

@app.get("/cartera", response_class=HTMLResponse)
def cartera(
    request: Request,
    propiedad_id: str = "",
    estatus: str = "",
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    estado = estado_conjunto(conjunto)

    # Los dos filtros son combinables: se puede pedir una propiedad concreta
    # *y* que además tenga adeudo.
    if propiedad_id:
        estado = [e for e in estado if str(e["propiedad"].id) == propiedad_id]
    if estatus == "con_adeudo":
        estado = [e for e in estado if not e["al_corriente"]]
    elif estatus == "al_corriente":
        estado = [e for e in estado if e["al_corriente"]]

    return tpl(
        request,
        "cartera.html",
        conjunto=conjunto,
        estado=estado,
        propiedades=sorted(conjunto.propiedades, key=orden_natural),
        filtros={"propiedad_id": propiedad_id, "estatus": estatus},
        filtros_activos=bool(propiedad_id or estatus),
        total_propiedades=len([p for p in conjunto.propiedades if p.activo]),
    )


# ---------------------------------------------------------------------------
# Reporte mensual
# ---------------------------------------------------------------------------

def _mensaje_whatsapp(conjunto, reporte) -> str:
    """Texto prellenado para compartir. Nunca se envía solo: solo abre
    WhatsApp con el mensaje escrito para que el administrador elija a quién
    mandárselo y adjunte la imagen."""
    lineas = [
        f"*{conjunto.nombre}* — Reporte de {reporte['periodo_largo']}",
        "",
        f"Saldo al cierre del mes: {dinero(reporte['saldo_cierre'])}",
        f"Saldo al inicio del mes: {dinero(reporte['saldo_apertura'])}",
        f"Ingresos del mes: {dinero(reporte['ingresos_reales'])}",
        f"Egresos del mes: {dinero(reporte['total_egresos'])}",
        f"Por cobrar: {dinero(reporte['cartera_total'])}",
        f"Al corriente: {reporte['propiedades_al_corriente']} de "
        f"{reporte['propiedades_al_corriente'] + reporte['propiedades_con_adeudo']} propiedades",
    ]
    if reporte["proyectos_en_curso"]:
        lineas.append("")
        lineas.append("*Proyectos en curso:*")
        for p in reporte["proyectos_en_curso"]:
            lineas.append(f"• {p['nombre']}: {dinero(p['recaudado'])} de {dinero(p['monto_total'])}")
    return "\n".join(lineas)


def _mes_pedido(request: Request, conjunto):
    """Qué mes se está pidiendo, validado contra los que de verdad existen.

    Los meses cerrados se pueden consultar todos, siempre. Lo que no se puede
    es pedir un mes que todavía no termina: sus cifras aún se mueven. Si piden
    uno así, se cae al más reciente que sí esté cerrado.
    """
    disponibles = meses_disponibles(conjunto)
    if not disponibles:
        return None, []
    try:
        anio = int(request.query_params.get("anio", ""))
        mes = int(request.query_params.get("mes", ""))
    except ValueError:
        return (disponibles[0]["anio"], disponibles[0]["mes"]), disponibles

    if any(d["anio"] == anio and d["mes"] == mes for d in disponibles):
        return (anio, mes), disponibles
    return (disponibles[0]["anio"], disponibles[0]["mes"]), disponibles


@app.get("/reporte", response_class=HTMLResponse)
def reporte_ver(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    pedido, disponibles = _mes_pedido(request, conjunto)
    if pedido is None:
        # La cuenta se abrió este mismo mes: todavía no hay ningún mes cerrado
        # que reportar. Se dice así, en vez de enseñar un reporte en ceros que
        # parecería un error.
        proximo = dt.date.today().replace(day=1)
        return tpl(
            request,
            "reporte.html",
            conjunto=conjunto,
            reporte=None,
            sin_meses=True,
            proximo_mes=proximo,
            smtp_real=modo_real_configurado(),
            resultado=None,
            imagen=None,
            whatsapp="",
        )

    reporte = datos_reporte(conjunto, pedido[0], pedido[1])
    return tpl(
        request,
        "reporte.html",
        conjunto=conjunto,
        reporte=reporte,
        sin_meses=False,
        smtp_real=modo_real_configurado(),
        resultado=None,
        imagen=request.query_params.get("imagen"),
        whatsapp=urllib.parse.quote(_mensaje_whatsapp(conjunto, reporte)),
    )


@app.get("/reporte/imprimir", response_class=HTMLResponse)
def reporte_imprimir(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    """Versión limpia del reporte, sin menús ni botones, que abre el diálogo
    de impresión del navegador. Desde ahí se guarda como PDF. Se hizo así a
    propósito: no agrega ninguna librería ni costo de despliegue."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    pedido, _ = _mes_pedido(request, conjunto)
    if pedido is None:
        return RedirectResponse("/reporte", status_code=302)
    reporte = datos_reporte(conjunto, pedido[0], pedido[1])
    return tpl(request, "reporte_imprimir.html", conjunto=conjunto, reporte=reporte)


@app.post("/reporte/imagen")
def reporte_imagen(
    request: Request,
    anio: int = Form(None),
    mes: int = Form(None),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Genera el reporte como imagen PNG para poder compartirla."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    disponibles = meses_disponibles(conjunto)
    if not disponibles:
        return RedirectResponse("/reporte", status_code=302)
    if not any(d["anio"] == anio and d["mes"] == mes for d in disponibles):
        anio, mes = disponibles[0]["anio"], disponibles[0]["mes"]
    reporte = datos_reporte(conjunto, anio, mes)
    ruta = generar_imagen_reporte(conjunto, reporte)
    return RedirectResponse(
        f"/reporte?anio={anio}&mes={mes}&imagen={urllib.parse.quote(ruta)}", status_code=302
    )


@app.post("/reporte/enviar", response_class=HTMLResponse)
def reporte_enviar(
    request: Request,
    destinatarios: str = Form(...),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    pedido, _ = _mes_pedido(request, conjunto)
    if pedido is None:
        return RedirectResponse("/reporte", status_code=302)
    reporte = datos_reporte(conjunto, pedido[0], pedido[1])
    cuerpo = templates.get_template("correo_reporte.html").render(conjunto=conjunto, reporte=reporte)
    asunto = f"Reporte de {reporte['periodo']} - {conjunto.nombre}"

    resultados = []
    for destino in [d.strip() for d in destinatarios.split(",") if d.strip()]:
        resultados.append((destino, enviar_correo(destino, asunto, cuerpo)))

    return tpl(
        request,
        "reporte.html",
        conjunto=conjunto,
        reporte=reporte,
        sin_meses=False,
        smtp_real=modo_real_configurado(),
        resultado=resultados,
        imagen=None,
        whatsapp=urllib.parse.quote(_mensaje_whatsapp(conjunto, reporte)),
    )


def enviar_reporte_automatico_mensual():
    """Tarea programada: envía el reporte acumulado al correo de la cuenta
    de cada conjunto. Se ejecuta el día 1 de cada mes (ver scheduler abajo).
    Requiere que el proceso siga corriendo (por eso conviene un hosting
    con proceso persistente, no solo funciones bajo demanda)."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        for conjunto in db.query(models.Conjunto).all():
            # Corre el día 1: el mes que acaba de cerrar es justo el que toca.
            if not hay_reporte_disponible(conjunto):
                continue
            anio, mes = mes_reportable()
            reporte = datos_reporte(conjunto, anio, mes)
            cuerpo = templates.get_template("correo_reporte.html").render(conjunto=conjunto, reporte=reporte)
            enviar_correo(
                conjunto.cuenta_email,
                f"Reporte de {reporte['periodo']} - {conjunto.nombre}",
                cuerpo,
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Configuración: monto de mantenimiento (con vigencia), recordatorio de
# revisión, contraseña, método de pago (Stripe), propiedades y traspaso.
# ---------------------------------------------------------------------------

def _contexto_configuracion(request, conjunto, **extra):
    base = dict(
        conjunto=conjunto,
        hoy=dt.date.today().isoformat(),
        error_password=None,
        exito_password=False,
        error_borrado=None,
        error_recuperacion=None,
        exito_recuperacion=False,
        mensaje_pago=None,
        opciones_revision=OPCIONES_REVISION,
        propiedades=sorted(conjunto.propiedades, key=orden_natural),
        tipos=TIPOS_PROPIEDAD,
        fichas_incompletas=[p for p in conjunto.propiedades if p.activo and not p.ficha_completa],
        error=request.query_params.get("error"),
        guardada=request.query_params.get("guardada"),
        borrada=request.query_params.get("borrada"),
    )
    base.update(extra)
    return tpl(request, "configuracion.html", **base)


@app.get("/configuracion", response_class=HTMLResponse)
def configuracion_ver(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    return _contexto_configuracion(request, conjunto)


@app.post("/configuracion/monto")
def configuracion_monto(
    request: Request,
    nuevo_monto: float = Form(...),
    vigente_desde: str = Form(...),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    fecha_vigencia = dt.datetime.strptime(vigente_desde, "%Y-%m-%d").date()
    db.add(models.MontoMensual(conjunto_id=conjunto.id, monto=nuevo_monto, vigente_desde=fecha_vigencia))
    # El monto "actual" mostrado en el dashboard es el vigente hoy; puede no
    # ser este cambio si su vigencia es en el futuro.
    conjunto.monto_mensual = conjunto.monto_vigente_en(dt.date.today())
    if fecha_vigencia <= dt.date.today():
        conjunto.monto_mensual = nuevo_monto
        conjunto.monto_confirmado_en = dt.date.today()
    db.commit()
    return RedirectResponse("/configuracion", status_code=302)


@app.post("/configuracion/revision")
def configuracion_revision(
    request: Request,
    monto_revision_meses: int = Form(...),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """La pregunta de cada cuánto revisar el monto solo aparece en el alta
    inicial, pero la respuesta se puede cambiar aquí cuando quieran —
    incluida la vuelta atrás: si eligieron 'yo lo cambio cuando sea
    necesario' y se arrepienten, pueden volver a pedir el recordatorio."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    conjunto.monto_revision_meses = monto_revision_meses
    conjunto.monto_confirmado_en = dt.date.today()
    db.commit()
    return RedirectResponse("/configuracion", status_code=302)


@app.post("/configuracion/fecha-limite")
def configuracion_fecha_limite(
    request: Request,
    fecha_limite_pago: int = Form(...),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """El día del mes en que vence el mantenimiento. Cambia el momento a partir
    del cual el mes en curso cuenta como adeudo, así que la cartera se mueve en
    cuanto se guarda."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    conjunto.fecha_limite_pago = min(max(int(fecha_limite_pago), 1), 31)
    db.commit()
    return RedirectResponse("/configuracion", status_code=302)


@app.post("/configuracion/direccion")
def configuracion_direccion(
    request: Request,
    direccion: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    conjunto.direccion = direccion.strip()
    db.commit()
    return RedirectResponse("/configuracion", status_code=302)


@app.post("/configuracion/recuperacion")
def configuracion_recuperacion(
    request: Request,
    correo_recuperacion: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """El correo de recuperación es aparte del correo de la cuenta: idealmente
    es de otra persona responsable de la administración, no de quien está
    administrando hoy. Sirve para el «Olvidé mi contraseña» — si el
    administrador pierde el celular donde tiene tanto la app como su correo,
    esta es la puerta de salida."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    correo_recuperacion = correo_recuperacion.strip()
    if correo_recuperacion and not _parece_correo(correo_recuperacion):
        return _contexto_configuracion(
            request, conjunto,
            error_recuperacion=f"«{correo_recuperacion}» no parece un correo electrónico.",
        )
    conjunto.correo_recuperacion = correo_recuperacion or None
    db.commit()
    return _contexto_configuracion(request, conjunto, exito_recuperacion=True)


@app.post("/configuracion/propiedades/{propiedad_id}/editar")
def configuracion_propiedad_editar(
    propiedad_id: int,
    request: Request,
    tipo: str = Form("casa"),
    nombre_dueno: str = Form(""),
    celular_dueno: str = Form(""),
    email_dueno: str = Form(""),
    nombre_residente: str = Form(""),
    celular_residente: str = Form(""),
    email_residente: str = Form(""),
    notas: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Edición de una propiedad ya dada de alta.

    Aquí el número **no** se toca: ya viaja en comprobantes entregados a los
    vecinos y en reportes que quizá ya circularon. El saldo inicial tampoco:
    se fijó al alta y de ahí en adelante lo que mueve el saldo son los pagos.
    """
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    propiedad = db.query(models.Propiedad).filter_by(
        id=propiedad_id, conjunto_id=conjunto.id
    ).first()
    if not propiedad:
        return RedirectResponse("/configuracion", status_code=302)

    error = _guardar_ficha_propiedad(
        propiedad,
        {
            "numero": propiedad.numero, "tipo": tipo,
            "nombre_dueno": nombre_dueno, "celular_dueno": celular_dueno,
            "email_dueno": email_dueno, "nombre_residente": nombre_residente,
            "celular_residente": celular_residente, "email_residente": email_residente,
            "notas": notas,
        },
        permitir_numero=False,
        conjunto=conjunto,
    )
    if error:
        db.rollback()
        return RedirectResponse(
            f"/configuracion?error={urllib.parse.quote(error)}#propiedad-{propiedad.id}",
            status_code=302,
        )

    db.commit()
    return RedirectResponse(
        f"/configuracion?guardada={propiedad.id}#propiedad-{propiedad.id}", status_code=302
    )


@app.get("/configuracion/descargar")
def configuracion_descargar(conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    """Todo el historial del conjunto en un ZIP: los datos en CSV para Excel y
    un archivo legible para imprimir o mandar a la asamblea.

    No está aquí solo para el borrado. Que el conjunto pueda llevarse lo suyo
    cuando quiera es lo que hace que confiar en la plataforma no sea una
    apuesta: si mañana deciden irse, se van con todo.
    """
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    contenido, nombre = exportar_conjunto(conjunto)
    return Response(
        content=contenido,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.post("/configuracion/borrar-cuenta", response_class=HTMLResponse)
def configuracion_borrar_cuenta(
    request: Request,
    confirmacion: str = Form(""),
    password: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Borra la cuenta del conjunto y todo lo que cuelga de ella.

    Tres frenos, a propósito: la contraseña, escribir el nombre del conjunto, y
    la descarga ofrecida antes. No es exceso de trámite — quien aprieta este
    botón no está borrando lo suyo, está borrando las cuentas de todos sus
    vecinos y los comprobantes que ya les entregó. El aviso al correo de la
    cuenta deja constancia de que ocurrió y de quién lo hizo.
    """
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    # En la versión de demostración esto queda bloqueado: un visitante curioso
    # borraría el conjunto de ejemplo para todos los demás.
    if MODO_DEMO:
        return _contexto_configuracion(
            request, conjunto,
            error_borrado="En la versión de demostración no se puede borrar la cuenta: "
                          "se la llevaría también a las demás personas que están viendo el ejemplo. "
                          "En una cuenta real el botón funciona.",
        )

    if not bcrypt.verify(password, conjunto.password_hash):
        return _contexto_configuracion(
            request, conjunto, error_borrado="La contraseña no es correcta."
        )

    if confirmacion.strip().lower() != (conjunto.nombre or "").strip().lower():
        return _contexto_configuracion(
            request, conjunto,
            error_borrado=f"Para confirmar, escribe exactamente el nombre del conjunto: «{conjunto.nombre}».",
        )

    nombre = conjunto.nombre
    admin = conjunto.admin_nombre
    cuenta_email = conjunto.cuenta_email
    resumen = {
        "propiedades": len(conjunto.propiedades),
        "pagos": len(conjunto.pagos),
        "egresos": len(conjunto.egresos),
        "proyectos": len(conjunto.proyectos),
    }

    # El aviso sale ANTES de borrar: después ya no habría a quién avisarle.
    avisar_a_la_cuenta(
        conjunto,
        f"Se borró la cuenta de {nombre}",
        "correo_cuenta_borrada.html",
        resumen=resumen,
        borrado_por=admin,
        fecha=dt.date.today(),
    )

    db.delete(conjunto)  # las propiedades, pagos, egresos y proyectos van detrás
    db.commit()
    request.session.clear()

    return tpl(
        request, "cuenta_borrada.html",
        # Ojo: `tpl()` ya usa `nombre` para el nombre de la plantilla, así que
        # la variable del conjunto va con otro nombre o chocan.
        nombre_conjunto=nombre, cuenta_email=cuenta_email, resumen=resumen,
    )


@app.post("/configuracion/password", response_class=HTMLResponse)
def configuracion_password(
    request: Request,
    password_actual: str = Form(...),
    password_nueva: str = Form(...),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    if not bcrypt.verify(password_actual, conjunto.password_hash):
        return _contexto_configuracion(
            request, conjunto, error_password="La contraseña actual no es correcta."
        )
    conjunto.password_hash = bcrypt.hash(password_nueva)
    db.commit()
    return _contexto_configuracion(request, conjunto, exito_password=True)


@app.post("/configuracion/metodo-pago", response_class=HTMLResponse)
def configuracion_metodo_pago(
    request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    resultado = iniciar_actualizacion_metodo_pago(conjunto)
    if resultado["listo"] and resultado["url"]:
        return RedirectResponse(resultado["url"], status_code=302)
    return _contexto_configuracion(request, conjunto, mensaje_pago=resultado["detalle"])


@app.post("/configuracion/propiedades/nueva")
def configuracion_propiedad_nueva(
    request: Request,
    numero: str = Form(""),
    tipo: str = Form("casa"),
    nombre_dueno: str = Form(""),
    celular_dueno: str = Form(""),
    email_dueno: str = Form(""),
    nombre_residente: str = Form(""),
    celular_residente: str = Form(""),
    email_residente: str = Form(""),
    notas: str = Form(""),
    tipo_saldo: str = Form("cero"),
    monto_saldo: float = Form(0.0),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Alta de una propiedad después del alta del conjunto.

    Aquí el número sí se escribe —si no, no habría forma de ponerle B-4 a la
    bodega nueva—; queda congelado en cuanto se guarda, igual que el de las
    demás. Lleva los mismos campos que la ficha completa de una propiedad —
    antes solo pedía número, tipo, propietario y saldo, y el resto había que
    ir a capturarlo aparte. Y el saldo inicial se elige con las mismas tres
    opciones del alta.
    """
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    numero = (numero or "").strip() or siguiente_numero_propiedad(conjunto)
    repetido = any(
        (p.numero or "").strip().lower() == numero.lower() for p in conjunto.propiedades
    )
    if repetido:
        return RedirectResponse(
            "/configuracion?error="
            + urllib.parse.quote(f"Ya hay una propiedad con el número «{numero}»."),
            status_code=302,
        )

    for valor in (email_dueno, email_residente):
        valor = (valor or "").strip()
        if valor and not _parece_correo(valor):
            return RedirectResponse(
                "/configuracion?error="
                + urllib.parse.quote(f"«{valor}» no parece un correo electrónico."),
                status_code=302,
            )

    propiedad = models.Propiedad(
        conjunto_id=conjunto.id,
        numero=numero,
        tipo=tipo if tipo in TIPOS_PROPIEDAD_DICT else "otro",
        nombre_dueno=nombre_dueno.strip() or None,
        celular_dueno=celular_dueno.strip() or None,
        email_dueno=email_dueno.strip() or None,
        nombre_residente=nombre_residente.strip() or None,
        celular_residente=celular_residente.strip() or None,
        email_residente=email_residente.strip() or None,
        notas=notas.strip() or "N/A",
        saldo_inicial=_saldo_desde_opciones(tipo_saldo, monto_saldo),
    )
    db.add(propiedad)
    db.commit()
    return RedirectResponse(
        f"/configuracion?guardada={propiedad.id}#propiedad-{propiedad.id}", status_code=302
    )


@app.post("/configuracion/propiedades/{propiedad_id}/borrar")
def configuracion_propiedad_borrar(
    propiedad_id: int,
    request: Request,
    confirmacion: str = Form(""),
    password: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Borra una propiedad, aunque ya tenga pagos o cargos de proyecto encima.

    Dos frenos, como en el borrado de la cuenta: la contraseña y escribir el
    número exacto de la propiedad. Sus pagos se van con ella —la relación
    tiene cascade="all, delete-orphan"— así que el saldo acumulado del
    conjunto se ajusta solo: se calcula en vivo sumando los pagos que existen,
    y los que se acaban de borrar dejan de contar.
    """
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    propiedad = db.query(models.Propiedad).filter_by(
        id=propiedad_id, conjunto_id=conjunto.id
    ).first()
    if not propiedad:
        return RedirectResponse("/configuracion", status_code=302)

    if not bcrypt.verify(password, conjunto.password_hash):
        return RedirectResponse(
            "/configuracion?error="
            + urllib.parse.quote("La contraseña no es correcta.")
            + f"#propiedad-{propiedad.id}",
            status_code=302,
        )

    if confirmacion.strip().lower() != (propiedad.numero or "").strip().lower():
        return RedirectResponse(
            "/configuracion?error="
            + urllib.parse.quote(
                f"Para confirmar, escribe exactamente el número de la propiedad: «{propiedad.numero}»."
            )
            + f"#propiedad-{propiedad.id}",
            status_code=302,
        )

    numero = propiedad.numero
    db.delete(propiedad)
    db.commit()
    return RedirectResponse(
        f"/configuracion?borrada={urllib.parse.quote(numero)}", status_code=302
    )


# ---------------------------------------------------------------------------
# Fase 3: traspaso de administrador
# ---------------------------------------------------------------------------

@app.get("/administrador", response_class=HTMLResponse)
def administrador_form(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    historial = sorted(conjunto.cambios_admin, key=lambda c: c.fecha, reverse=True)
    return tpl(request, "administrador.html", conjunto=conjunto, historial=historial, error=None)


@app.post("/administrador/actualizar", response_class=HTMLResponse)
def administrador_actualizar(
    request: Request,
    password_actual: str = Form(...),
    admin_nombre_nuevo: str = Form(...),
    login_email_nuevo: str = Form(...),
    password_nueva: str = Form(...),
    correo_recuperacion_nuevo: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    if not bcrypt.verify(password_actual, conjunto.password_hash):
        historial = sorted(conjunto.cambios_admin, key=lambda c: c.fecha, reverse=True)
        return tpl(
            request,
            "administrador.html",
            conjunto=conjunto,
            historial=historial,
            error="La contraseña actual no es correcta.",
        )

    correo_recuperacion_nuevo = correo_recuperacion_nuevo.strip()
    if correo_recuperacion_nuevo and not _parece_correo(correo_recuperacion_nuevo):
        historial = sorted(conjunto.cambios_admin, key=lambda c: c.fecha, reverse=True)
        return tpl(
            request,
            "administrador.html",
            conjunto=conjunto,
            historial=historial,
            error=f"«{correo_recuperacion_nuevo}» no parece un correo electrónico.",
        )

    admin_anterior = conjunto.admin_nombre

    # Instantánea del estado al momento del cambio, reutilizando el motor
    # de reportes ya construido en Fase 2.
    reporte = datos_reporte(conjunto)
    html_snapshot = templates.get_template("correo_reporte.html").render(conjunto=conjunto, reporte=reporte)
    marca = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_snapshot = f"traspaso_{conjunto.id}_{marca}.html"
    ruta_snapshot = os.path.join(DATA_DIR, "traspasos", nombre_snapshot)
    os.makedirs(os.path.dirname(ruta_snapshot), exist_ok=True)
    with open(ruta_snapshot, "w", encoding="utf-8") as f:
        f.write(html_snapshot)

    db.add(
        models.CambioAdministrador(
            conjunto_id=conjunto.id,
            admin_anterior=admin_anterior,
            admin_nuevo=admin_nombre_nuevo,
            snapshot_path=ruta_snapshot,
        )
    )

    conjunto.admin_nombre = admin_nombre_nuevo
    conjunto.login_email = login_email_nuevo
    conjunto.password_hash = bcrypt.hash(password_nueva)
    if correo_recuperacion_nuevo:
        conjunto.correo_recuperacion = correo_recuperacion_nuevo
    db.commit()

    request.session.clear()
    return RedirectResponse("/login?traspaso=1", status_code=302)


# ---------------------------------------------------------------------------
# Scheduler: reporte mensual automático (día 1 de cada mes)
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler()
scheduler.add_job(enviar_reporte_automatico_mensual, "cron", day=1, hour=8, minute=0)


@app.on_event("startup")
def iniciar_scheduler():
    if not scheduler.running:
        scheduler.start()
