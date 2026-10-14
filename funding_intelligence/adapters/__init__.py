from .bndes import BndesAdapter
from .cnpq import CnpqAdapter
from .embrapii import EmbrapiiAdapter
from .fapesq import FapesqAdapter
from .finep import FinepAdapter
from .pdil import PdilAdapter
from .ppsus import PpsusAdapter
from .transferegov import TransferegovAdapter
from .transferegov_apis import EspeciaisAdapter, FundoAFundoAdapter, ParceriasAdapter

ADAPTERS = {
    "bndes": BndesAdapter,
    "cnpq": CnpqAdapter,
    "embrapii": EmbrapiiAdapter,
    "fapesq": FapesqAdapter,
    "finep": FinepAdapter,
    "pdil": PdilAdapter,
    "ppsus": PpsusAdapter,
    "transferegov": TransferegovAdapter,
    "transferegov-especiais": EspeciaisAdapter,
    "transferegov-fundoafundo": FundoAFundoAdapter,
    "transferegov-parcerias": ParceriasAdapter,
}

__all__ = ["ADAPTERS"]
