"""Cómo se muestra el dinero en toda la aplicación.

Regla única, y vive aquí para que no se contradiga de una pantalla a otra:

**Nunca se muestra un signo negativo.** Un "−$500.00" junto a la palabra
"a favor" se lee al revés — parece que le están quitando dinero al vecino. La
cifra sale siempre en positivo, y lo que dice de qué se trata es la palabra y
el color: verde el saldo a favor, rojo el adeudo.

Por dentro la convención sigue siendo la de siempre (positivo = debe,
negativo = a favor), porque es lo que hace que las sumas cuadren. Lo que cambia
es que el administrador ya no la ve.
"""


def dinero(valor: float) -> str:
    """Cantidad con formato de moneda, siempre en positivo y sin signo."""
    try:
        v = float(valor or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"${abs(v):,.2f}"


def dinero_con_signo(valor: float) -> str:
    """Solo para el estado de flujo de caja, donde un movimiento negativo sí
    significa salida de dinero y el signo es información, no confusión."""
    try:
        v = float(valor or 0)
    except (TypeError, ValueError):
        v = 0.0
    return ("-" if v < 0 else "") + f"${abs(v):,.2f}"


def estado_saldo(saldo: float) -> dict:
    """Cómo se presenta un saldo de propiedad.

    Devuelve la cifra ya formateada, la palabra que la acompaña y la clase de
    color. Cero y saldo a favor cuentan los dos como al corriente: solo se
    considera adeudo un saldo positivo.
    """
    try:
        v = float(saldo or 0)
    except (TypeError, ValueError):
        v = 0.0

    if v > 0.005:
        clave, palabra, clase = "debe", "Con adeudo", "saldo-debe"
    elif v < -0.005:
        clave, palabra, clase = "favor", "Saldo a favor", "saldo-favor"
    else:
        clave, palabra, clase = "cero", "Al corriente", "saldo-cero"

    return {
        "clave": clave,
        "palabra": palabra,
        "clase": clase,
        "monto": dinero(v),
        "al_corriente": clave != "debe",
        "crudo": round(v, 2),
    }


MESES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre",
    12: "diciembre",
}


def mes_largo(anio: int, mes: int) -> str:
    return f"{MESES.get(mes, '')} de {anio}"


def mes_titulo(anio: int, mes: int) -> str:
    nombre = MESES.get(mes, "")
    return f"{nombre.capitalize()} {anio}"
