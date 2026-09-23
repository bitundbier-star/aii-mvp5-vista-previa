"""Envío de correos.

En producción usa el SMTP configurado en variables de entorno (Titan, SendGrid,
etc.). Si las variables no están presentes guarda una vista previa en disco,
igual que antes.
"""
import datetime as dt
import mimetypes
import os
import smtplib
import uuid
from email.message import EmailMessage

from ..database import DATA_DIR
CORREOS_DIR = os.path.join(DATA_DIR, "correos_enviados")
os.makedirs(CORREOS_DIR, exist_ok=True)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

REMITENTE = "avisos@omnera.mx"
REPLY_TO  = ""   # vacío = no se puede responder; solo el admin puede escribir


def modo_real_configurado() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def enviar_correo(
    destinatario: str,
    asunto: str,
    cuerpo_html: str,
    adjuntos: list[str] | None = None,
) -> dict:
    """Manda un correo real o guarda una vista previa si no hay SMTP.

    Devuelve {"enviado": True/False, "detalle": str}.
    """
    adjuntos = adjuntos or []

    if not modo_real_configurado():
        return _guardar_preview(destinatario, asunto, cuerpo_html, adjuntos)

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"]    = f"Avisos AII <{REMITENTE}>"
    msg["To"]      = destinatario
    if REPLY_TO:
        msg["Reply-To"] = REPLY_TO
    msg.add_alternative(cuerpo_html, subtype="html")

    for ruta in adjuntos:
        if not ruta or not os.path.exists(ruta):
            continue
        with open(ruta, "rb") as f:
            datos = f.read()
        tipo, _ = mimetypes.guess_type(ruta)
        principal, secundario = (tipo or "application/octet-stream").split("/", 1)
        msg.add_attachment(datos, maintype=principal, subtype=secundario,
                           filename=os.path.basename(ruta))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(msg)
        return {"enviado": True, "detalle": "enviado"}
    except Exception as exc:
        # Si el SMTP falla se guarda como preview para no perder el correo
        _guardar_preview(destinatario, asunto, cuerpo_html, adjuntos)
        return {"enviado": False, "detalle": str(exc)}


def _guardar_preview(destinatario: str, asunto: str, cuerpo_html: str,
                     adjuntos: list[str]) -> dict:
    uid = uuid.uuid4().hex[:8]
    nombre = f"{dt.datetime.now():%Y%m%d_%H%M%S}_{uid}.html"
    ruta = os.path.join(CORREOS_DIR, nombre)
    with open(ruta, "w") as f:
        f.write(f"<!-- Para: {destinatario} | Asunto: {asunto} -->\n")
        if adjuntos:
            nombres = ", ".join(os.path.basename(a) for a in adjuntos if a)
            f.write(f"<!-- Adjuntos: {nombres} -->\n")
        f.write(cuerpo_html)
    return {"enviado": False, "detalle": f"preview guardado: {nombre}"}


def avisar_a_la_cuenta(conjunto, asunto: str, plantilla: str,
                        adjuntos: list[str] | None = None, **ctx):
    """Manda un aviso automático al correo de la cuenta del conjunto.

    Se llama internamente después de cada acción importante (pago registrado,
    egreso, reporte). No genera ruido hacia los vecinos: es solo para el admin.
    """
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(
        directory=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
    )
    cuerpo = templates.get_template(plantilla).render(conjunto=conjunto, **ctx)
    enviar_correo(conjunto.cuenta_email, asunto, cuerpo, adjuntos or [])
