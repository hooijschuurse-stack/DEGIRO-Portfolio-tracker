"""
Marktdata via Yahoo Finance (yfinance).

- ISIN -> Yahoo-ticker omzetten (met cache in ticker_mapping.json,
  die je zelf mag aanpassen als een ticker verkeerd gevonden wordt)
- Actuele koersen en koershistorie ophalen
- Valuta omrekenen naar EUR
"""

import json
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

MAPPING_BESTAND = Path(__file__).parent / "ticker_mapping.json"
INFO_BESTAND = Path(__file__).parent / "info_cache.json"
OVERRIDE_BESTAND = Path(__file__).parent / "koers_overrides.json"
STRATEGIE_BESTAND = Path(__file__).parent / "strategie.json"

# Strategie-labels die je per positie kunt kiezen (zoals PDT's strategie-tag).
STRATEGIE_OPTIES = ["Onbekend", "Waardebeleggen", "Groei beleggen", "Dividend",
                    "Indexbeleggen (ETF)", "Speculatief", "Lange termijn"]


def lees_strategie() -> dict[str, str]:
    """Handmatige strategie-tag per ISIN: {isin: label}."""
    if STRATEGIE_BESTAND.exists():
        return json.loads(STRATEGIE_BESTAND.read_text(encoding="utf-8"))
    return {}


def zet_strategie(isin: str, label: str | None) -> None:
    """Sla een strategie-label op (of verwijder bij leeg/Onbekend)."""
    s = lees_strategie()
    if label and label != "Onbekend":
        s[isin] = label
    else:
        s.pop(isin, None)
    STRATEGIE_BESTAND.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


def lees_koers_overrides() -> dict[str, float]:
    """Handmatige koersen (EUR per stuk) voor periodes zonder marktdata: {isin: koers}."""
    if OVERRIDE_BESTAND.exists():
        return json.loads(OVERRIDE_BESTAND.read_text(encoding="utf-8"))
    return {}


def zet_koers_override(isin: str, koers_eur: float | None) -> None:
    """Sla een handmatige koers op (of verwijder als koers None/0 is)."""
    ov = lees_koers_overrides()
    if koers_eur:
        ov[isin] = float(koers_eur)
    else:
        ov.pop(isin, None)
    OVERRIDE_BESTAND.write_text(json.dumps(ov, indent=2), encoding="utf-8")

# Vertaling van Yahoo's Engelse sectornamen naar het Nederlands.
SECTOR_NL = {
    "Technology": "Technologie",
    "Financial Services": "Financiële diensten",
    "Healthcare": "Gezondheidszorg",
    "Consumer Cyclical": "Consument (cyclisch)",
    "Consumer Defensive": "Consument (defensief)",
    "Industrials": "Industrie",
    "Energy": "Energie",
    "Utilities": "Nutsbedrijven",
    "Basic Materials": "Grondstoffen",
    "Communication Services": "Communicatie",
    "Real Estate": "Vastgoed",
}

