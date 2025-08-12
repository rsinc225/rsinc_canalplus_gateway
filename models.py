from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict

class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")

# ---------- Payloads "stateless" ----------
class OffersStatelessRequest(FlexibleModel):
    subscriberId: Optional[str] = None
    cardIndex: Optional[int] = None
    distributorNumber: Optional[str] = None
    countryId: Optional[int] = None

class OptionsStatelessRequest(FlexibleModel):
    subscriberId: Optional[str] = None
    cardIndex: Optional[int] = None
    offerCode: Optional[str] = None
    distributorNumber: Optional[str] = None
    countryId: Optional[int] = None

class BasketItem(FlexibleModel):
    type: str
    code: Optional[str] = None
    quantity: Optional[int] = 1
    amount: Optional[float] = None

class BasketStatelessRequest(FlexibleModel):
    subscriberId: Optional[str] = None
    cardIndex: Optional[int] = None
    distributorNumber: Optional[str] = None
    items: List[BasketItem] = Field(default_factory=list)

# ---------- Quick renewal direct ----------
class QuickRenewalRequest(FlexibleModel):
    subscriberId: str
    cardIndex: int
    distributorNumber: Optional[str] = None
    paymentMethod: Optional[str] = None
    amount: Optional[float] = None
    offerCode: Optional[str] = None
    duration: Optional[str] = None

# ---------- Flow recharge (panier + exécution) ----------
class RechargeFlowRequest(FlexibleModel):
    basket: BasketStatelessRequest
    quickRenewal: QuickRenewalRequest

# ---------- Téléchargement du report ----------
class ReportDownloadRequest(FlexibleModel):
    reportUrl: str  # ex: "/reports/frameset?...__format=PDF..."
