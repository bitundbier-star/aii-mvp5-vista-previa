"""Suscripción de la cuenta a AII, cobrada vía Stripe.

Nota de alcance (acordado con Sofía): en este MVP todavía no hay cuenta de
Stripe creada, así que esta sección queda como placeholder — igual que el
correo (services/email.py) antes de configurar SMTP, aquí no se falla ni se
inventa un cobro: se explica qué falta y se deja el botón listo para
cuando existan las llaves.

Cuando haya cuenta de Stripe, el flujo recomendado es el Portal de
Clientes de Stripe (Customer Portal): un botón que redirige a una página
alojada por Stripe donde el administrador captura/actualiza su tarjeta.
Stripe se encarga de todo el manejo de datos de tarjeta — AII nunca los
toca directamente, lo cual también simplifica el cumplimiento de PCI.
"""
import os

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")


def stripe_configurado() -> bool:
    return bool(STRIPE_SECRET_KEY)


def iniciar_actualizacion_metodo_pago(conjunto) -> dict:
    """Punto de entrada para 'Actualizar método de pago'. Devuelve un dict
    {"listo": bool, "url": str|None, "detalle": str} — cuando Stripe esté
    configurado, "url" será el link al Portal de Clientes al que se debe
    redirigir al administrador."""
    if not stripe_configurado():
        return {
            "listo": False,
            "url": None,
            "detalle": (
                "Stripe no está configurado todavía: agrega STRIPE_SECRET_KEY "
                "(y STRIPE_PRICE_ID si vas a cobrar una suscripción con un "
                "precio fijo) en tu .env — ver README — para conectar aquí el "
                "Portal de Clientes de Stripe."
            ),
        }

    return {
        "listo": False,
        "url": None,
        "detalle": (
            "Ya detecté llaves de Stripe configuradas, pero la conexión con "
            "el Portal de Clientes todavía no está implementada en este MVP."
        ),
    }
