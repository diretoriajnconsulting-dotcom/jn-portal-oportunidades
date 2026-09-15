from .cnpq import CnpqAdapter
from .fapesq import FapesqAdapter
from .finep import FinepAdapter
from .transferegov import TransferegovAdapter
from .transferegov_apis import EspeciaisAdapter, FundoAFundoAdapter, ParceriasAdapter

ADAPTERS = {
    "cnpq": CnpqAdapter,
    "fapesq": FapesqAdapter,
    "finep": FinepAdapter,
    "transferegov": TransferegovAdapter,
    "transferegov-especiais": EspeciaisAdapter,
    "transferegov-fundoafundo": FundoAFundoAdapter,
    "transferegov-parcerias": ParceriasAdapter,
}

__all__ = ["ADAPTERS"]