# Bekende ISIN -> Yahoo-ticker combinaties als startpunt.
BEKENDE_TICKERS = {
    "NL0010273215": "ASML.AS",
    "US0378331005": "AAPL",
    "US88160R1014": "TSLA",
    "US5949181045": "MSFT",
    "US02079K3059": "GOOGL",
    "US0231351067": "AMZN",
    "US67066G1040": "NVDA",
    "GB00BP6MXD84": "SHELL.AS",
    "NL0011794037": "AD.AS",
    "NL0000235190": "AIR.PA",
    "NL0011821202": "INGA.AS",
    "NL0000009082": "KPN.AS",
    "NL0013654783": "PRX.AS",
    "IE00B3RBWM25": "VWRL.AS",
    "IE00B4L5Y983": "IWDA.AS",
    "US4581401001": "INTC",
    "US30303M1027": "META",
    # Uit een echte DEGIRO-export geleerd (anders pakt Yahoo een dood listing):
    "NL0012866412": "BESI.AS",      # BE Semiconductor
    "NL0010558797": "OCI.AS",       # OCI
    "NL0010407946": "TRIO.AS",      # Triodos Bank
    "NL0000009165": "HEIA.AS",      # Heineken
    "US64110L1061": "NFLX",         # Netflix
    "US81762P1021": "NOW",          # ServiceNow
    "IE000J80JTL1": "GRID.DE",      # First Trust Nasdaq Clean Edge Smart Grid UCITS
    "IE00B5BMR087": "SXR8.DE",      # iShares Core S&P 500 UCITS (acc)
    "NL0011683594": "TDIV.AS",      # VanEck Morningstar Dev. Markets Dividend Leaders
    "NL0009272749": "TDT.AS",       # VanEck AEX UCITS (TAEX/TADX zijn delisted)
    "IE00B1XNHC34": "INRG.L",       # iShares Global Clean Energy
    "GB00B10RZP78": "ULVR.L",       # Unilever
    "NL0011872650": "BFIT.AS",      # Basic-Fit
    "NL0012015705": "",             # Just Eat Takeaway: overgenomen/gedelist, GEEN betrouwbare
                                    # ticker (JTKWY US-OTC heeft verkeerde prijsschaal: $4 vs €20
                                    # overnameprijs). Negeren i.p.v. fout waarderen.
    "NL0010773842": "NN.AS",        # NN Group
    "NL0011872643": "ASRNL.AS",     # ASR Nederland
    "NL0009739416": "PNL.AS",       # PostNL
    "NL0011821392": "LIGHT.AS",     # Signify
    "BMG3602E1084": "FLOW.AS",      # Flow Traders
    "NL0012747059": "CMCOM.AS",     # CM.com
    "NL0000337319": "BAMNB.AS",     # BAM Groep
    "NL0015000K93": "ECMPA.AS",     # Eurocommercial Properties
    "IE00B0M62Y33": "IAEX.AS",      # iShares AEX
    "US22788C1053": "CRWD",         # CrowdStrike
    "US62914V1061": "NIO",          # NIO
}

_valuta_cache: dict[str, str] = {}


def _lees_mapping() -> dict:
    if MAPPING_BESTAND.exists():
        return json.loads(MAPPING_BESTAND.read_text(encoding="utf-8"))
    return {}


def _schrijf_mapping(mapping: dict) -> None:
    MAPPING_BESTAND.write_text(json.dumps(mapping, indent=2), encoding="utf-8")


# Beurssuffixen op volgorde van voorkeur: EUR-noteringen eerst, dan Londen,
# dan de rest. Zo vermijden we dode of vreemd-genoteerde listings.
_BEURS_VOORKEUR = (".AS", ".DE", ".PA", ".MI", ".BR", ".LS", ".AMS", ".F", ".L")


# Benchmark-indices (Yahoo-tickers). Voor MSCI World gebruiken we de URTH-ETF.
BENCHMARKS = {"AEX": "^AEX", "S&P 500": "^GSPC", "MSCI World": "URTH", "NASDAQ": "^IXIC"}


def index_historie(naam: str, start) -> pd.Series:
    """Slotkoersen van een benchmark-index sinds `start` (eigen valuta)."""
    ticker = BENCHMARKS.get(naam)
    if not ticker:
        return pd.Series(dtype=float)
    try:
        data = yf.download(ticker, start=start, auto_adjust=True, progress=False, threads=False)["Close"]
        if isinstance(data, pd.DataFrame):
            data = data.iloc[:, 0]
        s = data.dropna()
        s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
        return s
    except Exception:
        return pd.Series(dtype=float)


def heeft_koersdata(ticker: str) -> bool:
    """Snelle check of een ticker recente koersdata heeft (niet delisted)."""
    try:
        return len(yf.Ticker(ticker).history(period="5d")) > 0
    except Exception:
        return False


def _beurs_score(symbool: str) -> int:
    for i, suf in enumerate(_BEURS_VOORKEUR):
        if symbool.endswith(suf):
            return i
    return len(_BEURS_VOORKEUR)  # tickers zonder suffix (VS) achteraan


