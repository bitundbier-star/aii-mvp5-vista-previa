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
from .formato import dinero

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "comprobantes"
)
os.makedirs(STATIC_DIR, exist_ok=True)

ANCHO = 900
ALTO = 1100
MARGEN = 60


def _como_lista(pago_o_partidas) -> list[Pago]:
    if isinstance(pago_o_partidas, (list, tuple)):
        return sorted(pago_o_partidas, key=lambda p: p.id or 0)
    return [pago_o_partidas]


def _nombre_partida(p: Pago) -> str:
    if p.concepto == "proyecto" and p.proyecto:
        return f"Proyecto: {p.proyecto.concepto}"
    return CONCEPTOS_PAGO_DICT.get(p.concepto, "Otros")


def _tiene_recargo(p: Pago) -> bool:
    return bool(
        p.incluye_recargo_tardio
        and p.monto_base_snapshot
        and p.monto > p.monto_base_snapshot + 0.005
    )


def dibujar_comprobante(pago_o_partidas) -> Image.Image:
    """Dibuja el comprobante de un recibo y devuelve la imagen en memoria.

    Un recibo puede tener una sola partida (lo normal) o varias, cuando un
    mismo depósito cubrió mantenimiento, gas, un proyecto… En ese caso se
    desglosa cada concepto con su monto y al final va el total recibido.
    """
    partidas = _como_lista(pago_o_partidas)
    primera = partidas[0]
    conjunto = primera.conjunto
    propiedad = primera.propiedad
    total = round(sum(p.monto for p in partidas), 2)
    cancelado = all(p.cancelado for p in partidas)

    # Renglones del desglose: una partida con recargo ocupa dos renglones.
    renglones = []
    for p in partidas:
        if _tiene_recargo(p):
            renglones.append((_nombre_partida(p), p.monto_base_snapshot))
            renglones.append(("Monto por pago posterior al día límite", round(p.monto - p.monto_base_snapshot, 2)))
        else:
            renglones.append((_nombre_partida(p), p.monto))
    con_desglose = len(renglones) > 1

    alto = ALTO + (max(len(renglones) - 2, 0) * 40 if con_desglose else 0)
    img = Image.new("RGB", (ANCHO, alto), color="#ffffff")
    draw = ImageDraw.Draw(img)

    f_titulo = fuente(34, negrita=True)
    f_folio = fuente(20)
    f_label = fuente(20, negrita=True)
    f_valor = fuente(24)
    f_monto = fuente(56, negrita=True)
    f_pie = fuente(16)

    y = MARGEN
    pegar_logo(img, MARGEN, y, alto=56)
    y += 84

    draw.text((MARGEN, y), "Comprobante de pago", font=f_titulo, fill="#1a1a1a")
    if cancelado:
        draw.text((ANCHO - MARGEN, y + 6), "CANCELADO", font=f_label, fill="#b3261e", anchor="ra")
    y += 50
    draw.text((MARGEN, y), f"Folio: {primera.folio_recibo}", font=f_folio, fill="#555555")
    y += 50
    draw.line((MARGEN, y, ANCHO - MARGEN, y), fill="#dddddd", width=2)
    y += 40

    campos = [
        ("Conjunto", conjunto.nombre),
        ("Administrador en turno", conjunto.admin_nombre),
        ("Propiedad", propiedad.etiqueta),
        ("Fecha de recepción", primera.fecha_recepcion.strftime("%d/%m/%Y")),
    ]
    if not con_desglose:
        campos.append(("Concepto", CONCEPTOS_PAGO_DICT.get(primera.concepto, "Otros")))
        if primera.concepto == "proyecto" and primera.proyecto:
            campos.append(("Proyecto", primera.proyecto.concepto))
    campos.append(("Método de pago", primera.metodo_pago_legible))

    for label, valor in campos:
        draw.text((MARGEN, y), label, font=f_label, fill="#777777")
        y += 28
        draw.text((MARGEN, y), str(valor), font=f_valor, fill="#1a1a1a")
        y += 44

    y += 20
    draw.line((MARGEN, y, ANCHO - MARGEN, y), fill="#dddddd", width=2)
    y += 40

    # Desglose: nadie debe adivinar por qué el total es el que es, ni qué
    # parte de su depósito se fue a cada concepto.
    if con_desglose:
        draw.text((MARGEN, y), "Desglose", font=f_label, fill="#777777")
        y += 38
        for nombre, monto in renglones:
            draw.text((MARGEN, y), nombre[:52], font=f_valor, fill="#1a1a1a")
            draw.text((ANCHO - MARGEN, y), dinero(monto), font=f_valor, fill="#1a1a1a", anchor="ra")
            y += 40
        draw.line((MARGEN, y, ANCHO - MARGEN, y), fill="#dddddd", width=1)
        y += 24

    draw.text((MARGEN, y), "Total recibido" if con_desglose else "Monto recibido", font=f_label, fill="#777777")
    y += 32
    draw.text((MARGEN, y), f"{dinero(total)} MXN", font=f_monto, fill="#b3261e" if cancelado else "#0a7d3c")

    y = alto - 80
    draw.line((MARGEN, y, ANCHO - MARGEN, y), fill="#dddddd", width=1)
    y += 15
    draw.text(
        (MARGEN, y),
        "Generado automáticamente por la plataforma de administración del conjunto.",
        font=f_pie,
        fill="#999999",
    )

    return img


def comprobante_png(pago_o_partidas) -> bytes:
    """El comprobante como bytes PNG, para servirlo por HTTP."""
    buffer = io.BytesIO()
    dibujar_comprobante(pago_o_partidas).save(buffer, format="PNG")
    return buffer.getvalue()


def comprobante_pdf(pago_o_partidas) -> bytes:
    """El mismo comprobante, en PDF (para descargarlo o mandarlo desde la
    computadora). Es la misma imagen metida en una hoja PDF: Pillow lo hace
    solo, sin librerías nuevas, y así el PDF y la imagen nunca difieren."""
    buffer = io.BytesIO()
    dibujar_comprobante(pago_o_partidas).save(buffer, format="PDF", resolution=110.0)
    return buffer.getvalue()


def _folio_de(pago_o_partidas) -> str:
    return _como_lista(pago_o_partidas)[0].folio_recibo


def comprobante_para_adjuntar(pago_o_partidas) -> str:
    """Escribe el comprobante (PNG) en un archivo temporal y devuelve su ruta.

    Solo para adjuntarlo al correo, que necesita un archivo en disco. Vive en
    la carpeta temporal del sistema, no entre los archivos de la aplicación:
    es de usar y tirar.
    """
    ruta = os.path.join(tempfile.gettempdir(), f"{_folio_de(pago_o_partidas)}.png")
    dibujar_comprobante(pago_o_partidas).save(ruta)
    return ruta


def comprobante_pdf_para_adjuntar(pago_o_partidas) -> str:
    ruta = os.path.join(tempfile.gettempdir(), f"{_folio_de(pago_o_partidas)}.pdf")
    with open(ruta, "wb") as f:
        f.write(comprobante_pdf(pago_o_partidas))
    return ruta


def generar_comprobante(pago) -> str:
    """Compatibilidad con el código que aún espera una ruta de archivo."""
    return comprobante_para_adjuntar(pago)
