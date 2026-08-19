from .cnpq import CnpqAdapter
from .fapesq import FapesqAdapter
from .finep import FinepAdapter
from .transferegov import TransferegovAdapter

ADAPTERS = {
    "cnpq": CnpqAdapter,
    "fapesq": FapesqAdapter,
    "finep": FinepAdapter,
    "transferegov": TransferegovAdapter,
}

__all__ = ["ADAPTERS"]
