import datetime as dt
import os
import re
import shutil
import urllib.parse
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.hash import bcrypt
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from .database import Base, engine, get_db, DATA_DIR, asegurar_columnas_nuevas
from . import models
from .models import Cupon
from .models import (
    TIPOS_PROPIEDAD,
    TIPOS_PROPIEDAD_DICT,
    CONCEPTOS_PAGO,
    CONCEPTOS_PAGO_DICT,
    METODOS_PAGO,
    METODOS_PAGO_DICT,
)
from .services.cartera import estado_conjunto, estado_propiedad, orden_natural
from .services.comprobante import (
    comprobante_para_adjuntar,
    comprobante_pdf,
    comprobante_pdf_para_adjuntar,
    comprobante_png,
)
from .services.email import enviar_correo, modo_real_configurado
from .services.exportar import exportar_conjunto
from .services.plantilla_propiedades import (
    generar_plantilla as generar_plantilla_propiedades,
    leer_plantilla as leer_plantilla_propiedades,
)
from .services.formato import dinero, estado_saldo, mes_titulo
from .services.reportes import (
    datos_reporte,
    hay_reporte_disponible,
    mes_reportable,
    meses_disponibles,
    resumen_actual,
    saldo_acumulado,
)
from .services.imagen_reporte import generar_imagen_reporte, periodo_en_espanol
from .services.pdf_reporte import generar_pdf_reporte, nombre_archivo_pdf
from .services.recordatorios import ejecutar_recordatorios
from .services.pagos_stripe import iniciar_actualizacion_metodo_pago
from .demo import MODO_DEMO, DEMO_EMAIL, DEMO_PASSWORD, DEMO_NOMBRE, sembrar_si_hace_falta

Base.metadata.create_all(bind=engine)
asegurar_columnas_nuevas()

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


def _version_estaticos() -> str:
    """Huella de los estilos y el logo. Va pegada a sus URLs (?v=...) para que
    cada despliegue que los cambie obligue al navegador a bajar la versión
    nueva — si no, el celular se queda con la hoja de estilos vieja guardada
    y un arreglo ya publicado parece no haberse aplicado."""
    import hashlib

    h = hashlib.md5()
    for rel in ("css/style.css", "img/logo.png", "img/favicon.png"):
        ruta = os.path.join(BASE_DIR, "static", rel)
        if os.path.exists(ruta):
            with open(ruta, "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:10]


templates.env.globals["v_static"] = _version_estaticos()

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
    aplica_recargo_tardio: str = Form(""),
    monto_mensual_tardio: str = Form(""),
    monto_revision_meses: int = Form(12),
    fecha_limite_pago: int = Form(11),
    saldo_inicial: float = Form(0.0),
    num_propiedades: int = Form(...),
    db: Session = Depends(get_db),
):
    recargo_activo = aplica_recargo_tardio == "1"
    monto_tardio = None
    if recargo_activo and monto_mensual_tardio.strip():
        try:
            monto_tardio = round(float(monto_mensual_tardio), 2)
        except ValueError:
            monto_tardio = None
    if not monto_tardio:
        recargo_activo = False

    # A partir de cuándo el sistema empieza a cobrar la cuota mensual sola.
    # El saldo inicial que se va a capturar por propiedad (aquí o en el
    # Excel, justo después de esta pantalla) se asume correcto al día en que
    # se captura, sin importar qué día del mes sea. Por eso, si hoy ya pasó
    # la fecha límite de pago de este conjunto, el mes en curso se da por
    # cobrado dentro de ese saldo y el cobro automático empieza hasta el
    # siguiente; si todavía no llega la fecha límite, este mes ni siquiera es
    # exigible todavía, así que el cobro automático sí lo incluye.
    hoy = dt.date.today()
    limite_pago = min(max(int(fecha_limite_pago or 11), 1), 31)
    inicio_cobros = restar_meses(hoy, -1) if hoy.day > limite_pago else hoy

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
        aplica_recargo_tardio=recargo_activo,
        monto_mensual_tardio=monto_tardio,
        monto_revision_meses=monto_revision_meses,
        fecha_limite_pago=limite_pago,
        saldo_inicial=round(max(saldo_inicial or 0.0, 0.0), 2),
        fecha_inicio_cobros=inicio_cobros,
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

