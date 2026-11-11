"""Notificaciones del sistema operativo (Windows, macOS, Linux) sin dependencias de Python.

Cada sistema trae su propia forma de mostrar una notificación, y la usamos llamando a un programa que ya viene
instalado: PowerShell (Windows), osascript (macOS) o notify-send (Linux).

REGLA DE SEGURIDAD (la más importante de este archivo): el texto que se muestra NUNCA se pega dentro de un script.
Si el texto de un post se interpolara en el código de PowerShell o de AppleScript, un post con comillas o con
`$(...)` podría EJECUTAR comandos en la compu (inyección de comandos). Por eso:
  - Cada script es una constante: es exactamente el mismo texto sea cual sea el post.
  - El título, el cuerpo y la URL viajan por VARIABLES DE ENTORNO del proceso hijo, aparte del script. El script
    las lee como datos y las asigna a un campo de texto: nunca las evalúa.
  - Los procesos se lanzan con una LISTA de argumentos, sin shell (nunca `shell=True`).
  - En Linux no hay script: el texto va como argumento suelto, después de `--` para que no se lea como opción.

Mostrar una notificación nunca puede romper la app: se hace en un hilo aparte (el scheduler no espera), con un
tiempo máximo, y cualquier error se registra como aviso y se sigue.
"""
import base64
import logging
import os
import platform
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15


class Notifier(Protocol):
    """Lo que el resto de la app necesita: mostrar un aviso. Los tests usan un fake que solo anota las llamadas."""

    def notify(self, title: str, body: str, url: str | None = None) -> None: ...


@dataclass(frozen=True)
class Command:
    """Un proceso para lanzar: lista de argumentos (sin shell) y las variables de entorno que se le agregan."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- Windows
# Script CONSTANTE. Arma el aviso en un documento XML asignando el texto con InnerText (el DOM lo escapa solo),
# así que el texto del usuario nunca se interpreta ni como código ni como XML.
# Si POSTLINE_DRY_RUN está definida, imprime el XML en vez de mostrar el aviso (lo usan los tests).
_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).InnerText = $env:POSTLINE_TITLE
$texts.Item(1).InnerText = $env:POSTLINE_BODY
if ($env:POSTLINE_URL) {
    $xml.DocumentElement.SetAttribute('activationType', 'protocol')
    $xml.DocumentElement.SetAttribute('launch', $env:POSTLINE_URL)
}
if ($env:POSTLINE_DRY_RUN) { Write-Output $xml.GetXml(); exit 0 }
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""

# ---------------------------------------------------------------- macOS (JavaScript for Automation, también constante)
_MACOS_SCRIPT = """
ObjC.import('Foundation');
const env = $.NSProcessInfo.processInfo.environment;
const read = (key) => { const value = env.objectForKey(key); return value ? ObjC.unwrap(value) : ''; };
const app = Application.currentApplication();
app.includeStandardAdditions = true;
app.displayNotification(read('POSTLINE_BODY'), { withTitle: read('POSTLINE_TITLE') });
"""


def _clean(value: str) -> str:
    """Los procesos no aceptan el carácter nulo en argumentos ni en variables de entorno."""
    return value.replace("\x00", "")


def _http_url_or_none(url: str | None) -> str | None:
    """Solo se acepta una URL web: el aviso de Windows la abre al tocarla."""
    if url and urlparse(url).scheme in ("http", "https"):
        return _clean(url)
    return None


def _powershell_path() -> str | None:
    """Windows PowerShell 5.1 (no pwsh): es el que trae las clases de notificaciones de Windows."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    bundled = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return bundled if os.path.exists(bundled) else shutil.which("powershell.exe")


