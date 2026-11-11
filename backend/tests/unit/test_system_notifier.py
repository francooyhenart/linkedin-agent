"""Notificaciones del sistema: seguridad (inyección de comandos), no bloquear, timeout y fallos.

La parte que más importa: el texto de un post NUNCA se ejecuta. Se prueba de dos formas:
  - estructural: el texto no aparece en el script ni en la lista de argumentos (viaja por variables de entorno);
  - real (solo Windows): se corre el PowerShell verdadero con un post hostil y se comprueba que el texto sale
    LITERAL en el aviso y que no se creó ningún archivo "canario" (lo que habría hecho un comando inyectado).
"""
import logging
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

import pytest

from app.shared.notifications import system
from app.shared.notifications.system import (
    Command,
    SystemNotifier,
    build_command,
    linux_command,
    macos_command,
    run_command,
    windows_command,
)

# Cada texto trae algo que un shell o PowerShell/AppleScript ejecutaría si se interpolara.
HOSTILE = [
    "'; New-Item canary_a.txt; '",
    '"; New-Item canary_b.txt; "',
    "$(New-Item -Path canary_c.txt)",
    "`New-Item canary_d.txt`",
    "texto\nsegunda línea; New-Item canary_e.txt",
    "-- --help $env:USERNAME %PATH% ${HOME} $(whoami) | calc & calc",
    "<b>negrita</b> & <script>alert(1)</script> ]]> <!-- x -->",
    'tell application "Finder" to quit\r\n"} & do shell script "touch canary_f"',
    "ñandú áéíóú 🙂 日本語",
]
POWERSHELL = "powershell.exe"


# ---------------------------------------------------------------- estructura: el texto no viaja dentro del script
@pytest.mark.parametrize("payload", HOSTILE)
def test_windows_el_texto_va_por_variables_de_entorno_no_dentro_del_script(payload):
    command = windows_command(payload, payload, "http://127.0.0.1:8000/#calendario", powershell=POWERSHELL)

    assert isinstance(command.argv, list) and all(isinstance(a, str) for a in command.argv)
    assert command.env["LINKEDIN_AGENT_TITLE"] == payload and command.env["LINKEDIN_AGENT_BODY"] == payload
    assert payload not in " ".join(command.argv)  # ni crudo...
    import base64

    script = base64.b64decode(command.argv[-1]).decode("utf-16-le")
    assert payload not in script  # ...ni dentro del script codificado


def test_windows_el_script_es_identico_para_cualquier_texto():
    first = windows_command("hola", "mundo", None, powershell=POWERSHELL)
    for payload in HOSTILE:
        other = windows_command(payload, payload, "http://127.0.0.1:8000/#calendario", powershell=POWERSHELL)
        assert other.argv == first.argv  # mismo ejecutable, mismas opciones, mismo script


@pytest.mark.parametrize("payload", HOSTILE)
def test_macos_el_texto_va_por_variables_de_entorno_no_dentro_del_script(payload):
    command = macos_command(payload, payload, osascript="osascript")
    assert command.env == {"LINKEDIN_AGENT_TITLE": payload, "LINKEDIN_AGENT_BODY": payload}
    assert all(payload not in arg for arg in command.argv)
    assert command.argv == macos_command("x", "y", osascript="osascript").argv


@pytest.mark.parametrize("payload", HOSTILE)
def test_linux_el_texto_es_un_argumento_suelto_despues_de_guion_guion(payload):
    command = linux_command(payload, payload, notify_send="notify-send")
    separator = command.argv.index("--")
    assert command.argv[:separator][0] == "notify-send"
    assert all(payload not in arg for arg in command.argv[:separator])  # nada del texto antes del "--"
    title, body = command.argv[separator + 1 :]
    assert title == body  # mismo texto escapado en ambos
    assert "<" not in body and ">" not in body  # el marcado se escapa: se muestra literal


def test_linux_escapa_el_marcado():
    command = linux_command("a & b", "<b>x</b>", notify_send="notify-send")
    assert command.argv[-2:] == ["a &amp; b", "&lt;b&gt;x&lt;/b&gt;"]


def test_el_caracter_nulo_se_saca_porque_los_procesos_no_lo_aceptan():
    assert windows_command("a\x00b", "c\x00d", None, powershell=POWERSHELL).env["LINKEDIN_AGENT_TITLE"] == "ab"
    assert linux_command("a\x00b", "c", notify_send="notify-send").argv[-2] == "ab"


def test_windows_solo_acepta_urls_web_para_abrir_al_tocar():
    ok = windows_command("t", "b", "http://127.0.0.1:8000/#calendario", powershell=POWERSHELL)
    assert ok.env["LINKEDIN_AGENT_URL"] == "http://127.0.0.1:8000/#calendario"
    for bad in ("file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", "calc.exe", "", None):
        assert "LINKEDIN_AGENT_URL" not in windows_command("t", "b", bad, powershell=POWERSHELL).env


