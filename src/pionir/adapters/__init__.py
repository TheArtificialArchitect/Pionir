"""Specialist adapters maintained by Pionir."""

from .atani_cli import AtaniCliAdapter, AtaniCliSettings
from .bryo_status import BryoStatusAdapter, BryoStatusSettings
from .content import ContentAdapter, ContentSettings
from .crew import CrewAdapter, CrewAdapterSettings
from .daedalus import DaedalusAdapter, DaedalusSettings
from .devto import DevtoAdapter, DevtoSettings
from .galatea import GalateaAdapter, GalateaSettings
from .instagram import InstagramAdapter, InstagramSettings
from .melete import MeleteAdapter, MeleteSettings
from .nyx_status import NyxStatusAdapter, NyxStatusSettings
from .stdio_json import StdioJsonAdapter, StdioJsonSettings, load_stdio_adapters
from .voodoo_status import VoodooStatusAdapter, VoodooStatusSettings

__all__ = [
    "AtaniCliAdapter",
    "AtaniCliSettings",
    "BryoStatusAdapter",
    "BryoStatusSettings",
    "ContentAdapter",
    "ContentSettings",
    "CrewAdapter",
    "CrewAdapterSettings",
    "DaedalusAdapter",
    "DaedalusSettings",
    "DevtoAdapter",
    "DevtoSettings",
    "GalateaAdapter",
    "GalateaSettings",
    "InstagramAdapter",
    "InstagramSettings",
    "MeleteAdapter",
    "MeleteSettings",
    "NyxStatusAdapter",
    "NyxStatusSettings",
    "StdioJsonAdapter",
    "StdioJsonSettings",
    "VoodooStatusAdapter",
    "VoodooStatusSettings",
    "load_stdio_adapters",
]