def windows_command(title: str, body: str, url: str | None, powershell: str | None = None) -> Command | None:
    exe = powershell or _powershell_path()
    if exe is None:
        return None
    encoded = base64.b64encode(_WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")  # formato de -EncodedCommand
    env = {"POSTLINE_TITLE": _clean(title), "POSTLINE_BODY": _clean(body)}
    if safe_url := _http_url_or_none(url):
        env["POSTLINE_URL"] = safe_url
    argv = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    return Command(argv, env)


def macos_command(title: str, body: str, osascript: str | None = None) -> Command | None:
    exe = osascript or shutil.which("osascript")
    if exe is None:
        return None
    return Command(
        [exe, "-l", "JavaScript", "-e", _MACOS_SCRIPT],
        {"POSTLINE_TITLE": _clean(title), "POSTLINE_BODY": _clean(body)},
    )


def _escape_markup(text: str) -> str:
    """Muchos servidores de notificaciones de Linux leen un poco de HTML (<b>, <a>) en el cuerpo: se escapa para
    que el texto se vea literal."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def linux_command(title: str, body: str, notify_send: str | None = None) -> Command | None:
    exe = notify_send or shutil.which("notify-send")
    if exe is None:
        return None
    # Sin script y sin shell: título y cuerpo son argumentos sueltos, después de "--" (así un texto que empieza
    # con "-" no se confunde con una opción).
    argv = [exe, "--app-name=Postline", "--expire-time=10000", "--", _escape_markup(_clean(title)), _escape_markup(_clean(body))]
    return Command(argv)


def build_command(title: str, body: str, url: str | None = None, system: str | None = None) -> Command | None:
    """El comando del sistema actual, o None si este sistema no está soportado o falta el programa que hace falta."""
    system = system or platform.system()
    if system == "Windows":
        return windows_command(title, body, url)
    if system == "Darwin":
        return macos_command(title, body)
    if system == "Linux":
        return linux_command(title, body)
    return None


# ---------------------------------------------------------------- ejecución
def run_command(command: Command, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Lanza el proceso (lista de argumentos, sin shell) y espera como máximo `timeout` segundos.
    Si se pasa, subprocess lo mata y lanza TimeoutExpired."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # que no parpadee una consola
    return subprocess.run(  # noqa: S603 - lista de argumentos, sin shell
        command.argv,
        env={**os.environ, **command.env},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
        **kwargs,
    )


class SystemNotifier:
    """Muestra avisos del sistema operativo. `notify` vuelve enseguida: el trabajo real corre en un hilo aparte."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        builder: Callable[..., Command | None] = build_command,
        runner: Callable[..., subprocess.CompletedProcess] = run_command,
    ):
        self.timeout = timeout
        self._build = builder
        self._run = runner
        self._warned_unsupported = False
        self.last_thread: threading.Thread | None = None  # solo para que los tests puedan esperar al hilo

    def notify(self, title: str, body: str, url: str | None = None) -> None:
        thread = threading.Thread(target=self.deliver, args=(title, body, url), name="system-notification", daemon=True)
        self.last_thread = thread
        thread.start()

    def deliver(self, title: str, body: str, url: str | None = None) -> None:
        """Muestra el aviso y espera a que termine (con tiempo máximo). NUNCA lanza.

        Los logs no incluyen el título ni el cuerpo: son texto privado (el comienzo de un post)."""
        try:
            command = self._build(title, body, url)
            if command is None:
                if not self._warned_unsupported:
                    self._warned_unsupported = True
                    logger.warning("No puedo mostrar notificaciones del sistema acá (sistema no soportado o falta el programa que las muestra)")
                return
            result = self._run(command, self.timeout)
            if result.returncode != 0:
                logger.warning(
                    "La notificación del sistema falló (código %s): %s",
                    result.returncode,
                    (result.stderr or b"").decode("utf-8", "replace").strip()[:300],
                )
        except subprocess.TimeoutExpired:
            logger.warning("La notificación del sistema tardó más de %s s y se cortó", self.timeout)
        except Exception:  # noqa: BLE001 - un aviso fallido no puede romper nada
            logger.warning("No se pudo mostrar la notificación del sistema", exc_info=True)
