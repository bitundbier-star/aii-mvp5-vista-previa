"""Recordatorios automáticos de pago.

Se llama desde un endpoint /recordatorios/ejecutar que el cron job de Render
dispara cada día a las 9:00 AM hora de la Ciudad de México (15:00 UTC).

Por cada conjunto con recordatorios activos, revisa qué propiedades no han
pagado el mes en curso y manda el correo que corresponda según la distancia
a la fecha límite.
"""
import datetime as dt
import os

from .email import enviar_correo, modo_real_configurado

CDMX_OFFSET = dt.timezone(dt.timedelta(hours=-6))

MSG_DEFAULT_10D = (
    "Hola {nombre}, te recordamos que tu pago de mantenimiento de {conjunto} "
    "vence el {fecha_limite}. Puedes realizar tu depósito con anticipación."
)
MSG_DEFAULT_3D = (
    "Hola {nombre}, tu pago de mantenimiento vence en 3 días ({fecha_limite}). "
    "No olvides realizarlo a tiempo."
)
MSG_DEFAULT_DIA = (
    "Hola {nombre}, hoy es el último día para realizar tu pago de mantenimiento "
    "de {conjunto}. Evita inconvenientes pagando hoy."
)
MSG_DEFAULT_VENCIDO = (
    "Hola {nombre}, tu pago de mantenimiento de {conjunto} venció ayer. "
    "Por favor realiza tu pago a la brevedad o comunícate con tu administrador."
)


def _plantilla(conjunto, campo: str, default: str) -> str:
    return getattr(conjunto, campo, None) or default


def _propiedades_sin_pagar(conjunto, mes_inicio: dt.date) -> list:
    """Propiedades activas que no tienen ningún pago de mantenimiento
    no cancelado en el mes en curso."""
    pagadas = {
        p.propiedad_id
        for p in conjunto.pagos
        if not p.cancelado
        and p.concepto == "mantenimiento"
        and p.fecha_recepcion >= mes_inicio
    }
    return [
        prop for prop in conjunto.propiedades
        if prop.activo and prop.id not in pagadas
    ]


def _correo_propiedad(prop) -> str | None:
    return prop.email_dueno or prop.email_residente or None


def _cuerpo_html(mensaje: str) -> str:
    return f"""
<div style="font-family:Arial,sans-serif; max-width:480px; margin:0 auto; color:#333;">
  <p style="font-size:15px; line-height:1.6;">{mensaje}</p>
  <p style="margin-top:20px; color:#888; font-size:12px;">
    Este es un aviso automático. No respondas a este correo.
    Para consultas, comunícate directamente con el administrador de tu conjunto.
  </p>
</div>"""


def ejecutar_recordatorios(db) -> dict:
    """Revisa todos los conjuntos y manda los recordatorios que correspondan hoy."""
    from ..models import Conjunto

    hoy = dt.datetime.now(CDMX_OFFSET).date()
    mes_inicio = hoy.replace(day=1)
    enviados = 0
    omitidos = 0

    for conjunto in db.query(Conjunto).filter_by(recordatorios_activos=True).all():
        if not conjunto.fecha_limite_pago:
            continue

        fecha_limite = hoy.replace(day=conjunto.fecha_limite_pago)
        # Si la fecha límite ya pasó este mes, usar el mes que viene para el cálculo
        if fecha_limite < mes_inicio:
            continue

        dias_restantes = (fecha_limite - hoy).days

        if dias_restantes == 10:
            campo, default = "recordatorio_msg_10d", MSG_DEFAULT_10D
            asunto_base = "Recordatorio: 10 días para tu pago"
        elif dias_restantes == 3:
            campo, default = "recordatorio_msg_3d", MSG_DEFAULT_3D
            asunto_base = "Recordatorio: 3 días para tu pago"
        elif dias_restantes == 0:
            campo, default = "recordatorio_msg_dia", MSG_DEFAULT_DIA
            asunto_base = "Hoy vence tu pago de mantenimiento"
        elif dias_restantes == -1:
            campo, default = "recordatorio_msg_vencido", MSG_DEFAULT_VENCIDO
            asunto_base = "Tu pago de mantenimiento venció ayer"
        else:
            continue

        plantilla_msg = _plantilla(conjunto, campo, default)
        sin_pagar = _propiedades_sin_pagar(conjunto, mes_inicio)

        for prop in sin_pagar:
            correo = _correo_propiedad(prop)
            if not correo:
                omitidos += 1
                continue
            nombre = prop.nombre_dueno or prop.nombre_residente or prop.etiqueta
            mensaje = plantilla_msg.format(
                nombre=nombre,
                conjunto=conjunto.nombre,
                fecha_limite=fecha_limite.strftime("%d/%m/%Y"),
            )
            resultado = enviar_correo(
                correo,
                f"{asunto_base} — {conjunto.nombre}",
                _cuerpo_html(mensaje),
            )
            if resultado["enviado"]:
                enviados += 1
            else:
                omitidos += 1

    return {"enviados": enviados, "omitidos": omitidos, "fecha": hoy.isoformat()}
