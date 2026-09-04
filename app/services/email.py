"""Envío de correo. Si no hay credenciales SMTP configuradas (.env), el
correo no se envía de verdad: se guarda en data/correos_enviados/ como
vista previa, para poder probar el flujo completo sin depender de un
proveedor de correo real. Ver README para configurar un proveedor real
(Gmail con contraseña de aplicación, Resend, SendGrid, etc.)."""
import os
import smtplib
import uuid
import datetime as dt
from email.message import EmailMessage

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREVIEW_DIR = os.path.join(BASE_DIR, "data", "correos_enviados")
os.makedirs(PREVIEW_DIR, exist_ok=True)

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "no-responder@example.com")


def modo_real_configurado() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def enviar_correo(destinatario: str, asunto: str, cuerpo_html: str, adjuntos: list[str] | None = None) -> dict:
    """Envía un correo. Devuelve un dict con {"enviado": bool, "modo": "real"|"simulado", "detalle": str}."""
    adjuntos = adjuntos or []

    if not modo_real_configurado():
        marca = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        sufijo = uuid.uuid4().hex[:6]
        nombre = f"{marca}_{sufijo}_{destinatario.replace('@', '_at_')}.html"
        ruta = os.path.join(PREVIEW_DIR, nombre)
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(f"<!-- Para: {destinatario} | Asunto: {asunto} -->\n")
            f.write(cuerpo_html)
        return {
            "enviado": False,
            "modo": "simulado",
            "detalle": (
                f"SMTP no configurado: el correo se guardó como vista previa en "
                f"data/correos_enviados/{nombre} en lugar de enviarse de verdad."
            ),
        }

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = SMTP_FROM
    msg["To"] = destinatario
    msg.set_content("Este correo requiere un cliente compatible con HTML.")
    msg.add_alternative(cuerpo_html, subtype="html")

    for ruta_adjunto in adjuntos:
        if os.path.exists(ruta_adjunto):
            with open(ruta_adjunto, "rb") as f:
                datos = f.read()
            msg.add_attachment(
                datos, maintype="image", subtype="png", filename=os.path.basename(ruta_adjunto)
            )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return {"enviado": True, "modo": "real", "detalle": f"Correo enviado a {destinatario}."}
    except Exception as exc:  # pragma: no cover - depende de red/credenciales
        return {"enviado": False, "modo": "error", "detalle": f"No se pudo enviar: {exc}"}
