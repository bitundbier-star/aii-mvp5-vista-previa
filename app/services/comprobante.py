"""Generación del comprobante de pago como imagen (PNG), con folio único.

El comprobante **no se guarda**: se vuelve a dibujar cada vez que alguien lo
pide. Todo lo que hace falta para trazarlo —folio, propiedad, monto, fecha,
concepto— ya vive en la base de datos, así que el archivo era una copia
redundante que además se perdía: el disco del servidor es temporal, y en cada
despliegue los comprobantes guardados desaparecían dejando los enlaces rotos.

Dibujarlo al vuelo cuesta milisegundos, no ocupa espacio y el folio siempre
produce exactamente la misma imagen.
"""
import io
import os
import tempfile

from PIL import Image, ImageDraw

from ..models import Pago, CONCEPTOS_PAGO_DICT
from .imagen import fuente, pegar_logo

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "comprobantes"
)
os.makedirs(STATIC_DIR, exist_ok=True)

ANCHO = 900
ALTO = 1100
MARGEN = 60


def dibujar_comprobante(pago: Pago) -> Image.Image:
    """Dibuja el comprobante de un pago y devuelve la imagen en memoria."""
    conjunto = pago.conjunto
    propiedad = pago.propiedad

    img = Image.new("RGB", (ANCHO, ALTO), color="#ffffff")
    draw = ImageDraw.Draw(img)

    f_titulo = fuente(34, negrita=True)
    f_folio = fuente(20)
    f_label = fuente(20, negrita=True)
    f_valor = fuente(24)
    f_monto = fuente(56, negrita=True)
    f_pie = fuente(16)

    y = MARGEN
    pegar_logo(img, MARGEN, y, alto=48)
    y += 72

    draw.text((MARGEN, y), "Comprobante de pago", font=f_titulo, fill="#1a1a1a")
    y += 50
    draw.text((MARGEN, y), f"Folio: {pago.folio}", font=f_folio, fill="#555555")
    y += 50
    draw.line((MARGEN, y, ANCHO - MARGEN, y), fill="#dddddd", width=2)
    y += 40

    campos = [
        ("Conjunto", conjunto.nombre),
        ("Administrador en turno", conjunto.admin_nombre),
        ("Propiedad", propiedad.etiqueta),
        ("Fecha de recepción", pago.fecha_recepcion.strftime("%d/%m/%Y")),
        ("Concepto", CONCEPTOS_PAGO_DICT.get(pago.concepto, "Otros")),
    ]
    if pago.concepto == "proyecto" and pago.proyecto:
        campos.append(("Proyecto", pago.proyecto.concepto))
    campos.append(("Método de pago", pago.metodo_pago_legible))

    for label, valor in campos:
        draw.text((MARGEN, y), label, font=f_label, fill="#777777")
        y += 28
        draw.text((MARGEN, y), str(valor), font=f_valor, fill="#1a1a1a")
        y += 44

    y += 20
    draw.line((MARGEN, y, ANCHO - MARGEN, y), fill="#dddddd", width=2)
    y += 40
    draw.text((MARGEN, y), "Monto recibido", font=f_label, fill="#777777")
    y += 32
    draw.text((MARGEN, y), f"${pago.monto:,.2f} MXN", font=f_monto, fill="#0a7d3c")

    y = ALTO - 80
    draw.line((MARGEN, y, ANCHO - MARGEN, y), fill="#dddddd", width=1)
    y += 15
    draw.text(
        (MARGEN, y),
        "Generado automáticamente por la plataforma de administración del conjunto.",
        font=f_pie,
        fill="#999999",
    )

    return img


def comprobante_png(pago: Pago) -> bytes:
    """El comprobante como bytes PNG, para servirlo por HTTP."""
    buffer = io.BytesIO()
    dibujar_comprobante(pago).save(buffer, format="PNG")
    return buffer.getvalue()


def comprobante_para_adjuntar(pago: Pago) -> str:
    """Escribe el comprobante en un archivo temporal y devuelve su ruta.

    Solo para adjuntarlo al correo, que necesita un archivo en disco. Vive en
    la carpeta temporal del sistema, no entre los archivos de la aplicación:
    es de usar y tirar.
    """
    ruta = os.path.join(tempfile.gettempdir(), f"{pago.folio}.png")
    dibujar_comprobante(pago).save(ruta)
    return ruta


def generar_comprobante(pago: Pago) -> str:
    """Compatibilidad con el código que aún espera una ruta de archivo."""
    return comprobante_para_adjuntar(pago)