def zoek_ticker(isin: str, productnaam: str = "") -> str | None:
    """
    Zet een ISIN om naar een Yahoo Finance ticker.
    Volgorde: eigen mapping-bestand -> bekende lijst -> Yahoo-zoekfunctie.

    Bij de zoekfunctie controleren we of een kandidaat écht koersdata heeft
    (Yahoo geeft voor UCITS-ETF's vaak een dood listing terug, bijv. de
    Stuttgart-notering) en kiezen we bij voorkeur een EUR-/Europese beurs.
    Het resultaat wordt opgeslagen zodat we maar één keer hoeven te zoeken.
    """
    mapping = _lees_mapping()
    if isin in mapping:
        return mapping[isin] or None  # leeg = bewust genegeerd
    if isin in BEKENDE_TICKERS:
        mapping[isin] = BEKENDE_TICKERS[isin]
        _schrijf_mapping(mapping)
        return mapping[isin]

    ticker = None
    try:
        quotes = yf.Search(isin, max_results=10).quotes
        kandidaten = [q.get("symbol") for q in quotes if q.get("symbol")]
        kandidaten.sort(key=_beurs_score)  # Europese/EUR-listings eerst
        met_data = next((s for s in kandidaten if heeft_koersdata(s)), None)
        ticker = met_data or (kandidaten[0] if kandidaten else None)
    except Exception:
        pass

    mapping[isin] = ticker or ""
    _schrijf_mapping(mapping)
    return ticker


def zet_ticker(isin: str, ticker: str) -> None:
    """Sla een (handmatig gecorrigeerde) ticker op. Leeg = product negeren."""
    mapping = _lees_mapping()
    mapping[isin] = ticker.strip()
    _schrijf_mapping(mapping)


def valuta_van(ticker: str) -> str:
    """In welke valuta noteert deze ticker? (EUR, USD, GBp, ...)"""
    if ticker in _valuta_cache:
        return _valuta_cache[ticker]
    valuta = "EUR"
    try:
        valuta = yf.Ticker(ticker).fast_info["currency"] or "EUR"
    except Exception:
        pass
    _valuta_cache[ticker] = valuta
    return valuta


def koers_historie(tickers: list[str], start) -> pd.DataFrame:
    """Dagelijkse slotkoersen per ticker vanaf `start` (lokale valuta)."""
    if not tickers:
        return pd.DataFrame()
    data = yf.download(tickers, start=start, auto_adjust=False, progress=False, threads=False)["Close"]
    if isinstance(data, pd.Series):  # bij één ticker geeft yfinance een Series
        data = data.to_frame(name=tickers[0])
    return data


def actuele_koersen(tickers: list[str]) -> dict[str, float]:
    """Meest recente koers per ticker (lokale valuta, ~15 min vertraagd)."""
    if not tickers:
        return {}
    data = yf.download(tickers, period="5d", auto_adjust=False, progress=False, threads=False)["Close"]
    if isinstance(data, pd.Series):
        data = data.to_frame(name=tickers[0])
    koersen = {}
    for t in data.columns:
        serie = data[t].dropna()
        if len(serie):
            koersen[t] = float(serie.iloc[-1])
    return koersen


def fx_naar_eur_historie(valuta: str, start) -> pd.Series | None:
    """
    Dagelijkse wisselkoers: hoeveel EUR is 1 eenheid `valuta` waard?
    Geeft None terug voor EUR (geen omrekening nodig).
    GBp (Britse pence) wordt als GBP/100 behandeld.
    """
    if valuta == "EUR":
        return None
    pence = valuta in ("GBp", "GBX")
    basis = "GBP" if pence else valuta
    try:
        serie = yf.download(f"{basis}EUR=X", start=start, auto_adjust=False, progress=False)["Close"]
        if isinstance(serie, pd.DataFrame):
            serie = serie.iloc[:, 0]
        serie = serie.dropna()
        return serie / 100 if pence else serie
    except Exception:
        return None


