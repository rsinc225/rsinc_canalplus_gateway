# app/main.py
import os
import time
import asyncio
from typing import Optional
from urllib.parse import urljoin
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from models import (
    OffersStatelessRequest,
    OptionsStatelessRequest,
    BasketStatelessRequest,
    QuickRenewalRequest,
    RechargeFlowRequest,
    ReportDownloadRequest,
)

# ================== CONFIG ==================
load_dotenv()

def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"❌ Variable d'environnement manquante: {name}")
    return v

CANAL_BASE      = require_env("CANAL_BASE").rstrip("/")
CANAL_AUTH_PATH = require_env("CANAL_AUTH_PATH")
CANAL_USER      = require_env("CANAL_USER")
CANAL_PASS      = require_env("CANAL_PASS")

BOMI_BASE      = os.getenv("BOMI_BASE", "").rstrip("/")
COUNTRY_ID     = os.getenv("COUNTRY_ID", "114")
SALE_DEVICE_ID = os.getenv("SALE_DEVICE_ID", "MYPOS")
DISTRIBUTOR_ID = os.getenv("DISTRIBUTOR_ID", "23268")

CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "10"))
READ_TIMEOUT    = float(os.getenv("READ_TIMEOUT", "60"))
WRITE_TIMEOUT   = float(os.getenv("WRITE_TIMEOUT", "30"))
POOL_TIMEOUT    = float(os.getenv("POOL_TIMEOUT", "10"))
REFRESH_SAFETY  = int(os.getenv("REFRESH_SAFETY", "15"))

ALLOWED_ORIGINS = [o for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o]
API_BASE = f"{CANAL_BASE}/api/rest/onlinesales/v1"
DEFAULT_TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT
)
BASE_HEADERS = {"Accept": "application/json"}

# Optionnel : clé d’API (si vide, pas de check)
EXPECTED_API_KEY = os.getenv("API_KEY", "").strip()

async def verify_api_key(x_api_key: Optional[str] = Header(default=None)):
    if not EXPECTED_API_KEY:
        return True  # pas de clé configurée -> pas de vérif
    if x_api_key != EXPECTED_API_KEY:
        raise HTTPException(status_code=401, detail="X-API-KEY invalide")
    return True