def test_sistema_no_soportado_o_sin_el_programa_devuelve_none(monkeypatch):
    assert build_command("t", "b", system="Plan9") is None
    monkeypatch.setattr(system.shutil, "which", lambda name: None)
    assert linux_command("t", "b") is None
    assert macos_command("t", "b") is None


# ---------------------------------------------------------------- ejecución real en Windows con texto hostil
@pytest.mark.skipif(sys.platform != "win32", reason="usa el PowerShell de Windows")
@pytest.mark.parametrize("payload", HOSTILE)
def test_windows_real_un_post_hostil_se_muestra_literal_y_no_ejecuta_nada(payload, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # si algo se ejecutara, el archivo canario aparecería acá
    command = windows_command(payload, payload + " (cuerpo)", "http://127.0.0.1:8000/#calendario")
    assert command is not None, "no se encontró Windows PowerShell"
    dry = Command(command.argv, {**command.env, "LINKEDIN_AGENT_DRY_RUN": "1"})  # imprime el aviso en vez de mostrarlo

    result = run_command(dry, timeout=60)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    toast = ET.fromstring(result.stdout.decode("utf-8-sig"))
    title, body = [node.text or "" for node in toast.iter("text")]
    normalize = lambda s: s.replace("\r\n", "\n")  # noqa: E731 - el XML normaliza los saltos de línea
    assert normalize(title) == normalize(payload)
    assert normalize(body) == normalize(payload + " (cuerpo)")
    assert toast.get("launch") == "http://127.0.0.1:8000/#calendario"
    assert list(tmp_path.iterdir()) == []  # ningún comando inyectado creó archivos


# ---------------------------------------------------------------- cómo se lanza el proceso
def test_el_proceso_se_lanza_con_lista_sin_shell_y_con_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    run_command(Command(["programa", "arg"], {"LINKEDIN_AGENT_BODY": "x"}), timeout=7)

    assert seen["argv"] == ["programa", "arg"]
    assert not seen["kwargs"].get("shell")  # nunca shell=True
    assert seen["kwargs"]["timeout"] == 7
    assert seen["kwargs"]["env"]["LINKEDIN_AGENT_BODY"] == "x"


def test_un_proceso_colgado_se_corta_por_timeout():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(Command([sys.executable, "-c", "import time; time.sleep(60)"]), timeout=1)
    assert time.monotonic() - started < 15


# ---------------------------------------------------------------- no bloquea y no rompe
def test_notify_vuelve_enseguida_aunque_el_aviso_tarde():
    release = threading.Event()
    started = threading.Event()

    def slow_runner(command, timeout):
        started.set()
        release.wait(timeout=30)
        return subprocess.CompletedProcess(command.argv, 0, b"", b"")

    notifier = SystemNotifier(builder=lambda *a: Command(["x"]), runner=slow_runner)
    before = time.monotonic()
    notifier.notify("título", "cuerpo")
    returned_after = time.monotonic() - before

    assert returned_after < 1  # no esperó al runner
    assert started.wait(timeout=5)  # pero el aviso sí se está procesando, en otro hilo
    assert notifier.last_thread.is_alive()
    release.set()
    notifier.last_thread.join(timeout=5)
    assert not notifier.last_thread.is_alive()


@pytest.mark.parametrize(
    "boom",
    [
        subprocess.TimeoutExpired(cmd="x", timeout=1),
        FileNotFoundError("no está el programa"),
        PermissionError("sin permiso"),
        RuntimeError("cualquier cosa"),
    ],
)
def test_si_el_aviso_falla_se_registra_un_aviso_y_no_se_lanza_nada(boom, caplog):
    def runner(command, timeout):
        raise boom

    notifier = SystemNotifier(builder=lambda *a: Command(["x"]), runner=runner)
    with caplog.at_level(logging.WARNING, logger=system.logger.name):
        notifier.deliver("título privado", "cuerpo privado")  # no debe lanzar

    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "privado" not in caplog.text  # el texto del post no se escribe en los logs


def test_si_el_programa_termina_con_error_se_registra_un_aviso(caplog):
    runner = lambda command, timeout: subprocess.CompletedProcess(command.argv, 1, b"", b"sin servidor de notificaciones")  # noqa: E731
    notifier = SystemNotifier(builder=lambda *a: Command(["x"]), runner=runner)
    with caplog.at_level(logging.WARNING, logger=system.logger.name):
        notifier.deliver("t", "b")
    assert "código 1" in caplog.text and "sin servidor de notificaciones" in caplog.text


def test_sistema_no_soportado_avisa_una_sola_vez(caplog):
    notifier = SystemNotifier(builder=lambda *a: None, runner=lambda *a: pytest.fail("no debería correr nada"))
    with caplog.at_level(logging.WARNING, logger=system.logger.name):
        notifier.deliver("t", "b")
        notifier.deliver("t", "b")
    assert len(caplog.records) == 1