def product_info(ticker: str) -> dict:
    """
    Sector, land en type (Aandeel/ETF) van een ticker, met cache op schijf
    omdat dit ophalen traag is (1-2 seconden per ticker, eenmalig).
    """
    cache = {}
    if INFO_BESTAND.exists():
        cache = json.loads(INFO_BESTAND.read_text(encoding="utf-8"))
    # Herlaad als de cache nog van vóór de laatste uitbreiding is (beta/payout/yield).
    if ticker in cache and "beta" in cache[ticker]:
        return cache[ticker]

    info = {}
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        pass

    soort = info.get("quoteType", "")
    sector = info.get("sector")
    if soort == "ETF":
        # ETF's hebben geen sector; de fondscategorie is informatiever.
        sector = info.get("category") or "ETF / Indexfonds"
        land = "Gespreid (fonds)"
        industrie = info.get("category") or "ETF / Indexfonds"
    else:
        land = info.get("country") or "Onbekend"
        industrie = info.get("industry") or "Onbekend"

    resultaat = {
        "sector": SECTOR_NL.get(sector, sector) or "Onbekend",
        "industrie": industrie,
        "land": land,
        "werelddeel": werelddeel_van_land(land),
        "type": {"EQUITY": "Aandeel", "ETF": "ETF"}.get(soort, soort or "Onbekend"),
        "marktkap_usd": _marktkap_usd(info),
        "beta": info.get("beta"),
        "payout_ratio": info.get("payoutRatio"),
        "div_yield": info.get("dividendYield"),
    }
    cache[ticker] = resultaat
    INFO_BESTAND.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return resultaat


def _marktkap_usd(info: dict) -> float | None:
    """Marktkapitalisatie in USD uit yfinance .info; None als onbekend/ETF."""
    mc = info.get("marketCap")
    if not mc:
        return None
    valuta = info.get("currency") or "USD"
    if valuta == "USD":
        return float(mc)
    # Grof omrekenen naar USD voor de grootteschaal (categorie-indeling is ruim).
    fx_eur = fx_naar_eur_nu(valuta)             # 1 valuta = fx_eur EUR
    usd_per_eur = 1.0 / fx_naar_eur_nu("USD") if fx_naar_eur_nu("USD") else 1.0
    return float(mc) * fx_eur * usd_per_eur


# Marktkapitalisatie-categorieën van PDT (art. 280), grenzen in USD.
_MARKTKAP_GRENZEN = (
    (300e9, "Mega Cap"),
    (50e9, "Large Cap"),
    (10e9, "Mid Cap"),
    (1e9, "Small Cap"),
    (100e6, "Micro Cap"),
)


def marktkap_categorie(marktkap_usd: float | None) -> str:
    """Deel een marktkapitalisatie (USD) in volgens PDT's schaal (art. 280)."""
    if not marktkap_usd:
        return "Onbekend"
    for grens, naam in _MARKTKAP_GRENZEN:
        if marktkap_usd >= grens:
            return naam
    return "Nano Cap"


# Land (Yahoo's Engelse landnaam) -> werelddeel.
_WERELDDEEL = {
    "Netherlands": "Europa", "Germany": "Europa", "France": "Europa", "Belgium": "Europa",
    "United Kingdom": "Europa", "Switzerland": "Europa", "Spain": "Europa", "Italy": "Europa",
    "Ireland": "Europa", "Sweden": "Europa", "Denmark": "Europa", "Norway": "Europa",
    "Finland": "Europa", "Luxembourg": "Europa", "Austria": "Europa", "Portugal": "Europa",
    "United States": "Noord-Amerika", "Canada": "Noord-Amerika", "Mexico": "Noord-Amerika",
    "China": "Azië", "Japan": "Azië", "Taiwan": "Azië", "South Korea": "Azië", "India": "Azië",
    "Hong Kong": "Azië", "Singapore": "Azië", "Israel": "Azië",
    "Brazil": "Zuid-Amerika", "Argentina": "Zuid-Amerika",
    "Australia": "Oceanië", "New Zealand": "Oceanië",
    "South Africa": "Afrika",
}


def werelddeel_van_land(land: str | None) -> str:
    """Vertaal een (Engelse) landnaam naar een werelddeel."""
    if not land or land == "Onbekend":
        return "Onbekend"
    if land == "Gespreid (fonds)":
        return "Gespreid (fonds)"
    return _WERELDDEEL.get(land, "Overig")


# Yahoo-beurssuffix -> leesbare beursnaam.
_BEURS_NAMEN = {
    ".AS": "Euronext Amsterdam", ".PA": "Euronext Parijs", ".BR": "Euronext Brussel",
    ".LS": "Euronext Lissabon", ".DE": "Xetra (Frankfurt)", ".F": "Frankfurt",
    ".MI": "Borsa Italiana", ".L": "London Stock Exchange", ".SW": "SIX (Zwitserland)",
    ".MC": "Bolsa Madrid", ".ST": "Nasdaq Stockholm", ".CO": "Nasdaq Kopenhagen",
    ".HE": "Nasdaq Helsinki", ".OL": "Oslo Børs", ".VI": "Wenen",
}