# ================== SESSION CANAL ==================
class CanalSession:
    def __init__(self):
        self._token: Optional[str] = None
        self._exp: float = 0.0
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self):
        self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        # Auth au boot (si ça échoue, on réessaiera au premier call)
        try:
            await self.login()
        except Exception as e:
            print("[STARTUP] Auth différée:", e)

    async def stop(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def login(self) -> None:
        assert self._client is not None, "client HTTP non initialisé"
        url = f"{CANAL_BASE}{CANAL_AUTH_PATH}"
        payload = {"userName": CANAL_USER, "password": CANAL_PASS}

        r = await self._client.post(
            url, json=payload, headers={**BASE_HEADERS, "Content-Type": "application/json"}
        )
        print(f"[AUTH] status={r.status_code} url={url}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Auth failed ({r.status_code})")

        data = r.json()
        token = data.get("token")
        if not token:
            raise HTTPException(status_code=502, detail="Auth: 'token' introuvable")

        ttl = 300  # ~5 min (Pas d’info d’expiration fournie)
        now = time.time()
        self._token = token
        self._exp = now + ttl - REFRESH_SAFETY
        print(f"[AUTH] token en cache ~{ttl - REFRESH_SAFETY}s")

    async def ensure_token(self) -> str:
        async with self._lock:
            if not self._token or time.time() >= self._exp:
                await self.login()
            return self._token  # type: ignore

    async def req(self, method: str, url: str, **kwargs) -> httpx.Response:
        if not self._client:
            await self.start()
        token = await self.ensure_token()
        headers = kwargs.pop("headers", {})
        headers = {**BASE_HEADERS, **headers, "Authorization": f"Bearer {token}"}

        r = await self._client.request(method, url, headers=headers, **kwargs)  # type: ignore
        if r.status_code == 401:
            async with self._lock:
                await self.login()
                headers["Authorization"] = f"Bearer {self._token}"
            r = await self._client.request(method, url, headers=headers, **kwargs)  # type: ignore
        return r

auth = CanalSession()

# ================== APP (lifespan) ==================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await auth.start()
    yield
    await auth.stop()

app = FastAPI(title="RSINC Canal+ Gateway", version="1.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ================== Utils ==================
def _to_json_response(r: httpx.Response) -> JSONResponse:
    try:
        return JSONResponse(status_code=r.status_code, content=r.json())
    except Exception:
        return JSONResponse(status_code=r.status_code, content={"raw": r.text})

def _abs_report_url(report_url: str) -> str:
    return report_url if report_url.startswith("http") else urljoin(CANAL_BASE + "/", report_url.lstrip("/"))

# ================== Health/Auth ==================
@app.get("/health")
async def health():
    return {"ok": True, "token_cached": bool(auth._token)}

@app.api_route("/authenticate", methods=["GET", "POST"], dependencies=[Depends(verify_api_key)])
async def authenticate():
    await auth.login()
    return {"authenticated": True, "token_cached": True}

# ============ ENDPOINTS Canal+ (lecture) ============
@app.get("/distributors", dependencies=[Depends(verify_api_key)])
async def get_distributors():
    url = f"{API_BASE}/cgaOnlineSales/getDistributors"
    r = await auth.req("GET", url)
    return _to_json_response(r)

@app.get("/distributors/{number}/creditPDV", dependencies=[Depends(verify_api_key)])
async def credit_pdv(number: str):
    url = f"{API_BASE}/cgaOnlineSales/getDistributors/{number}/creditPDV"
    r = await auth.req("GET", url)
    return _to_json_response(r)

@app.get("/distributors/{number}/rights", dependencies=[Depends(verify_api_key)])
async def distributor_rights(number: str):
    url = f"{API_BASE}/cgaOnlineSales/getDistributors/{number}/rights"
    r = await auth.req("GET", url)
    return _to_json_response(r)

@app.get("/subscribers", dependencies=[Depends(verify_api_key)])
async def get_subscribers(
    userId: str,
    distributorNumber: str,
    subscriberNumber: Optional[str] = None,
    phoneNumber: Optional[str] = None,
    materialNumber: Optional[str] = None,
    email: Optional[str] = None,
):
    url = f"{API_BASE}/cgaOnlineSales/getSubscribers"
    params = {
        "userId": userId,
        "distributorNumber": distributorNumber,
        "subscriberNumber": subscriberNumber,
        "phoneNumber": phoneNumber,
        "materialNumber": materialNumber,
        "email": email,
    }
    params = {k: v for k, v in params.items() if v not in (None, "")}
    r = await auth.req("GET", url, params=params)
    return _to_json_response(r)

@app.get("/countries", dependencies=[Depends(verify_api_key)])
async def get_countries():
    url = f"{API_BASE}/cgaOnlineSales/getCountries"
    r = await auth.req("GET", url)
    return _to_json_response(r)

@app.get("/solde-subscriber/{subscriberId}/{cardIndex}", dependencies=[Depends(verify_api_key)])
async def get_solde_subscriber(subscriberId: str, cardIndex: int):
    url = f"{API_BASE}/cgaOnlineSales/getSoldeSubscriber/{subscriberId}/{cardIndex}"
    r = await auth.req("GET", url)
    return _to_json_response(r)

@app.get("/reactivate-possible/{subscriberId}/{cardIndex}", dependencies=[Depends(verify_api_key)])
async def is_possible_to_reactivate(subscriberId: str, cardIndex: int):
    url = f"{API_BASE}/cgaOnlineSales/isPossibleToReactivate/{subscriberId}/{cardIndex}"
    r = await auth.req("GET", url)
    return _to_json_response(r)

@app.get("/available-coupons/{subscriberId}/{cardIndex}", dependencies=[Depends(verify_api_key)])
async def get_available_coupons(subscriberId: str, cardIndex: int):
    url = f"{API_BASE}/cgaOnlineSales/getAvailableCoupons/{subscriberId}/{cardIndex}"
    r = await auth.req("GET", url)
    return _to_json_response(r)

@app.get("/eligibility-appointment", dependencies=[Depends(verify_api_key)])
async def eligibility_appointment(dateToDate: str = Query(...)):
    url = f"{API_BASE}/cgaOnlineSales/eligibilityAppointment"
    r = await auth.req("GET", url, params={"dateToDate": dateToDate})
    return _to_json_response(r)

@app.get("/can-be-renewed", dependencies=[Depends(verify_api_key)])
async def can_be_renewed(
    dateToDate: str = Query("dd"),
    distributorNumber: str = Query(...),
):
    url = f"{API_BASE}/cgaOnlineSales/canBeRenewed"
    r = await auth.req("GET", url, params={"dateToDate": dateToDate, "distributorNumber": distributorNumber})
    return _to_json_response(r)

@app.get("/group-broadcasting-ways", dependencies=[Depends(verify_api_key)])
async def get_group_broadcasting_ways(dateToDate: str = Query("dd")):
    url = f"{API_BASE}/cgaOnlineSales/getGroupBroadcastingWays"
    r = await auth.req("GET", url, params={"dateToDate": dateToDate})
    return _to_json_response(r)

@app.get("/durations", dependencies=[Depends(verify_api_key)])
async def get_durations(
    dateToDate: str = Query("dd"),
    distributorNumber: str = Query(...),
):
    url = f"{API_BASE}/cgaOnlineSales/getDurations"
    r = await auth.req("GET", url, params={"dateToDate": dateToDate, "distributorNumber": distributorNumber})
    return _to_json_response(r)

@app.get("/payment-methods", dependencies=[Depends(verify_api_key)])
async def get_payment_methods(dateToDate: str = Query("dd")):
    url = f"{API_BASE}/cgaOnlineSales/getPaymentMethods"
    r = await auth.req("GET", url, params={"dateToDate": dateToDate})
    return _to_json_response(r)

# ---------- Stateless (POST) ----------
@app.post("/offers/stateless", dependencies=[Depends(verify_api_key)])
async def get_available_offers_stateless(payload: OffersStatelessRequest):
    url = f"{API_BASE}/cgaOnlineSales/getAvailableOffersStateless"
    r = await auth.req("POST", url, json=payload.model_dump(mode="json"))
    return _to_json_response(r)

@app.post("/options/stateless", dependencies=[Depends(verify_api_key)])
async def get_available_options_stateless(payload: OptionsStatelessRequest):
    url = f"{API_BASE}/cgaOnlineSales/getAvailableOptionsStateless"
    r = await auth.req("POST", url, json=payload.model_dump(mode="json"))
    return _to_json_response(r)

@app.post("/basket/stateless", dependencies=[Depends(verify_api_key)])
async def get_basket_stateless(payload: BasketStatelessRequest):
    url = f"{API_BASE}/cgaOnlineSales/getBasketStateless"
    r = await auth.req("POST", url, json=payload.model_dump(mode="json"))
    return _to_json_response(r)

# ---------- BOMI ----------
@app.get("/bomi/payment-means", dependencies=[Depends(verify_api_key)])
async def bomi_payment_means(
    countryId: str = Query(COUNTRY_ID),
    managementAct: str = Query("FLASH_RENEWAL"),
    saleDeviceId: str = Query(SALE_DEVICE_ID),
    distributorId: str = Query(DISTRIBUTOR_ID),
):
    if not BOMI_BASE:
        raise HTTPException(500, "BOMI_BASE non configuré")
    url = f"{BOMI_BASE}/api/v1/paymentMeans"
    params = {
        "countryId": countryId,
        "managementAct": managementAct,
        "saleDeviceId": saleDeviceId,
        "distributorId": distributorId,
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        r = await client.get(url, params=params, headers=BASE_HEADERS)
    return _to_json_response(r)

@app.get("/bomi/payment-means/quick", dependencies=[Depends(verify_api_key)])
async def bomi_payment_means_quick(
    countryId: str = Query(COUNTRY_ID),
    managementAct: str = Query("RENEWAL_QUICK"),
    saleDeviceId: str = Query(SALE_DEVICE_ID),
    distributorId: str = Query(DISTRIBUTOR_ID),
):
    if not BOMI_BASE:
        raise HTTPException(500, "BOMI_BASE non configuré")
    url = f"{BOMI_BASE}/api/v1/paymentMeans"
    params = {
        "countryId": countryId,
        "managementAct": managementAct,
        "saleDeviceId": saleDeviceId,
        "distributorId": distributorId,
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        r = await client.get(url, params=params, headers=BASE_HEADERS)
    return _to_json_response(r)

# ---------- Quick renewal direct ----------
@app.post("/register-quick-renewal", dependencies=[Depends(verify_api_key)])
async def register_quick_renewal(payload: QuickRenewalRequest):
    url = f"{API_BASE}/cgaOnlineSales/registerQuickRenewal"
    r = await auth.req("POST", url, json=payload.model_dump(mode="json"))
    return _to_json_response(r)

# ---------- Flow complet + lien PDF ----------
@app.post("/recharge", dependencies=[Depends(verify_api_key)])
async def recharge_flow(flow: RechargeFlowRequest):
    # 1) Basket
    basket_url = f"{API_BASE}/cgaOnlineSales/getBasketStateless"
    r_basket = await auth.req("POST", basket_url, json=flow.basket.model_dump(mode="json"))
    if r_basket.status_code != 200:
        return _to_json_response(r_basket)
    basket_json = r_basket.json()
    if basket_json.get("severity") != "SUCCESS":
        return JSONResponse(status_code=502, content={"detail": "Panier non SUCCESS", "basket": basket_json})

    basket = basket_json.get("basket") or {}
    first_amount = basket.get("firstAmount")
    duration = basket.get("duration")
    sel_offer = basket.get("selectedOffer") or {}
    offer_code = sel_offer.get("offerCode")

    # 2) BOMI (info seulement)
    bomi_json = None
    if BOMI_BASE:
        bomi_url = f"{BOMI_BASE}/api/v1/paymentMeans"
        params = {
            "countryId": COUNTRY_ID,
            "managementAct": "RENEWAL_QUICK",
            "saleDeviceId": SALE_DEVICE_ID,
            "distributorId": DISTRIBUTOR_ID,
        }
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            r_bomi = await client.get(bomi_url, params=params, headers=BASE_HEADERS)
        try:
            bomi_json = r_bomi.json()
        except Exception:
            bomi_json = {"raw": r_bomi.text}

    # 3) registerQuickRenewal
    quick = flow.quickRenewal.model_dump(mode="json")
    quick.setdefault("amount", first_amount)
    if offer_code and not quick.get("offerCode"):
        quick["offerCode"] = offer_code
    if duration and not quick.get("duration"):
        quick["duration"] = str(duration)

    reg_url = f"{API_BASE}/cgaOnlineSales/registerQuickRenewal"
    r_reg = await auth.req("POST", reg_url, json=quick)

    try:
        reg_json = r_reg.json()
    except Exception:
        reg_json = {"raw": r_reg.text}

    report_url = reg_json.get("reportUrl")
    absolute_report = _abs_report_url(report_url) if report_url else None

    return JSONResponse(
        status_code=200 if r_reg.status_code == 200 else r_reg.status_code,
        content={
            "status": reg_json.get("severity"),
            "message": reg_json.get("message"),
            "reportUrl": report_url,
            "reportUrlAbsolute": absolute_report,
            "steps": {
                "basket": basket_json,
                "paymentMeansQuick": bomi_json,
                "registerQuickRenewal": reg_json,
            },
        },
    )

# ---------- Téléchargement du PDF ----------
@app.get("/report/download", dependencies=[Depends(verify_api_key)])
async def report_download_get(reportUrl: str):
    return await _download_report(reportUrl)

@app.post("/report/download", dependencies=[Depends(verify_api_key)])
async def report_download_post(body: ReportDownloadRequest):
    return await _download_report(body.reportUrl)

async def _download_report(reportUrl: str):
    if not reportUrl or "reports/" not in reportUrl:
        raise HTTPException(400, "reportUrl invalide")
    url = _abs_report_url(reportUrl)
    r = await auth.req("GET", url, headers={"Accept": "*/*"})
    ctype = r.headers.get("Content-Type", "").lower()
    fname = "recu.pdf"
    dispo = r.headers.get("Content-Disposition", "")
    if "filename=" in dispo:
        fname = dispo.split("filename=")[-1].strip('"; ')
    if "application/pdf" in ctype or url.lower().endswith(".pdf"):
        return StreamingResponse(
            iter([r.content]),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    return StreamingResponse(
        iter([r.content]),
        media_type=ctype or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
# ================== Endpoints BOMI (lecture) ==================
