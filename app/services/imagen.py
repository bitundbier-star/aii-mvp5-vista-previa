"""Utilidades compartidas para dibujar imágenes (comprobantes y reporte).

Todo se hace con Pillow, que ya era dependencia del proyecto: no se agrega
ninguna librería nueva ni ningún costo de despliegue.
"""
import os

from PIL import Image, ImageFont

BASE_STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"
)
LOGO_PATH = os.path.join(BASE_STATIC, "img", "logo-con-nombre.png")
LOGO_ICONO_PATH = os.path.join(BASE_STATIC, "img", "logo.png")

_CANDIDATAS = {
    True: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ],
    False: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ],
}


def fuente(tam: int, negrita: bool = False):
    """Devuelve una fuente escalable del tamaño pedido, pase lo que pase."""
    for ruta in _CANDIDATAS[bool(negrita)]:
        if os.path.exists(ruta):
            try:
                return ImageFont.truetype(ruta, tam)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=tam)
    except TypeError:  # Pillow anterior a 10.1
        return ImageFont.load_default()


def pegar_logo(img: Image.Image, x: int, y: int, alto: int = 60, con_nombre: bool = True) -> int:
    """Pega el logo de AII en (x, y) escalado a `alto` píxeles de altura.
    Devuelve el ancho que ocupó, o 0 si el archivo no está disponible (para
    que el dibujo siga funcionando aunque falte la imagen)."""
    ruta = LOGO_PATH if con_nombre else LOGO_ICONO_PATH
    if not os.path.exists(ruta):
        return 0
    try:
        logo = Image.open(ruta).convert("RGBA")
    except Exception:
        return 0
    ancho = max(int(logo.width * (alto / logo.height)), 1)
    logo = logo.resize((ancho, alto), Image.LANCZOS)
    img.paste(logo, (x, y), logo)
    return ancho


from .formato import dinero  # noqa: E402,F401