def beurs_van_ticker(ticker: str | None) -> str:
    """Leid de beurs af uit het Yahoo-tickersuffix; geen suffix = VS (NYSE/Nasdaq)."""
    if not ticker:
        return "Onbekend"
    for suf, naam in _BEURS_NAMEN.items():
        if ticker.endswith(suf):
            return naam
    return "VS (NYSE/Nasdaq)" if "." not in ticker else "Overig"


def dividend_info(ticker: str) -> dict:
    """
    Vooruitkijkende dividendgegevens van een ticker:
    - rate: verwacht dividend per aandeel per jaar (lokale valuta)
    - yield: dividendrendement (fractie)
    - ex_datum: eerstvolgende/laatste ex-dividenddatum (string of None)
    Komt uit yfinance .info; ontbreekt vaak bij ETF's/groeiaandelen.
    """
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        info = {}
    ex = info.get("exDividendDate")
    ex_datum = None
    if ex:
        try:
            ex_datum = pd.to_datetime(ex, unit="s").date().isoformat()
        except Exception:
            ex_datum = None
    return {
        "rate": info.get("dividendRate") or 0.0,
        "yield": info.get("dividendYield") or 0.0,
        "ex_datum": ex_datum,
    }


def dividend_historie(ticker: str, start) -> pd.Series:
    """Uitgekeerde dividenden per aandeel (lokale valuta) sinds `start`."""
    try:
        s = yf.Ticker(ticker).dividends
        if s is None or len(s) == 0:
            return pd.Series(dtype=float)
        s.index = s.index.tz_localize(None)
        return s[s.index >= pd.Timestamp(start)]
    except Exception:
        return pd.Series(dtype=float)


WATCHLIST_BESTAND = Path(__file__).parent / "watchlist.json"


def lees_watchlist() -> list[str]:
    """De gevolgde tickers (watchlist)."""
    if WATCHLIST_BESTAND.exists():
        return json.loads(WATCHLIST_BESTAND.read_text(encoding="utf-8"))
    return []


def voeg_watchlist(ticker: str) -> None:
    wl = lees_watchlist()
    t = ticker.strip().upper()
    if t and t not in wl:
        wl.append(t)
        WATCHLIST_BESTAND.write_text(json.dumps(wl, indent=2), encoding="utf-8")


def verwijder_watchlist(ticker: str) -> None:
    wl = [t for t in lees_watchlist() if t != ticker]
    WATCHLIST_BESTAND.write_text(json.dumps(wl, indent=2), encoding="utf-8")


CONFIG_BESTAND = Path(__file__).parent / "config.json"
MACRO_BESTAND = Path(__file__).parent / "macro_events.json"


def lees_config() -> dict:
    if CONFIG_BESTAND.exists():
        return json.loads(CONFIG_BESTAND.read_text(encoding="utf-8"))
    return {}


def zet_config(sleutel: str, waarde: str) -> None:
    cfg = lees_config()
    cfg[sleutel] = waarde
    CONFIG_BESTAND.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def lees_macro_curated() -> list[dict]:
    """Bewerkbare, gecureerde macro-agenda (macro_events.json). Wordt geseed met
    een paar indicatieve voorbeelden die je zelf kunt aanpassen/uitbreiden."""
    if MACRO_BESTAND.exists():
        return json.loads(MACRO_BESTAND.read_text(encoding="utf-8"))
    seed = [
        {"datum": "2026-06-18", "land": "US", "event": "Fed (FOMC) rentebesluit", "impact": "High"},
        {"datum": "2026-07-15", "land": "US", "event": "Inflatie (CPI)", "impact": "High"},
        {"datum": "2026-07-24", "land": "EU", "event": "ECB rentebesluit", "impact": "High"},
        {"datum": "2026-08-01", "land": "US", "event": "Banenrapport (NFP)", "impact": "High"},
    ]
    MACRO_BESTAND.write_text(json.dumps(seed, indent=2, ensure_ascii=False), encoding="utf-8")
    return seed