@app.get("/dashboard/desglose", response_class=HTMLResponse)
def dashboard_desglose(
    request: Request,
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Dashboard interno con el desglose del saldo, ingresos y egresos."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    resumen = resumen_actual(conjunto)
    return tpl(request, "dashboard_desglose.html", conjunto=conjunto, resumen=resumen)


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
        hoy_fecha=dt.date.today(),
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
        # Ordenadas por id (orden de creación), NO por número: si se ordenara
        # por número, cambiar el 5 por el 201 movería esa tarjeta hasta el
        # final de la lista a media captura, y el administrador perdería el
        # hilo de en cuál se había quedado. El orden se estabiliza por
        # número recién cuando se editan desde Configuración, donde el
        # número ya no cambia.
        propiedades=sorted(conjunto.propiedades, key=lambda p: p.id),
        tipos=TIPOS_PROPIEDAD,
        error=request.query_params.get("error"),
        guardada=request.query_params.get("guardada"),
        importadas=request.query_params.get("importadas"),
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


@app.get("/propiedades/plantilla.xlsx")
def propiedades_plantilla(conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    contenido = generar_plantilla_propiedades(conjunto)
    nombre = f"propiedades_{conjunto.nombre}.xlsx".replace(" ", "_")
    return Response(
        content=contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.post("/propiedades/importar", response_class=HTMLResponse)
async def propiedades_importar(
    request: Request,
    archivo: UploadFile = File(...),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Lee el Excel subido y muestra una pantalla de confirmación — todavía
    no escribe nada en la base de datos. Ver propiedades_importar_confirmar
    para el paso que sí guarda."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    contenido = await archivo.read()
    filas, errores = leer_plantilla_propiedades(contenido, conjunto)

    if not filas:
        return RedirectResponse(
            f"/propiedades?error={urllib.parse.quote(errores[0] if errores else 'El archivo no tiene renglones que se puedan leer.')}",
            status_code=302,
        )

    import json as _json

    return tpl(
        request,
        "propiedades_importar_confirmar.html",
        conjunto=conjunto,
        filas=filas,
        errores=errores,
        datos_json=_json.dumps(filas),
    )


@app.post("/propiedades/importar/confirmar")
def propiedades_importar_confirmar(
    datos: str = Form(...),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    import json as _json

    try:
        filas = _json.loads(datos)
    except ValueError:
        return RedirectResponse("/propiedades?error=No%20se%20pudo%20leer%20la%20confirmaci%C3%B3n.", status_code=302)

    aplicadas = 0
    for f in filas:
        propiedad = db.query(models.Propiedad).filter_by(
            id=f["propiedad_id"], conjunto_id=conjunto.id
        ).first()
        if not propiedad:
            continue
        nuevo_numero = (f.get("numero") or "").strip()
        if nuevo_numero:
            propiedad.numero = nuevo_numero
        propiedad.tipo = f["tipo"] if f["tipo"] in TIPOS_PROPIEDAD_DICT else "otro"
        propiedad.nombre_dueno = f["nombre_dueno"].strip()
        propiedad.celular_dueno = f["celular_dueno"].strip() or None
        propiedad.email_dueno = f["email_dueno"].strip() or None
        propiedad.nombre_residente = f["nombre_residente"].strip() or None
        propiedad.celular_residente = f["celular_residente"].strip() or None
        propiedad.email_residente = f["email_residente"].strip() or None
        propiedad.notas = f["notas"].strip() or "N/A"
        propiedad.saldo_inicial = _saldo_desde_opciones(f["tipo_saldo"], f["monto_saldo"])
        aplicadas += 1

    db.commit()
    return RedirectResponse(f"/propiedades?importadas={aplicadas}", status_code=302)


# ---------------------------------------------------------------------------
# Pagos (ingresos) + comprobante
# ---------------------------------------------------------------------------

def _agrupar_recibos(pagos) -> list[dict]:
    """Junta las partidas de un mismo recibo (un depósito que cubrió varios
    conceptos) en un solo renglón. Los pagos viejos, de un solo concepto,
    quedan como recibos de una sola partida."""
    recibos = {}
    orden = []
    for p in pagos:
        clave = p.folio_recibo
        if clave not in recibos:
            recibos[clave] = []
            orden.append(clave)
        recibos[clave].append(p)
    resultado = []
    for clave in orden:
        partidas = sorted(recibos[clave], key=lambda x: x.id)
        resultado.append(_recibo_desde_partidas(clave, partidas))
    return resultado


def _recibo_desde_partidas(folio: str, partidas: list) -> dict:
    primera = partidas[0]
    return {
        "folio": folio,
        "partidas": partidas,
        "primera": primera,
        "propiedad": primera.propiedad,
        "fecha_recepcion": primera.fecha_recepcion,
        "metodo_pago_legible": primera.metodo_pago_legible,
        "total": round(sum(p.monto for p in partidas), 2),
        "cancelado": all(p.cancelado for p in partidas),
        "cancelado_en": primera.cancelado_en,
        "puede_cancelarse": all(p.puede_cancelarse for p in partidas),
        "varias": len(partidas) > 1,
    }


def _partidas_de_recibo(db: Session, conjunto, folio: str) -> list:
    """Todas las partidas del recibo al que pertenece `folio` (sirve tanto
    el folio del recibo como el de una de sus partidas: folio-2, folio-3…)."""
    pago = db.query(models.Pago).filter_by(folio=folio, conjunto_id=conjunto.id).first()
    if not pago:
        return []
    clave = pago.folio_recibo
    partidas = (
        db.query(models.Pago)
        .filter(models.Pago.conjunto_id == conjunto.id)
        .filter((models.Pago.recibo == clave) | (models.Pago.folio == clave))
        .order_by(models.Pago.id)
        .all()
    )
    return partidas


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
    recibos = _agrupar_recibos(pagos)
    total_recibos = len(recibos)

    # Vista por defecto: los últimos 4 meses. No es un corte de acceso — el
    # historial completo sigue ahí y se ve con "Ver todo el historial".
    ver_todo = todos == "1"
    desde_efectivo = desde
    if not ver_todo and not desde:
        desde_efectivo = restar_meses(dt.date.today(), MESES_VISTA_PAGOS - 1).isoformat()

    # Todos los filtros son combinables entre sí: se aplican uno tras otro.
    # Un recibo con varias partidas aparece si CUALQUIERA de sus partidas
    # coincide (p. ej. al filtrar por Gas sale el depósito de mantenimiento
    # + gas), y se muestra completo.
    if folio.strip():
        aguja = folio.strip().lower()
        recibos = [r for r in recibos if aguja in r["folio"].lower()]
    if propiedad_id:
        recibos = [r for r in recibos if str(r["propiedad"].id) == propiedad_id]
    if concepto:
        recibos = [r for r in recibos if any(p.concepto == concepto for p in r["partidas"])]
    if desde_efectivo:
        limite = dt.date.fromisoformat(desde_efectivo)
        recibos = [r for r in recibos if r["fecha_recepcion"] >= limite]
    if hasta:
        limite = dt.date.fromisoformat(hasta)
        recibos = [r for r in recibos if r["fecha_recepcion"] <= limite]

    filtros_activos = bool(folio.strip() or propiedad_id or concepto or desde or hasta)

    return tpl(
        request,
        "pagos.html",
        conjunto=conjunto,
        recibos=recibos,
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
        total_pagos=total_recibos,
        error=request.query_params.get("error"),
        cancelado=request.query_params.get("cancelado"),
    )


def _contexto_pago_nuevo(request, conjunto, error=None, previo=None):
    return tpl(
        request,
        "pago_nuevo.html",
        conjunto=conjunto,
        propiedades=sorted(conjunto.propiedades, key=orden_natural),
        proyectos=[p for p in conjunto.proyectos if p.en_curso],
        conceptos=CONCEPTOS_PAGO,
        metodos=METODOS_PAGO,
        hoy=dt.date.today().isoformat(),
        error=error,
        previo=previo or {},
    )


@app.get("/pagos/nuevo", response_class=HTMLResponse)
def pago_nuevo_form(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    return _contexto_pago_nuevo(request, conjunto)


@app.get("/pagos/monto-sugerido/{propiedad_id}")
def pago_monto_sugerido(
    propiedad_id: int, conjunto=Depends(requerir_login), db: Session = Depends(get_db)
):
    """Lo que esa propiedad debe hoy, separado en mantenimiento y en cada
    proyecto (ya con el monto por pago posterior al día límite si aplica).
    Alimenta el botón "Usar lo que debe hoy" del formulario de pago — no es
    la cuota fija, es el adeudo real de hoy, que es lo que de verdad se
    necesita para saldar."""
    if not conjunto:
        return {"monto": 0.0, "mantenimiento": 0.0, "proyectos": []}
    propiedad = db.query(models.Propiedad).filter_by(id=propiedad_id, conjunto_id=conjunto.id).first()
    if not propiedad:
        return {"monto": 0.0, "mantenimiento": 0.0, "proyectos": []}
    estado = estado_propiedad(propiedad)
    total = max(round(estado["saldo"], 2), 0.0)
    en_curso = {p.id for p in conjunto.proyectos if p.en_curso}
    proyectos = [
        {"proyecto_id": d["proyecto_id"], "nombre": d["nombre"], "pendiente": d["saldo"]}
        for d in estado["detalle_proyectos"]
        if d["saldo"] > 0.005 and d["proyecto_id"] in en_curso
    ]
    # Si hay saldo a favor que no alcanzó a aplicarse, lo que debe en total
    # puede ser menos que la suma de lo pendiente: nunca se sugiere más.
    if sum(d["pendiente"] for d in proyectos) > total:
        proyectos = []
    mantenimiento = round(max(total - sum(d["pendiente"] for d in proyectos), 0.0), 2)
    return {"monto": total, "mantenimiento": mantenimiento, "proyectos": proyectos}


@app.post("/pagos/nuevo")
def pago_nuevo_submit(
    request: Request,
    propiedad_id: int = Form(...),
    fecha_recepcion: str = Form(...),
    concepto: list[str] = Form(...),
    concepto_descripcion: list[str] = Form([]),
    monto: list[str] = Form(...),
    proyecto_id: list[str] = Form([]),
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

    if metodo_pago not in METODOS_PAGO_DICT:
        metodo_pago = "otro"
    fecha_pago = dt.datetime.strptime(fecha_recepcion, "%Y-%m-%d").date()

    previo = {
        "propiedad_id": propiedad_id,
        "fecha_recepcion": fecha_recepcion,
        "metodo_pago": metodo_pago,
        "correo_destino": correo_destino,
        "partidas": [],
    }

    # Cada renglón del formulario es una partida: concepto + monto (+ el
    # proyecto, si el concepto es Proyecto). Los renglones vacíos se ignoran.
    proyectos_ids = list(proyecto_id) + [""] * (len(concepto) - len(proyecto_id))
    descs = list(concepto_descripcion) + [""] * len(concepto)
    partidas = []
    error = None
    for i, (c, m) in enumerate(zip(concepto, monto)):
        pid = proyectos_ids[i] if i < len(proyectos_ids) else ""
        previo["partidas"].append({"concepto": c, "monto": m, "proyecto_id": pid})
        try:
            valor = round(float((m or "").replace(",", "").strip() or 0), 2)
        except ValueError:
            error = "Uno de los montos no es un número válido."
            continue
        if valor <= 0:
            continue
        if c not in CONCEPTOS_PAGO_DICT:
            c = "otros"
        proyecto_ref = None
        if c == "proyecto":
            if pid:
                proyecto_ref = (
                    db.query(models.Proyecto).filter_by(id=int(pid), conjunto_id=conjunto.id).first()
                )
            if not proyecto_ref:
                error = "Elige a qué proyecto corresponde el monto de «Proyecto»."
                continue
        partidas.append((c, valor, proyecto_ref))

    if not error and not partidas:
        error = "Escribe el monto recibido de al menos un concepto."
    if error:
        return _contexto_pago_nuevo(request, conjunto, error=error, previo=previo)

    folio = conjunto.siguiente_folio()

    creadas = []
    for n, (c, valor, proyecto_ref) in enumerate(partidas, start=1):
        # Si el mantenimiento de este conjunto ya está en zona de pago tardío
        # (el mes ya pasó su fecha límite), el comprobante lo explica. Es un
        # snapshot para mostrarlo — la cartera siempre se calcula en vivo,
        # esto no la afecta.
        incluye_recargo = bool(
            c == "mantenimiento"
            and conjunto.aplica_recargo_tardio
            and conjunto.monto_mensual_tardio
            and conjunto.cargo_del_mes_es_exigible(fecha_pago.year, fecha_pago.month, fecha_pago)
        )
        monto_base = conjunto.monto_vigente_en(fecha_pago) if incluye_recargo else None
        desc = descs[n-1].strip() if n-1 < len(descs) else ""
        pago = models.Pago(
            conjunto_id=conjunto.id,
            propiedad_id=propiedad.id,
            proyecto_id=proyecto_ref.id if proyecto_ref else None,
            folio=folio if n == 1 else f"{folio}-{n}",
            recibo=folio,
            fecha_recepcion=fecha_pago,
            monto=valor,
            concepto=c,
            concepto_descripcion=desc if c == "otros" and desc else None,
            metodo_pago=metodo_pago,
            incluye_recargo_tardio=incluye_recargo,
            monto_base_snapshot=monto_base,
        )
        db.add(pago)
        creadas.append(pago)
    db.commit()
    for p in creadas:
        db.refresh(p)

    # El comprobante se dibuja al vuelo. Solo se escriben archivos temporales
    # para poder adjuntarlos al correo.
    adjuntos = [comprobante_pdf_para_adjuntar(creadas), comprobante_para_adjuntar(creadas)]
    recibo = _recibo_desde_partidas(folio, creadas)

    # El comprobante siempre se manda al correo de la cuenta, sin que el
    # administrador tenga que acordarse.
    avisar_a_la_cuenta(
        conjunto,
        f"Comprobante de pago - {conjunto.nombre} - Folio {folio}",
        "correo_comprobante.html",
        adjuntos=adjuntos,
        recibo=recibo,
    )

    # Mandárselo también al vecino que pagó es opcional.
    destino = correo_destino.strip()
    if destino:
        cuerpo = templates.get_template("correo_comprobante.html").render(recibo=recibo, conjunto=conjunto)
        enviar_correo(
            destino,
            f"Comprobante de pago - {conjunto.nombre} - Folio {folio}",
            cuerpo,
            adjuntos,
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
    partidas = _partidas_de_recibo(db, conjunto, folio)
    if not partidas:
        return RedirectResponse("/pagos", status_code=302)
    return Response(
        content=comprobante_png(partidas),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="Comprobante {partidas[0].folio_recibo}.png"'},
    )


@app.get("/comprobantes/{folio}/pdf")
def comprobante_pdf_descarga(folio: str, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    partidas = _partidas_de_recibo(db, conjunto, folio)
    if not partidas:
        return RedirectResponse("/pagos", status_code=302)
    return Response(
        content=comprobante_pdf(partidas),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Comprobante {partidas[0].folio_recibo}.pdf"'},
    )


def _correos_sugeridos(propiedad) -> list[dict]:
    sugeridos = []
    if propiedad.email_dueno:
        sugeridos.append({"correo": propiedad.email_dueno, "quien": propiedad.nombre_dueno or "Propietario"})
    if propiedad.email_residente and propiedad.email_residente != propiedad.email_dueno:
        sugeridos.append({"correo": propiedad.email_residente, "quien": propiedad.nombre_residente or "Residente"})
    return sugeridos


@app.get("/comprobantes/{folio}", response_class=HTMLResponse)
def ver_comprobante(folio: str, request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    partidas = _partidas_de_recibo(db, conjunto, folio)
    if not partidas:
        return RedirectResponse("/pagos", status_code=302)
    recibo = _recibo_desde_partidas(partidas[0].folio_recibo, partidas)
    return tpl(
        request,
        "comprobante.html",
        conjunto=conjunto,
        recibo=recibo,
        correos_sugeridos=_correos_sugeridos(recibo["propiedad"]),
        smtp_real=modo_real_configurado(),
        enviado_al_vecino=request.query_params.get("vecino") == "1",
        error=request.query_params.get("error"),
        cancelado=request.query_params.get("cancelado"),
        enviado_a=request.query_params.get("enviado_a"),
    )


@app.post("/comprobantes/{folio}/enviar")
def comprobante_enviar(
    folio: str,
    correo: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Manda el comprobante (PDF + imagen) al correo que se escriba. Es la
    forma de enviarlo directo desde la computadora."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    partidas = _partidas_de_recibo(db, conjunto, folio)
    if not partidas:
        return RedirectResponse("/pagos", status_code=302)
    folio_recibo = partidas[0].folio_recibo
    destino = correo.strip()
    if not _parece_correo(destino):
        return RedirectResponse(
            f"/comprobantes/{folio_recibo}?error={urllib.parse.quote('Ese correo no parece válido. Revísalo e intenta de nuevo.')}",
            status_code=302,
        )
    recibo = _recibo_desde_partidas(folio_recibo, partidas)
    cuerpo = templates.get_template("correo_comprobante.html").render(recibo=recibo, conjunto=conjunto)
    enviar_correo(
        destino,
        f"Comprobante de pago - {conjunto.nombre} - Folio {folio_recibo}",
        cuerpo,
        [comprobante_pdf_para_adjuntar(partidas), comprobante_para_adjuntar(partidas)],
    )
    return RedirectResponse(
        f"/comprobantes/{folio_recibo}?enviado_a={urllib.parse.quote(destino)}", status_code=302
    )


@app.post("/pagos/{pago_id}/cancelar")
def pago_cancelar(
    pago_id: int,
    volver: str = Form("pagos"),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Deshace el daño de un pago mal registrado, sin borrar el rastro: el
    pago se marca como cancelado (nunca se elimina) y deja de contar para la
    cartera, como si nunca hubiera entrado ese dinero. Solo se puede hacer
    dentro de las primeras 48 horas de haberlo registrado — pasado ese
    tiempo, ya no se puede deshacer desde aquí.

    Si el pago es un recibo con varias partidas, se cancela el recibo
    completo: fue un solo depósito, y cancelar solo una parte dejaría un
    comprobante que ya no coincide con lo que se entregó.
    """
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    pago = db.query(models.Pago).filter_by(id=pago_id, conjunto_id=conjunto.id).first()
    if not pago:
        return RedirectResponse("/pagos", status_code=302)

    partidas = _partidas_de_recibo(db, conjunto, pago.folio)
    destino = f"/comprobantes/{pago.folio_recibo}" if volver == "comprobante" else "/pagos"

    if not all(p.puede_cancelarse for p in partidas):
        mensaje = (
            "Este pago ya está cancelado."
            if all(p.cancelado for p in partidas)
            else "Ya pasaron más de 48 horas desde que se registró este pago, así que ya no se puede cancelar desde aquí."
        )
        return RedirectResponse(f"{destino}?error={urllib.parse.quote(mensaje)}", status_code=302)

    momento = dt.datetime.utcnow()
    for p in partidas:
        p.cancelado = True
        p.cancelado_en = momento
    db.commit()

    return RedirectResponse(f"{destino}?cancelado=1", status_code=302)


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
        cancelado=request.query_params.get("cancelado") == "1",
    )


@app.post("/proyectos/nuevo")
async def proyecto_nuevo(
    request: Request,
    concepto: str = Form(...),
    descripcion: str = Form(""),
    descripcion_detalle: str = Form(""),
    compromisos_proveedor: str = Form(""),
    monto_total: float = Form(...),
    fecha_limite_pago: str = Form(""),
    estado: str = Form("por_iniciar"),
    comentario_estado: str = Form(""),
    financiamiento: str = Form("fondo"),
    financiamiento_pct_fondo: str = Form(""),
    cot1: UploadFile | None = File(None),
    cot1_proveedor: str = Form(""),
    cot1_monto: str = Form(""),
    cot2: UploadFile | None = File(None),
    cot2_proveedor: str = Form(""),
    cot2_monto: str = Form(""),
    cot3: UploadFile | None = File(None),
    cot3_proveedor: str = Form(""),
    cot3_monto: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    def _monto_cot(v: str) -> float | None:
        try:
            return float(v.replace(",", "").strip()) if v.strip() else None
        except ValueError:
            return None

    pct_fondo = _monto_cot(financiamiento_pct_fondo)

    proyecto = models.Proyecto(
        conjunto_id=conjunto.id,
        concepto=concepto,
        descripcion=descripcion or None,
        descripcion_detalle=descripcion_detalle or None,
        compromisos_proveedor=compromisos_proveedor or None,
        monto_total=monto_total,
        monto_por_propiedad=monto_por_propiedad_calculado(conjunto, monto_total),
        fecha_limite_pago=dt.datetime.strptime(fecha_limite_pago, "%Y-%m-%d").date()
        if fecha_limite_pago
        else None,
        estado=estado,
        comentario_estado=comentario_estado or None,
        financiamiento=financiamiento or None,
        financiamiento_pct_fondo=pct_fondo,
        cot1_proveedor=cot1_proveedor or None,
        cot1_monto=_monto_cot(cot1_monto),
        cot2_proveedor=cot2_proveedor or None,
        cot2_monto=_monto_cot(cot2_monto),
        cot3_proveedor=cot3_proveedor or None,
        cot3_monto=_monto_cot(cot3_monto),
    )
    db.add(proyecto)
    db.flush()   # necesitamos el id para nombrar los archivos

    for slot, archivo in [("cot1", cot1), ("cot2", cot2), ("cot3", cot3)]:
        if _archivo_subido(archivo):
            setattr(proyecto, f"{slot}_path", _guardar_cot(archivo, conjunto.id, slot))

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
async def proyecto_editar_submit(
    proyecto_id: int,
    request: Request,
    concepto: str = Form(...),
    descripcion: str = Form(""),
    descripcion_detalle: str = Form(""),
    compromisos_proveedor: str = Form(""),
    monto_total: float = Form(...),
    fecha_limite_pago: str = Form(""),
    estado: str = Form("por_iniciar"),
    comentario_estado: str = Form(""),
    cot1: UploadFile | None = File(None),
    cot1_proveedor: str = Form(""),
    cot1_monto: str = Form(""),
    cot2: UploadFile | None = File(None),
    cot2_proveedor: str = Form(""),
    cot2_monto: str = Form(""),
    cot3: UploadFile | None = File(None),
    cot3_proveedor: str = Form(""),
    cot3_monto: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if not proyecto:
        return RedirectResponse("/proyectos", status_code=302)

    def _monto_cot(v: str) -> float | None:
        try:
            return float(v.replace(",", "").strip()) if v.strip() else None
        except ValueError:
            return None

    proyecto.concepto = concepto
    proyecto.descripcion = descripcion or None
    proyecto.descripcion_detalle = descripcion_detalle or None
    proyecto.compromisos_proveedor = compromisos_proveedor or None
    proyecto.monto_total = monto_total
    proyecto.monto_por_propiedad = monto_por_propiedad_calculado(conjunto, monto_total)
    proyecto.fecha_limite_pago = (
        dt.datetime.strptime(fecha_limite_pago, "%Y-%m-%d").date() if fecha_limite_pago else None
    )
    proyecto.estado = estado
    proyecto.comentario_estado = comentario_estado or None
    proyecto.cot1_proveedor = cot1_proveedor or None
    proyecto.cot1_monto = _monto_cot(cot1_monto)
    proyecto.cot2_proveedor = cot2_proveedor or None
    proyecto.cot2_monto = _monto_cot(cot2_monto)
    proyecto.cot3_proveedor = cot3_proveedor or None
    proyecto.cot3_monto = _monto_cot(cot3_monto)

    for slot, archivo in [("cot1", cot1), ("cot2", cot2), ("cot3", cot3)]:
        if _archivo_subido(archivo):
            setattr(proyecto, f"{slot}_path", _guardar_cot(archivo, conjunto.id, slot))

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



@app.get("/proyectos/{proyecto_id}/pagos", response_class=HTMLResponse)
def proyecto_pagos_lista(
    proyecto_id: int,
    request: Request,
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Quién ha pagado su cuota de este proyecto y quién no."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if not proyecto:
        return RedirectResponse("/proyectos", status_code=302)

    pagado_por = {}
    for p in proyecto.pagos:
        if not p.cancelado:
            pagado_por[p.propiedad_id] = round(pagado_por.get(p.propiedad_id, 0.0) + p.monto, 2)

    filas = []
    for prop in sorted(conjunto.propiedades, key=orden_natural):
        if not prop.activo:
            continue
        pagado = pagado_por.get(prop.id, 0.0)
        pendiente = round(max(proyecto.monto_por_propiedad - pagado, 0.0), 2)
        filas.append({
            "propiedad": prop,
            "pagado": pagado,
            "pendiente": pendiente,
            "completo": pendiente <= 0.005,
        })

    return tpl(
        request, "proyecto_pagos.html",
        conjunto=conjunto, proyecto=proyecto, filas=filas,
        total_pagado=round(sum(f["pagado"] for f in filas), 2),
        total_pendiente=round(sum(f["pendiente"] for f in filas), 2),
    )


@app.get("/proyectos/{proyecto_id}/cancelar", response_class=HTMLResponse)
def proyecto_cancelar_form(
    proyecto_id: int,
    request: Request,
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Pantalla de cancelación con vista previa del impacto en cada propiedad."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if not proyecto or proyecto.cancelado:
        return RedirectResponse("/proyectos", status_code=302)
    return tpl(request, "proyecto_cancelar.html", conjunto=conjunto, proyecto=proyecto,
               preview=None, error=None)


def _preview_cancelacion(proyecto, sin_recuperar: float,
                          reparto: str, credito_modo: str) -> list[dict]:
    """Calcula qué le pasaría a cada propiedad si se cancela el proyecto.

    Devuelve una lista de filas con:
    - propiedad, pagado_al_proyecto, perdida_asignada, devuelto,
      saldo_actual, saldo_nuevo, delta (negativo = favor, positivo = debe más)
    """
    from app.services.cartera import estado_propiedad

    pagos_proyecto = [
        p for p in proyecto.pagos
        if not p.cancelado and p.concepto == "proyecto"
    ]

    # Mapa propiedad_id → cuánto pagó al proyecto
    pagado_por = {}
    for p in pagos_proyecto:
        pagado_por[p.propiedad_id] = round(pagado_por.get(p.propiedad_id, 0.0) + p.monto, 2)

    total_recaudado = round(sum(pagado_por.values()), 2)
    sin_recuperar = max(0.0, min(round(sin_recuperar, 2), total_recaudado))

    propiedades_activas = [prop for prop in proyecto.conjunto.propiedades if prop.activo]
    n_todas = len(propiedades_activas)
    n_pagaron = len(pagado_por)

    filas = []
    for prop in sorted(propiedades_activas, key=lambda x: x.id):
        pagado = round(pagado_por.get(prop.id, 0.0), 2)
        estado_actual = estado_propiedad(prop)
        saldo_actual = round(estado_actual["saldo"], 2)

        if reparto == "todas":
            # La pérdida se divide entre todas las propiedades activas
            perdida = round(sin_recuperar / n_todas, 2) if n_todas else 0.0
        else:
            # Solo entre quienes pagaron, en proporción a lo que aportaron
            if pagado > 0 and total_recaudado > 0:
                perdida = round(sin_recuperar * pagado / total_recaudado, 2)
            else:
                perdida = 0.0

        devuelto = max(0.0, round(pagado - perdida, 2))

        if credito_modo == "todo_favor":
            # Todo lo devuelto queda como saldo a favor, sin tocar el adeudo
            saldo_nuevo = round(saldo_actual - devuelto, 2)
        else:
            # Lo devuelto primero cubre lo que deba, y solo el sobrante queda a favor
            debe = max(0.0, saldo_actual)
            cubre = min(devuelto, debe)
            sobrante = round(devuelto - cubre, 2)
            saldo_nuevo = round(saldo_actual - cubre - sobrante, 2)

        filas.append({
            "propiedad": prop,
            "pagado": pagado,
            "perdida": perdida,
            "devuelto": devuelto,
            "saldo_actual": saldo_actual,
            "saldo_nuevo": saldo_nuevo,
            "cambia": abs(saldo_nuevo - saldo_actual) > 0.005,
        })

    return filas


@app.post("/proyectos/{proyecto_id}/cancelar/preview")
def proyecto_cancelar_preview(
    proyecto_id: int,
    request: Request,
    motivo: str = Form(""),
    sin_recuperar: str = Form("0"),
    reparto_perdida: str = Form("pagaron"),
    credito_modo: str = Form("cubrir_deuda"),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Calcula la vista previa sin aplicar nada."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if not proyecto or proyecto.cancelado:
        return RedirectResponse("/proyectos", status_code=302)
    try:
        monto_no_rec = float((sin_recuperar or "0").replace(",", "").strip())
    except ValueError:
        monto_no_rec = 0.0
    error = None
    if not motivo.strip():
        error = "Escribe el motivo de la cancelación antes de ver la vista previa."
    preview = None if error else _preview_cancelacion(proyecto, monto_no_rec, reparto_perdida, credito_modo)
    return tpl(request, "proyecto_cancelar.html", conjunto=conjunto, proyecto=proyecto,
               preview=preview, error=error,
               form={"motivo": motivo, "sin_recuperar": sin_recuperar,
                     "reparto_perdida": reparto_perdida, "credito_modo": credito_modo})


@app.post("/proyectos/{proyecto_id}/cancelar/confirmar")
async def proyecto_cancelar_confirmar(
    proyecto_id: int,
    request: Request,
    motivo: str = Form(...),
    sin_recuperar: str = Form("0"),
    reparto_perdida: str = Form("pagaron"),
    credito_modo: str = Form("cubrir_deuda"),
    respaldo: UploadFile | None = File(None),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Aplica la cancelación: marca el proyecto, cancela sus pagos y crea
    los créditos (pagos con monto negativo) para cada propiedad."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if not proyecto or proyecto.cancelado:
        return RedirectResponse("/proyectos", status_code=302)
    if not motivo.strip():
        return tpl(request, "proyecto_cancelar.html", conjunto=conjunto, proyecto=proyecto,
                   preview=None, error="El motivo es obligatorio.")

    try:
        monto_no_rec = float((sin_recuperar or "0").replace(",", "").strip())
    except ValueError:
        monto_no_rec = 0.0

    preview = _preview_cancelacion(proyecto, monto_no_rec, reparto_perdida, credito_modo)
    ahora = dt.datetime.utcnow()

    # 1. Cancelar los pagos del proyecto (dejan de sumar en la cartera)
    for pago in proyecto.pagos:
        if not pago.cancelado:
            pago.cancelado = True
            pago.cancelado_en = ahora

    # 2. Crear créditos de reembolso para las propiedades que recibirán algo.
    #    Se usan pagos de concepto "mantenimiento" con monto = devuelto para
    #    que la cartera los aplique como saldo a favor.  Con credito_modo
    #    "todo_favor" se usa concepto "otros" para que no baje la deuda de
    #    mantenimiento (el crédito se queda a favor libre).
    for fila in preview:
        if fila["devuelto"] <= 0.005:
            continue
        concepto_credito = "mantenimiento" if credito_modo == "cubrir_deuda" else "otros"
        conjunto.ultimo_folio += 1
        folio = f"AII-{conjunto.id:04d}-{conjunto.ultimo_folio:05d}"
        db.add(models.Pago(
            conjunto_id=conjunto.id,
            propiedad_id=fila["propiedad"].id,
            folio=folio,
            recibo=folio,
            fecha_recepcion=ahora.date(),
            monto=fila["devuelto"],
            concepto=concepto_credito,
            metodo_pago="otro",
        ))

    # 3. Guardar respaldo si se subió uno
    ruta_respaldo = None
    if _archivo_subido(respaldo):
        ruta_respaldo = _guardar_cot(respaldo, conjunto.id, "cancelacion")

    # 4. Marcar el proyecto como cancelado
    proyecto.cancelado = True
    proyecto.cancelado_en = ahora
    proyecto.cancelado_motivo = motivo.strip()
    proyecto.cancelado_sin_recuperar = round(monto_no_rec, 2)
    proyecto.cancelado_reparto_perdida = reparto_perdida
    proyecto.cancelado_credito_modo = credito_modo
    if ruta_respaldo:
        proyecto.cot3_path = ruta_respaldo   # reutilizamos un slot libre

    db.commit()
    return RedirectResponse("/proyectos?cancelado=1", status_code=302)


@app.get("/registro/validar-cupon")
def validar_cupon(codigo: str = "", db: Session = Depends(get_db)):
    """Valida un código de cupón en tiempo real (llamado por JS en el registro)."""
    if not codigo.strip():
        return {"valido": False, "mensaje": ""}
    cupon = db.query(Cupon).filter_by(codigo=codigo.strip().upper()).first()
    if not cupon or not cupon.disponible:
        return {"valido": False, "mensaje": "Código no válido o ya vencido."}
    return {
        "valido": True,
        "mensaje": f"✓ Descuento de {cupon.descuento_legible} aplicado.",
        "descuento": cupon.descuento_legible,
    }


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------
import stripe as stripe_lib

stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID_BASICO", "")
STRIPE_PUB_KEY  = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PERIODO_PRUEBA_DIAS = 30

def _precio_con_cupon(precio_base: float, cupon) -> float:
    if not cupon:
        return precio_base
    if cupon.descuento_tipo == "porcentaje":
        return round(precio_base * (1 - cupon.descuento_valor / 100), 2)
    return max(0.0, round(precio_base - cupon.descuento_valor, 2))


@app.get("/pago", response_class=HTMLResponse)
def pago_form(
    request: Request,
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Pantalla de suscripción para conjuntos en periodo de prueba o sin pago activo."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    return tpl(request, "pago.html", conjunto=conjunto,
               stripe_pub_key=STRIPE_PUB_KEY, precio=500,
               error=request.query_params.get("error"))


@app.post("/pago/checkout")
def pago_checkout(
    request: Request,
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Crea una sesión de Stripe Checkout y redirige ahí."""
    if not conjunto or not stripe_lib.api_key or not STRIPE_PRICE_ID:
        return RedirectResponse("/dashboard", status_code=302)
    try:
        session = stripe_lib.checkout.Session.create(
            customer_email=conjunto.login_email,
            payment_method_types=["card"],
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            mode="subscription",
            success_url=str(request.base_url) + "pago/exito?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=str(request.base_url) + "pago?error=cancelado",
            metadata={"conjunto_id": str(conjunto.id)},
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return RedirectResponse(f"/pago?error={urllib.parse.quote(str(e))}", status_code=302)


@app.get("/pago/exito", response_class=HTMLResponse)
def pago_exito(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    return tpl(request, "pago_exito.html", conjunto=conjunto)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Webhook de Stripe: activa o suspende cuentas según el estado del pago."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe_lib.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe_lib.Event.construct_from(
                stripe_lib.util.convert_to_stripe_object(
                    __import__("json").loads(payload), stripe_lib.api_key, None
                ),
                stripe_lib.api_key,
            )
    except Exception:
        return Response(status_code=400)

    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        cid = obj.get("metadata", {}).get("conjunto_id")
        if cid:
            c = db.query(models.Conjunto).filter_by(id=int(cid)).first()
            if c:
                c.stripe_customer_id = obj.get("customer")
                c.stripe_subscription_id = obj.get("subscription")
                c.stripe_status = "active"
                c.prueba_hasta = None
                db.commit()

    elif event["type"] in ("customer.subscription.deleted", "invoice.payment_failed"):
        sub_id = obj.get("id") if event["type"] == "customer.subscription.deleted" else obj.get("subscription")
        if sub_id:
            c = db.query(models.Conjunto).filter_by(stripe_subscription_id=sub_id).first()
            if c:
                c.stripe_status = "canceled" if "deleted" in event["type"] else "past_due"
                db.commit()

    return {"ok": True}


# ---------------------------------------------------------------------------
# Recordatorios automáticos (llamado por el cron job de Render cada día)
# ---------------------------------------------------------------------------

RECORDATORIO_SECRET = os.environ.get("RECORDATORIO_SECRET", "")

@app.post("/recordatorios/ejecutar")
def recordatorios_ejecutar(
    request: Request,
    db: Session = Depends(get_db),
):
    """Endpoint que llama el cron job de Render cada día a las 9 AM CDMX.

    Protegido con un secret para que no lo pueda llamar cualquiera.
    """
    token = request.headers.get("X-Recordatorio-Secret", "")
    if RECORDATORIO_SECRET and token != RECORDATORIO_SECRET:
        return Response(status_code=401)
    resultado = ejecutar_recordatorios(db)
    return resultado


# ---------------------------------------------------------------------------
# Archivos de proyectos (cotizaciones)
# ---------------------------------------------------------------------------
ARCHIVOS_PROYECTOS_DIR = os.path.join(DATA_DIR, "archivos_proyectos")
os.makedirs(ARCHIVOS_PROYECTOS_DIR, exist_ok=True)

COTIZACION_CAMPOS = ["cot1", "cot2", "cot3"]

def _guardar_cot(archivo, conjunto_id: int, slot: str) -> str:
    ext = os.path.splitext(archivo.filename)[1].lower() or TIPO_A_EXT.get(archivo.content_type or "", "")
    nombre = f"{conjunto_id}_{slot}_{uuid.uuid4().hex}{ext}"
    with open(os.path.join(ARCHIVOS_PROYECTOS_DIR, nombre), "wb") as f:
        shutil.copyfileobj(archivo.file, f)
    return nombre

def _ruta_cot(nombre: str | None) -> str | None:
    if not nombre:
        return None
    ruta = os.path.join(ARCHIVOS_PROYECTOS_DIR, os.path.basename(nombre))
    return ruta if os.path.exists(ruta) else None

def _cot_url(proyecto_id: int, slot: str) -> str:
    return f"/proyectos/{proyecto_id}/cotizacion/{slot}"


@app.get("/proyectos/{proyecto_id}/cotizacion/{slot}")
def proyecto_cotizacion(
    proyecto_id: int,
    slot: str,
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto or slot not in COTIZACION_CAMPOS:
        return RedirectResponse("/proyectos", status_code=302)
    proyecto = db.query(models.Proyecto).filter_by(id=proyecto_id, conjunto_id=conjunto.id).first()
    if not proyecto:
        return RedirectResponse("/proyectos", status_code=302)
    ruta = _ruta_cot(getattr(proyecto, f"{slot}_path"))
    if not ruta:
        return HTMLResponse("<p style='font-family:sans-serif'>Archivo no disponible en el servidor.</p>", status_code=404)
    import mimetypes
    tipo = mimetypes.guess_type(ruta)[0] or "application/octet-stream"
    nombre_archivo = f"Cotizacion {slot[-1]} {proyecto.concepto[:40]}{os.path.splitext(ruta)[1]}"
    return FileResponse(ruta, media_type=tipo, filename=nombre_archivo, content_disposition_type="inline")


# ---------------------------------------------------------------------------
# Egresos
# ---------------------------------------------------------------------------

# Los archivos de los egresos (recibo, XML, comprobante de pago) NO viven en
# static/: todo lo que está ahí se puede abrir sin iniciar sesión con solo
# adivinar la dirección. Se guardan aparte y se sirven por una ruta que
# revisa que el egreso sea del conjunto de quien lo pide.
ARCHIVOS_EGRESOS_DIR = os.path.join(DATA_DIR, "archivos_egresos")
os.makedirs(ARCHIVOS_EGRESOS_DIR, exist_ok=True)

TAMANO_MAXIMO_ARCHIVO = 10 * 1024 * 1024  # 10 MB por archivo

# Qué se acepta en cada campo. La extensión manda (es lo que ve el usuario);
# si un celular no manda extensión, se deduce del tipo de contenido.
EXT_IMAGEN_PDF = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}
EXT_XML = {".xml"}
TIPO_A_EXT = {
    "application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/heic": ".heic", "image/heif": ".heif", "image/gif": ".gif",
    "text/xml": ".xml", "application/xml": ".xml",
}
CAMPOS_ARCHIVO_EGRESO = {
    "recibo": ("recibo_path", EXT_IMAGEN_PDF, "el recibo o factura"),
    "xml": ("xml_path", EXT_XML, "el XML de la factura"),
    "comprobante": ("comprobante_path", EXT_IMAGEN_PDF, "el comprobante de pago"),
}


def _archivo_subido(archivo) -> bool:
    return archivo is not None and bool(getattr(archivo, "filename", ""))


def _validar_archivo_egreso(archivo, campo: str) -> str | None:
    """Devuelve un mensaje de error si el archivo no sirve, o None si está bien."""
    _, extensiones, nombre = CAMPOS_ARCHIVO_EGRESO[campo]
    ext = os.path.splitext(archivo.filename)[1].lower() or TIPO_A_EXT.get(archivo.content_type or "", "")
    if ext not in extensiones:
        if campo == "xml":
            return f"En {nombre} solo se acepta un archivo .xml."
        return f"En {nombre} solo se aceptan imágenes (foto o captura) o PDF."
    archivo.file.seek(0, os.SEEK_END)
    tamano = archivo.file.tell()
    archivo.file.seek(0)
    if tamano > TAMANO_MAXIMO_ARCHIVO:
        return f"El archivo de {nombre} pesa más de 10 MB. Prueba con una foto más ligera o un PDF comprimido."
    return None


def _guardar_archivo_egreso(archivo, conjunto_id: int, campo: str) -> str:
    ext = os.path.splitext(archivo.filename)[1].lower() or TIPO_A_EXT.get(archivo.content_type or "", "")
    nombre = f"{conjunto_id}_{campo}_{uuid.uuid4().hex}{ext}"
    with open(os.path.join(ARCHIVOS_EGRESOS_DIR, nombre), "wb") as f:
        shutil.copyfileobj(archivo.file, f)
    return nombre


def _ruta_archivo_egreso(ruta_guardada: str | None) -> str | None:
    """Ruta en disco de un archivo de egreso. Los comprobantes subidos antes
    de esta versión quedaron en static/egresos/ y se siguen encontrando."""
    if not ruta_guardada:
        return None
    if ruta_guardada.startswith("egresos/"):
        ruta = os.path.join(BASE_DIR, "static", ruta_guardada)
    else:
        ruta = os.path.join(ARCHIVOS_EGRESOS_DIR, os.path.basename(ruta_guardada))
    return ruta if os.path.exists(ruta) else None


def _adjuntos_de_egreso(egreso) -> list[str]:
    rutas = [_ruta_archivo_egreso(getattr(egreso, c)) for c in ("recibo_path", "xml_path", "comprobante_path")]
    return [r for r in rutas if r]


def _contexto_egresos(request, conjunto, **extra):
    egresos = sorted(conjunto.egresos, key=lambda e: (e.fecha, e.id), reverse=True)
    hoy = dt.date.today()
    inicio_mes = hoy.replace(day=1)

    # Para el registro rápido de gastos fijos: el último monto usado por cada
    # concepto, para sugerirlo cuando el administrador vuelve a escribir el
    # mismo concepto (jardinería, luz de áreas comunes, etc.).
    ultimo_monto_por_concepto = {}
    for e in sorted(conjunto.egresos, key=lambda e: (e.fecha, e.id)):
        ultimo_monto_por_concepto[e.concepto] = e.monto

    contexto = dict(
        conjunto=conjunto,
        egresos=egresos,
        hoy=hoy.isoformat(),
        formas_pago=models.FORMAS_PAGO_EGRESO,
        conceptos_previos=sorted(ultimo_monto_por_concepto.keys()),
        montos_por_concepto=ultimo_monto_por_concepto,
        # El número grande de esta pantalla es lo que ha salido este mes, que
        # es lo que se está viendo aquí. El saldo acumulado del conjunto vive
        # en Inicio y en el reporte.
        egresos_del_mes=round(
            sum(e.monto for e in conjunto.egresos if e.fecha >= inicio_mes), 2
        ),
        saldo_acumulado=saldo_acumulado(conjunto, hoy),
        editar_id=request.query_params.get("editar"),
        error=None,
        error_editar=None,
        previo={},
        abrir_formulario=False,
    )
    contexto.update(extra)
    return tpl(request, "egresos.html", **contexto)


@app.get("/egresos", response_class=HTMLResponse)
def egresos_lista(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    return _contexto_egresos(request, conjunto)


@app.get("/egresos/{egreso_id}/archivo/{campo}")
def egreso_archivo(
    egreso_id: int, campo: str, conjunto=Depends(requerir_login), db: Session = Depends(get_db)
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    egreso = db.query(models.Egreso).filter_by(id=egreso_id, conjunto_id=conjunto.id).first()
    if not egreso or campo not in CAMPOS_ARCHIVO_EGRESO:
        return RedirectResponse("/egresos", status_code=302)
    ruta = _ruta_archivo_egreso(getattr(egreso, CAMPOS_ARCHIVO_EGRESO[campo][0]))
    if not ruta:
        return HTMLResponse(
            "<p style='font-family:sans-serif'>Este archivo ya no está disponible en el servidor.</p>",
            status_code=404,
        )
    from fastapi.responses import FileResponse

    import mimetypes

    tipo = mimetypes.guess_type(ruta)[0] or "application/octet-stream"
    nombre = f"{campo} - {egreso.concepto[:40]}{os.path.splitext(ruta)[1]}"
    return FileResponse(ruta, media_type=tipo, filename=nombre, content_disposition_type="inline")


@app.post("/egresos/nuevo")
async def egreso_nuevo(
    request: Request,
    concepto: str = Form(...),
    monto: float = Form(...),
    fecha: str = Form(...),
    forma_pago: str = Form(""),
    recibo: UploadFile | None = File(None),
    xml: UploadFile | None = File(None),
    comprobante: UploadFile | None = File(None),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    previo = {"concepto": concepto, "monto": monto, "fecha": fecha, "forma_pago": forma_pago}
    archivos = {"recibo": recibo, "xml": xml, "comprobante": comprobante}

    error = None
    if forma_pago not in models.FORMAS_PAGO_EGRESO_DICT:
        error = "Elige la forma en que se hizo el pago."
    elif not _archivo_subido(recibo):
        error = "Falta subir el recibo o factura (foto o PDF). Si el proveedor no dio recibo, sube la foto de una nota firmada."
    else:
        for campo, archivo in archivos.items():
            if _archivo_subido(archivo):
                error = _validar_archivo_egreso(archivo, campo)
                if error:
                    break
    if error:
        return _contexto_egresos(request, conjunto, error=error, previo=previo, abrir_formulario=True)

    rutas = {
        campo: _guardar_archivo_egreso(archivo, conjunto.id, campo)
        for campo, archivo in archivos.items()
        if _archivo_subido(archivo)
    }

    egreso = models.Egreso(
        conjunto_id=conjunto.id,
        concepto=concepto,
        monto=monto,
        fecha=dt.datetime.strptime(fecha, "%Y-%m-%d").date(),
        forma_pago=forma_pago,
        recibo_path=rutas.get("recibo"),
        xml_path=rutas.get("xml"),
        comprobante_path=rutas.get("comprobante"),
    )
    db.add(egreso)
    db.commit()
    db.refresh(egreso)

    # Cada egreso se avisa automáticamente al correo de la cuenta, con sus
    # archivos: es el control de que nadie saca dinero sin que quede registro.
    avisar_a_la_cuenta(
        conjunto,
        f"Egreso registrado - {conjunto.nombre} - {egreso.concepto}",
        "correo_egreso.html",
        adjuntos=_adjuntos_de_egreso(egreso),
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
    forma_pago: str = Form(""),
    recibo: UploadFile | None = File(None),
    xml: UploadFile | None = File(None),
    comprobante: UploadFile | None = File(None),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)

    egreso = db.query(models.Egreso).filter_by(id=egreso_id, conjunto_id=conjunto.id).first()
    if not egreso:
        return RedirectResponse("/egresos", status_code=302)

    archivos = {"recibo": recibo, "xml": xml, "comprobante": comprobante}
    error = None
    if forma_pago and forma_pago not in models.FORMAS_PAGO_EGRESO_DICT:
        error = "Elige la forma en que se hizo el pago."
    for campo, archivo in archivos.items():
        if not error and _archivo_subido(archivo):
            error = _validar_archivo_egreso(archivo, campo)
    if error:
        return _contexto_egresos(request, conjunto, error_editar=error, editar_id=str(egreso_id))

    egreso.concepto = concepto
    egreso.monto = monto
    egreso.fecha = dt.datetime.strptime(fecha, "%Y-%m-%d").date()
    if forma_pago:
        egreso.forma_pago = forma_pago

    # Subir un archivo aquí reemplaza al anterior de ese mismo campo; los
    # campos que se dejan vacíos conservan lo que ya tenían.
    for campo, archivo in archivos.items():
        if _archivo_subido(archivo):
            setattr(egreso, CAMPOS_ARCHIVO_EGRESO[campo][0], _guardar_archivo_egreso(archivo, conjunto.id, campo))

    db.commit()
    db.refresh(egreso)

    avisar_a_la_cuenta(
        conjunto,
        f"Egreso modificado - {conjunto.nombre} - {egreso.concepto}",
        "correo_egreso.html",
        adjuntos=_adjuntos_de_egreso(egreso),
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
    datos = {
        "concepto": egreso.concepto,
        "monto": egreso.monto,
        "fecha": egreso.fecha,
        "forma_pago_legible": egreso.forma_pago_legible,
    }
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

def _version_pedida(valor: str | None) -> str:
    """Detalle por propiedad (lo de siempre) o resumen sin nombres. Se elige
    cada vez que se genera el reporte; no se guarda como preferencia."""
    return "resumen" if (valor or "").strip() == "resumen" else "detalle"


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


# --- Destinatarios del reporte ------------------------------------------------

MODOS_DESTINATARIOS = ("todos", "duenos", "seleccion")


def _destinatarios_guardados(conjunto) -> dict:
    import json

    base = {"modo": "duenos", "seleccion": [], "extras": ""}
    try:
        guardado = json.loads(conjunto.reporte_destinatarios or "{}")
    except (ValueError, TypeError):
        guardado = {}
    base.update({k: v for k, v in guardado.items() if k in base})
    if base["modo"] not in MODOS_DESTINATARIOS:
        base["modo"] = "duenos"
    return base


def _correos_propiedades(conjunto) -> list[dict]:
    """Para la lista de 'uno por uno': cada propiedad con los correos que tiene
    registrados (propietario y residente)."""
    filas = []
    for p in sorted(conjunto.propiedades, key=orden_natural):
        if not p.activo:
            continue
        correos = []
        if p.email_dueno:
            correos.append({"correo": p.email_dueno.strip(), "quien": p.nombre_dueno or "Propietario", "rol": "propietario"})
        if p.email_residente and p.email_residente.strip().lower() != (p.email_dueno or "").strip().lower():
            correos.append({"correo": p.email_residente.strip(), "quien": p.nombre_residente or "Residente", "rol": "residente"})
        filas.append({"propiedad": p, "correos": correos})
    return filas


def _resolver_destinatarios(conjunto, modo: str, seleccion: list[str], extras: str) -> list[str]:
    correos = []
    if modo == "todos":
        for fila in _correos_propiedades(conjunto):
            correos += [c["correo"] for c in fila["correos"]]
    elif modo == "duenos":
        for fila in _correos_propiedades(conjunto):
            correos += [c["correo"] for c in fila["correos"] if c["rol"] == "propietario"]
    else:
        correos += [c.strip() for c in seleccion if c.strip()]
    correos += [c.strip() for c in re.split(r"[,;\s]+", extras or "") if c.strip()]
    vistos, unicos = set(), []
    for c in correos:
        clave = c.lower()
        if clave not in vistos and _parece_correo(c):
            vistos.add(clave)
            unicos.append(c)
    return unicos


def _contexto_reporte(request, conjunto, reporte, version, **extra):
    guardados = _destinatarios_guardados(conjunto)
    filas_correos = _correos_propiedades(conjunto)
    contexto = dict(
        conjunto=conjunto,
        reporte=reporte,
        version=version,
        sin_meses=False,
        smtp_real=modo_real_configurado(),
        resultado=None,
        destinatarios=guardados,
        filas_correos=filas_correos,
        total_duenos=sum(1 for f in filas_correos for c in f["correos"] if c["rol"] == "propietario"),
        total_correos=sum(len(f["correos"]) for f in filas_correos),
        sin_correo=sum(1 for f in filas_correos if not f["correos"]),
    )
    contexto.update(extra)
    return tpl(request, "reporte.html", **contexto)


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
        )

    reporte = datos_reporte(conjunto, pedido[0], pedido[1])
    version = _version_pedida(request.query_params.get("version"))
    return _contexto_reporte(request, conjunto, reporte, version)


@app.get("/reporte/imprimir", response_class=HTMLResponse)
def reporte_imprimir(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    """Versión limpia del reporte, sin menús ni botones, para imprimirlo en
    papel desde el navegador."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    pedido, _ = _mes_pedido(request, conjunto)
    if pedido is None:
        return RedirectResponse("/reporte", status_code=302)
    reporte = datos_reporte(conjunto, pedido[0], pedido[1])
    version = _version_pedida(request.query_params.get("version"))
    return tpl(request, "reporte_imprimir.html", conjunto=conjunto, reporte=reporte, version=version)


@app.get("/reporte/pdf")
def reporte_pdf(request: Request, conjunto=Depends(requerir_login), db: Session = Depends(get_db)):
    """El reporte como archivo PDF: para descargarlo, compartirlo desde el
    celular o adjuntarlo a un correo."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    pedido, _ = _mes_pedido(request, conjunto)
    if pedido is None:
        return RedirectResponse("/reporte", status_code=302)
    reporte = datos_reporte(conjunto, pedido[0], pedido[1])
    version = _version_pedida(request.query_params.get("version"))
    nombre = nombre_archivo_pdf(conjunto, reporte)
    return Response(
        content=generar_pdf_reporte(conjunto, reporte, version),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(nombre)}"},
    )


def _pdf_reporte_para_adjuntar(conjunto, reporte, version) -> str:
    import tempfile

    carpeta = tempfile.mkdtemp(prefix="reporte_")
    ruta = os.path.join(carpeta, nombre_archivo_pdf(conjunto, reporte))
    with open(ruta, "wb") as f:
        f.write(generar_pdf_reporte(conjunto, reporte, version))
    return ruta


@app.post("/reporte/enviar", response_class=HTMLResponse)
def reporte_enviar(
    request: Request,
    modo: str = Form("duenos"),
    seleccion: list[str] = Form([]),
    extras: str = Form(""),
    version: str = Form("detalle"),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    import json

    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    pedido, _ = _mes_pedido(request, conjunto)
    if pedido is None:
        return RedirectResponse("/reporte", status_code=302)
    reporte = datos_reporte(conjunto, pedido[0], pedido[1])
    version = _version_pedida(version)
    if modo not in MODOS_DESTINATARIOS:
        modo = "duenos"

    # Se recuerda la elección para dejarla puesta el mes que entra.
    conjunto.reporte_destinatarios = json.dumps(
        {"modo": modo, "seleccion": [c.strip() for c in seleccion if c.strip()], "extras": extras.strip()}
    )
    db.commit()

    destinos = _resolver_destinatarios(conjunto, modo, seleccion, extras)
    if not destinos:
        return _contexto_reporte(
            request, conjunto, reporte, version,
            error_envio="No hay ningún correo al cual enviar: marca al menos uno o escribe un correo extra.",
        )

    cuerpo = templates.get_template("correo_reporte.html").render(
        conjunto=conjunto, reporte=reporte, version=version
    )
    asunto = f"Reporte de {reporte['periodo']} - {conjunto.nombre}"
    adjunto = _pdf_reporte_para_adjuntar(conjunto, reporte, version)

    resultados = [(d, enviar_correo(d, asunto, cuerpo, [adjunto])) for d in destinos]
    return _contexto_reporte(request, conjunto, reporte, version, resultado=resultados)


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
            cuerpo = templates.get_template("correo_reporte.html").render(
                conjunto=conjunto, reporte=reporte, version="detalle"
            )
            enviar_correo(
                conjunto.cuenta_email,
                f"Reporte de {reporte['periodo']} - {conjunto.nombre}",
                cuerpo,
                [_pdf_reporte_para_adjuntar(conjunto, reporte, "detalle")],
            )
    finally:
        db.close()


def _correos_de(propiedad) -> list[str]:
    return [c for c in (propiedad.email_dueno, propiedad.email_residente) if c]


def enviar_recordatorios_inicio_mes():
    """Tarea programada: el día 1 de cada mes, avisa a los correos
    registrados de cada propiedad activa cuánto le toca pagar este mes y para
    cuándo. Nunca se manda a un conjunto que apagó los recordatorios, ni a una
    propiedad sin ningún correo capturado."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        hoy = dt.date.today()
        mes_nombre = mes_titulo(hoy.year, hoy.month)
        for conjunto in db.query(models.Conjunto).filter_by(recordatorios_activos=True).all():
            monto_mes = conjunto.monto_vigente_en(hoy.replace(day=1))
            for propiedad in conjunto.propiedades:
                if not propiedad.activo:
                    continue
                correos = _correos_de(propiedad)
                if not correos:
                    continue
                cuerpo = templates.get_template("correo_recordatorio_inicio_mes.html").render(
                    conjunto=conjunto, propiedad=propiedad, monto_mes=monto_mes, mes_nombre=mes_nombre,
                )
                for correo in correos:
                    enviar_correo(correo, f"Mantenimiento de {mes_nombre} - {conjunto.nombre}", cuerpo)
    finally:
        db.close()


def enviar_recordatorios_limite():
    """Tarea programada diaria: a quien todavía no haya registrado un pago
    este mes, le avisa un día antes de que la fecha límite de SU conjunto
    (cada conjunto elige la suya) se cumpla."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        hoy = dt.date.today()
        inicio_mes = hoy.replace(day=1)
        for conjunto in db.query(models.Conjunto).filter_by(recordatorios_activos=True).all():
            if conjunto.fecha_limite_pago - 1 != hoy.day:
                continue
            monto_normal = conjunto.monto_vigente_en(inicio_mes)
            for propiedad in conjunto.propiedades:
                if not propiedad.activo:
                    continue
                correos = _correos_de(propiedad)
                if not correos:
                    continue
                ya_pago = any(
                    p.fecha_recepcion >= inicio_mes and p.abona_a_cartera and not p.cancelado
                    for p in propiedad.pagos
                )
                if ya_pago:
                    continue
                cuerpo = templates.get_template("correo_recordatorio_limite.html").render(
                    conjunto=conjunto, propiedad=propiedad, monto_normal=monto_normal,
                )
                for correo in correos:
                    enviar_correo(correo, f"Mañana vence el mantenimiento - {conjunto.nombre}", cuerpo)
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


@app.post("/configuracion/monto-tardio")
def configuracion_monto_tardio(
    request: Request,
    aplica_recargo_tardio: str = Form(""),
    monto_mensual_tardio: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    """Sin historial propio (a diferencia del monto normal): el cambio se
    aplica de inmediato a toda la cartera pendiente, igual que ya pasa hoy al
    cambiar la fecha límite de pago."""
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    activo = aplica_recargo_tardio == "1"
    monto = None
    if activo and monto_mensual_tardio.strip():
        try:
            monto = round(float(monto_mensual_tardio), 2)
        except ValueError:
            monto = None
    conjunto.aplica_recargo_tardio = activo and bool(monto)
    conjunto.monto_mensual_tardio = monto
    db.commit()
    return RedirectResponse("/configuracion", status_code=302)


@app.post("/configuracion/recordatorios")
async def configuracion_recordatorios(
    request: Request,
    recordatorios_activos: str = Form(""),
    recordatorio_msg_10d: str = Form(""),
    recordatorio_msg_3d: str = Form(""),
    recordatorio_msg_dia: str = Form(""),
    recordatorio_msg_vencido: str = Form(""),
    conjunto=Depends(requerir_login),
    db: Session = Depends(get_db),
):
    if not conjunto:
        return RedirectResponse("/login", status_code=302)
    conjunto.recordatorios_activos = recordatorios_activos == "1"
    conjunto.recordatorio_msg_10d = recordatorio_msg_10d.strip() or None
    conjunto.recordatorio_msg_3d = recordatorio_msg_3d.strip() or None
    conjunto.recordatorio_msg_dia = recordatorio_msg_dia.strip() or None
    conjunto.recordatorio_msg_vencido = recordatorio_msg_vencido.strip() or None
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
scheduler.add_job(enviar_recordatorios_inicio_mes, "cron", day=1, hour=9, minute=0)
# Corre todos los días: cada conjunto elige su propia fecha límite, así que
# no hay un solo día del mes que sirva para todos.
scheduler.add_job(enviar_recordatorios_limite, "cron", hour=9, minute=30)


@app.on_event("startup")
def iniciar_scheduler():
    if not scheduler.running:
        scheduler.start()
