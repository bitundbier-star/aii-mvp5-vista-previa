"""Prueba del punto 2: el saldo inicial no debe duplicarse con el cobro
automático del mes en curso, sin importar qué día del mes se dé de alta la
cuenta."""
import datetime as dt
import re

import requests

BASE = "http://127.0.0.1:8123"


def check(nombre, cond):
    print(("OK  " if cond else "FAIL"), nombre)
    assert cond, nombre


hoy = dt.date.today()
print(f"Hoy es día {hoy.day} del mes ({hoy.isoformat()})")

# ---------------------------------------------------------------------------
# Caso A: fecha límite todavía no llega este mes (día límite = 28, muy
# probablemente después de hoy). El mes en curso NO debería contar como
# exigible todavía.
# ---------------------------------------------------------------------------
s = requests.Session()
r = s.post(f"{BASE}/registro", data={
    "nombre_conjunto": "Prueba saldo A", "direccion": "", "admin_nombre": "Ana",
    "login_email": "prueba_saldo_a@test.com", "password": "clave123",
    "cuenta_email": "cuenta_a@test.com", "monto_mensual": "800",
    "monto_revision_meses": "12", "fecha_limite_pago": "28",
    "saldo_inicial": "0", "num_propiedades": "1",
}, allow_redirects=True)
check("registro caso A", r.status_code == 200)
pid_a = int(re.search(r'id="propiedad-(\d+)"', r.text).group(1))
print(f"   -> propiedad id asignado en caso A: {pid_a}")

s.post(f"{BASE}/propiedades/{pid_a}/actualizar", data={
    "numero": "1", "tipo": "casa", "nombre_dueno": "Dueño A",
    "celular_dueno": "", "email_dueno": "", "nombre_residente": "",
    "celular_residente": "", "email_residente": "", "notas": "",
    "tipo_saldo": "cero", "monto_saldo": "0",
})
r = s.get(f"{BASE}/pagos/monto-sugerido/{pid_a}")
monto_a = r.json()["monto"]
print(f"   -> saldo con deadline=28 (todavía no vence este mes): {monto_a}")
check("caso A: nada se debe todavía (mes en curso aún no vence)", abs(monto_a) < 0.01)

# ---------------------------------------------------------------------------
# Caso B: fecha límite ya pasó este mes (día límite = 1). El admin captura un
# saldo inicial de 500 "al día de hoy" para la propiedad. El sistema NO debe
# sumarle encima la cuota de este mes (eso sería duplicar).
# ---------------------------------------------------------------------------
s2 = requests.Session()
r = s2.post(f"{BASE}/registro", data={
    "nombre_conjunto": "Prueba saldo B", "direccion": "", "admin_nombre": "Beto",
    "login_email": "prueba_saldo_b@test.com", "password": "clave123",
    "cuenta_email": "cuenta_b@test.com", "monto_mensual": "800",
    "monto_revision_meses": "12", "fecha_limite_pago": "1",
    "saldo_inicial": "0", "num_propiedades": "1",
}, allow_redirects=True)
check("registro caso B", r.status_code == 200)
pid_b = int(re.search(r'id="propiedad-(\d+)"', r.text).group(1))
print(f"   -> propiedad id asignado en caso B: {pid_b}")

r = s2.post(f"{BASE}/propiedades/{pid_b}/actualizar", data={
    "numero": "1", "tipo": "casa", "nombre_dueno": "Dueño B",
    "celular_dueno": "", "email_dueno": "", "nombre_residente": "",
    "celular_residente": "", "email_residente": "", "notas": "",
    "tipo_saldo": "debe", "monto_saldo": "500",
})
check("guardar saldo de 500 en caso B", r.status_code in (200, 302))

r = s2.get(f"{BASE}/pagos/monto-sugerido/{pid_b}")
monto_b = r.json()["monto"]
print(f"   -> saldo con deadline=1 (ya venció este mes) y saldo capturado=500: {monto_b}")
check("caso B: el saldo capturado NO se duplica con la cuota del mes en curso", abs(monto_b - 500.0) < 0.01)

print("\nTODAS LAS PRUEBAS DEL PUNTO 2 PASARON")