def macro_events_api(api_key: str, dagen: int = 45) -> list[dict]:
    """
    Live macro-economische agenda via Financial Modeling Prep (gratis API-key).
    Geeft alleen relevante landen (VS/eurozone/NL/DE) met impact High/Medium.
    Lege lijst bij geen key of fout (dan valt de app terug op de gecureerde lijst).
    """
    if not api_key:
        return []
    van = date.today().isoformat()
    tot = (date.today() + timedelta(days=dagen)).isoformat()
    url = ("https://financialmodelingprep.com/api/v3/economic_calendar"
           f"?from={van}&to={tot}&apikey={api_key}")
    try:
        with urllib.request.urlopen(url, timeout=12) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []
    landen = {"US", "EU", "Euro Zone", "Eurozone", "Netherlands", "Germany"}
    uit = []
    for e in data if isinstance(data, list) else []:
        if e.get("country") not in landen or e.get("impact") not in ("High", "Medium"):
            continue
        uit.append({"datum": str(e.get("date") or "")[:10], "land": e.get("country"),
                    "event": e.get("event"), "impact": e.get("impact")})
    return uit


DOELGEWICHTEN_BESTAND = Path(__file__).parent / "doelgewichten.json"


def lees_doelgewichten() -> dict[str, float]:
    """Doelgewicht (%) per ISIN voor rebalancing: {isin: pct}."""
    if DOELGEWICHTEN_BESTAND.exists():
        return json.loads(DOELGEWICHTEN_BESTAND.read_text(encoding="utf-8"))
    return {}


def zet_doelgewichten(gewichten: dict[str, float]) -> None:
    """Sla alle doelgewichten in één keer op (alleen >0 bewaren)."""
    schoon = {isin: float(p) for isin, p in gewichten.items() if p and float(p) > 0}
    DOELGEWICHTEN_BESTAND.write_text(json.dumps(schoon, indent=2), encoding="utf-8")


def fast_quote(ticker: str) -> dict:
    """Snelle koers-snapshot (zonder de trage .info): koers, vorige slot, valuta,
    52w-bereik, marktkap."""
    try:
        fi = yf.Ticker(ticker).fast_info
        return {
            "koers": fi.get("lastPrice"), "vorige": fi.get("previousClose"),
            "valuta": fi.get("currency"), "jaar_laag": fi.get("yearLow"),
            "jaar_hoog": fi.get("yearHigh"), "marktkap": fi.get("marketCap"),
        }
    except Exception:
        return {}


def earnings_datum(ticker: str):
    """Eerstvolgende (of laatste bekende) earnings-datum als ISO-string, of None."""
    try:
        cal = yf.Ticker(ticker).calendar or {}
        ed = cal.get("Earnings Date")
        if isinstance(ed, (list, tuple)) and ed:
            return pd.Timestamp(ed[0]).date().isoformat()
        if ed:
            return pd.Timestamp(ed).date().isoformat()
    except Exception:
        pass
    return None


def bedrijfsnieuws(ticker: str, maximaal: int = 5) -> list[dict]:
    """Recente nieuwsberichten van een ticker (titel, bron, datum, link)."""
    uit = []
    try:
        for n in (yf.Ticker(ticker).news or [])[:maximaal]:
            content = n.get("content", n)   # nieuwere yfinance nest onder 'content'
            titel = content.get("title") or n.get("title")
            if not titel:
                continue
            ts = n.get("providerPublishTime")
            datum = None
            if ts:
                datum = pd.to_datetime(ts, unit="s").date().isoformat()
            elif content.get("pubDate"):
                datum = str(content.get("pubDate"))[:10]
            link = (content.get("canonicalUrl", {}) or {}).get("url") or n.get("link")
            bron = ((content.get("provider", {}) or {}).get("displayName")
                    or n.get("publisher") or "")
            uit.append({"titel": titel, "bron": bron, "datum": datum, "link": link})
    except Exception:
        pass
    return uit


def fx_naar_eur_nu(valuta: str) -> float:
    """Actuele wisselkoers naar EUR; 1.0 als het al EUR is of het ophalen mislukt."""
    if valuta == "EUR":
        return 1.0
    serie = fx_naar_eur_historie(valuta, start=pd.Timestamp.today() - pd.Timedelta(days=7))
    if serie is not None and len(serie):
        return float(serie.iloc[-1])
    return 1.0
