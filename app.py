"""
Portfolio Tracker — Streamlit-app.

Starten:  streamlit run app.py
Daarna opent de app in je browser. Upload links je DEGIRO Transactions.csv
(of gebruik de demo-data om de app te verkennen).

Alle bedragen in de app zijn omgerekend naar EUR.
"""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

try:
    import anthropic
except ImportError:
    anthropic = None

import analyse
import account
import marktdata
from portfolio import verwerk_transacties, koppel_corporate_actions

st.set_page_config(page_title="Portfolio Tracker", page_icon="📈", layout="wide")


# ---------- Multi-user: persoonlijke instellingen per sessie ----------
# Lokaal (één gebruiker) gaan instellingen naar JSON-bestanden, zoals altijd.
# In de cloud draaien meerdere mensen dezelfde app-instantie: dan zouden gedeelde
# JSON-bestanden elkaars watchlist/doelgewichten/strategie/API-keys overschrijven.
# Zet in Streamlit Cloud (app → Settings → Secrets) `multiuser = true` — dan leven
# persoonlijke instellingen alléén in st.session_state (per browsersessie, niets
# op schijf). Gedeelde marktdata-caches (ticker_mapping, info_cache) blijven wel
# gedeeld: dat is geen persoonlijke informatie en delen is efficiënter.

def _is_multiuser() -> bool:
    try:
        return bool(st.secrets.get("multiuser", False))
    except Exception:  # geen secrets.toml aanwezig (lokaal)
        return False


MULTIUSER = _is_multiuser()


def _secret(naam, standaard=None):
    try:
        return st.secrets.get(naam, standaard)
    except Exception:  # geen secrets.toml (lokaal)
        return standaard


# Optie (a): jouw eigen Anthropic-key als backend-key (uit Streamlit Secrets),
# zodat gebruikers de assistent kunnen gebruiken zonder zelf een key in te vullen.
# Bescherm je rekening met een dag-limiet per gebruiker (per browsersessie).
# Zet in Streamlit Secrets:  anthropic_api_key = "sk-ant-..."   en optioneel
# chat_limiet_per_dag = 5 . Zonder backend-key valt de app terug op optie (b):
# elke gebruiker vult zijn eigen key in (dan geen limiet — het is hun rekening).
BACKEND_ANTHROPIC_KEY = str(_secret("anthropic_api_key", "") or "")
try:
    CHAT_LIMIET_PER_DAG = int(_secret("chat_limiet_per_dag", 5) or 5)
except (TypeError, ValueError):
    CHAT_LIMIET_PER_DAG = 5

_PREF_LEZERS = {
    "strategie": marktdata.lees_strategie,
    "doelgewichten": marktdata.lees_doelgewichten,
    "koers_overrides": marktdata.lees_koers_overrides,
    "watchlist": marktdata.lees_watchlist,
    "config": marktdata.lees_config,
}
_PREF_STANDAARD = {"watchlist": list}


def lees_pref(naam: str):
    """Persoonlijke voorkeur: session_state (multi-user) of JSON (lokaal)."""
    if MULTIUSER:
        maak = _PREF_STANDAARD.get(naam, dict)
        return st.session_state.setdefault(f"pref_{naam}", maak())
    return _PREF_LEZERS[naam]()


def pref_zet_strategie(isin: str, strategie: str) -> None:
    if MULTIUSER:
        lees_pref("strategie")[isin] = strategie
    else:
        marktdata.zet_strategie(isin, strategie)


def pref_zet_doelgewichten(gewichten: dict) -> None:
    if MULTIUSER:
        schoon = {i: float(p) for i, p in gewichten.items() if p and float(p) > 0}
        st.session_state["pref_doelgewichten"] = schoon
    else:
        marktdata.zet_doelgewichten(gewichten)


def pref_zet_koers_override(isin: str, koers) -> None:
    if MULTIUSER:
        ov = lees_pref("koers_overrides")
        if koers is None:
            ov.pop(isin, None)
        else:
            ov[isin] = koers
    else:
        marktdata.zet_koers_override(isin, koers)


def pref_voeg_watchlist(ticker: str) -> None:
    if MULTIUSER:
        wl = lees_pref("watchlist")
        if ticker not in wl:
            wl.append(ticker)
    else:
        marktdata.voeg_watchlist(ticker)


def pref_verwijder_watchlist(ticker: str) -> None:
    if MULTIUSER:
        wl = lees_pref("watchlist")
        if ticker in wl:
            wl.remove(ticker)
    else:
        marktdata.verwijder_watchlist(ticker)


def pref_zet_config(sleutel: str, waarde: str) -> None:
    if MULTIUSER:
        lees_pref("config")[sleutel] = waarde
    else:
        marktdata.zet_config(sleutel, waarde)

GROEN = "#4ade80"
ROOD = "#f87171"
GRIJS = "#94a3b8"
ACHTERGROND = "#0b0f1a"
KLEUREN = ["#4ade80", "#60a5fa", "#f472b6", "#fbbf24", "#a78bfa", "#34d399",
           "#fb923c", "#22d3ee", "#e879f9", "#facc15", "#818cf8", "#2dd4bf"]

st.markdown("""
<style>
/* Metric-kaarten */
[data-testid="stMetric"] {
    background: linear-gradient(145deg, #1a2332 0%, #141b29 100%);
    border: 1px solid #263244;
    border-radius: 14px;
    padding: 16px 20px;
}
[data-testid="stMetricLabel"] { color: #8b9bb4; }
[data-testid="stMetricValue"] { font-size: 1.45rem; }

/* Compacte rendement-kaartjes */
.periode-rij { display: flex; gap: 10px; flex-wrap: wrap; }
.periode-kaart {
    flex: 1; min-width: 90px; text-align: center;
    background: linear-gradient(145deg, #1a2332 0%, #141b29 100%);
    border: 1px solid #263244; border-radius: 12px; padding: 12px 8px;
}
.periode-kaart .p-label { color: #8b9bb4; font-size: 0.85rem; margin-bottom: 4px; }
.periode-kaart .p-waarde { font-size: 1.25rem; font-weight: 700; }
.p-groen { color: #4ade80; }
.p-rood { color: #f87171; }
.p-grijs { color: #94a3b8; }

/* Tabbladen */
button[role="tab"] { font-size: 1.05rem; padding: 8px 4px; }
[data-testid="stTabs"] { margin-top: 0.5rem; }

/* Zijbalk */
[data-testid="stSidebar"] { background: #0e1422; border-right: 1px solid #1d2738; }

/* Tabellen */
[data-testid="stDataFrame"] { border: 1px solid #263244; border-radius: 12px; }

/* Koppen */
h1 { font-weight: 800; letter-spacing: -0.5px; }
h3 { color: #cbd5e1; }
</style>
""", unsafe_allow_html=True)


# ---------- Helpers ----------

def euro(bedrag) -> str:
    if bedrag is None or pd.isna(bedrag):
        return "—"
    return f"€ {bedrag:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def euro_delta(bedrag, suffix: str = "") -> str | None:
    """Delta-tekst voor st.metric die MET het teken begint, zodat Streamlit
    groen/rood + pijl correct toont (negatief moet met '-' beginnen)."""
    if bedrag is None or pd.isna(bedrag):
        return None
    teken = "-" if bedrag < 0 else "+"
    return f"{teken}€ {abs(bedrag):,.0f}{suffix}".replace(",", ".")


def euro_kort(bedrag) -> str:
    if bedrag is None or pd.isna(bedrag):
        return "—"
    return f"€ {bedrag:,.0f}".replace(",", ".")


def procent(p) -> str:
    if p is None or pd.isna(p):
        return "—"
    return f"{p:+.1f}%"


def stijl_pos_neg(df_style, kolommen):
    """Groen voor positieve, rood voor negatieve getallen."""
    return df_style.map(
        lambda v: f"color: {GROEN}" if isinstance(v, (int, float)) and not pd.isna(v) and v > 0
        else (f"color: {ROOD}" if isinstance(v, (int, float)) and not pd.isna(v) and v < 0 else ""),
        subset=kolommen,
    )


def maak_donut(df, namen, waarden, titel="", klein_grens=0.03):
    """
    Strakke donut: kleine plakjes (<3%) samengevoegd tot 'Overig',
    totaalbedrag in het midden, donkere randjes tussen de plakjes.
    """
    data = df.groupby(namen)[waarden].sum().sort_values(ascending=False)
    totaal = data.sum()
    klein = data[data / totaal < klein_grens]
    if len(klein) > 1:
        data = data[data / totaal >= klein_grens]
        data.loc["Overig"] = klein.sum()

    fig = go.Figure(go.Pie(
        labels=list(data.index), values=list(data.values),
        hole=0.68, sort=False, direction="clockwise",
        marker=dict(colors=KLEUREN[:len(data)], line=dict(color=ACHTERGROND, width=3)),
        textinfo="percent", textposition="inside",
        insidetextfont=dict(size=13, color="#0b0f1a", family="sans-serif"),
        hovertemplate="<b>%{label}</b><br>€ %{value:,.0f} · %{percent}<extra></extra>",
    ))
    fig.add_annotation(text=f"<b>{euro_kort(totaal)}</b>", font=dict(size=19, color="#e2e8f0"),
                       showarrow=False, y=0.56)
    if titel:
        fig.add_annotation(text=titel, font=dict(size=12, color="#8b9bb4"),
                           showarrow=False, y=0.44)
    fig.update_layout(
        showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.05, x=0.5, xanchor="center",
                    font=dict(size=11.5, color="#cbd5e1")),
        height=380,
        margin=dict(t=20, b=10, l=10, r=10),
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def grafiek_layout(fig, hoogte=420):
    fig.update_layout(
        margin=dict(t=20, b=10), height=hoogte, hovermode="x unified",
        legend=dict(orientation="h", y=1.05),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="#1d2738"), yaxis=dict(gridcolor="#1d2738", tickprefix="€ "),
    )
    return fig


def verdeling_blok(df, dim, titel):
    """Donut + tabel (waarde, gewicht, resultaat) voor één verdeling-dimensie,
    zoals de klikbare verdelingen in PDT (Sector/Industrie/Land/...)."""
    kol1, kol2 = st.columns([2, 3])
    with kol1:
        st.plotly_chart(maak_donut(df, dim, "Waarde (EUR)", titel), use_container_width=True)
    with kol2:
        g = df.groupby(dim).agg(**{
            "Waarde (EUR)": ("Waarde (EUR)", "sum"),
            "Inleg (EUR)": ("Inleg (EUR)", "sum"),
            "Resultaat (EUR)": ("Resultaat (EUR)", "sum"),
            "Posities": ("Product", "count"),
        }).reset_index().sort_values("Waarde (EUR)", ascending=False)
        totaal = g["Waarde (EUR)"].sum()
        g["Gewicht (%)"] = g["Waarde (EUR)"] / totaal * 100 if totaal else None
        g["Resultaat (%)"] = g["Resultaat (EUR)"] / g["Inleg (EUR)"] * 100
        st.dataframe(
            stijl_pos_neg(g[[dim, "Posities", "Waarde (EUR)", "Gewicht (%)",
                             "Resultaat (EUR)", "Resultaat (%)"]].style.format({
                "Waarde (EUR)": "€ {:,.0f}", "Gewicht (%)": "{:.1f}%",
                "Resultaat (EUR)": "€ {:+,.0f}", "Resultaat (%)": "{:+.1f}%",
            }, na_rep="—"), ["Resultaat (EUR)", "Resultaat (%)"]),
            use_container_width=True, hide_index=True,
        )


# ---------- Data laden (met cache zodat het snel blijft) ----------

@st.cache_data(ttl=900, show_spinner="Koersen ophalen...")
def laad_marktdata(isins_en_namen: tuple):
    """Zoek tickers en haal actuele koersen + productinfo op. Cache: 15 min."""
    isin_naar_ticker = {}
    for isin, naam in isins_en_namen:
        isin_naar_ticker[isin] = marktdata.zoek_ticker(isin, naam)
    tickers = [t for t in isin_naar_ticker.values() if t]
    koersen = marktdata.actuele_koersen(tickers)
    valutas = {t: marktdata.valuta_van(t) for t in tickers}
    fx = {v: marktdata.fx_naar_eur_nu(v) for v in set(valutas.values())}
    info = {t: marktdata.product_info(t) for t in tickers}
    return isin_naar_ticker, koersen, valutas, fx, info


@st.cache_data(ttl=900, show_spinner="Koershistorie ophalen...")
def laad_historie(_transacties, cache_sleutel: str, isin_naar_ticker: dict, overrides: dict):
    # GEEN yfinance-splitcorrectie: DEGIRO's aantallen zijn al juist (splits en
    # bonusaandelen staan in de export), en yfinance .splits bevat ook stock-
    # dividenden/herwaarderingen die de aantallen juist zouden corrumperen.
    return analyse.waarde_historie(_transacties, isin_naar_ticker, overrides)


@st.cache_data(ttl=900)
def basis_fx() -> dict[str, float]:
    """Wisselkoersen voor dividend/kasstromen in vreemde valuta (fallback)."""
    return {c: marktdata.fx_naar_eur_nu(c) for c in ("USD", "GBP", "GBp", "CHF")}


@st.cache_data(ttl=3600, show_spinner="Benchmarks ophalen...")
def laad_benchmark(naam: str, start_iso: str):
    return marktdata.index_historie(naam, start_iso)


@st.cache_data(ttl=900, show_spinner="Watchlist-koersen ophalen...")
def watchlist_quotes(tickers: tuple) -> pd.DataFrame:
    """Snapshot per gevolgde ticker: koers, dagmutatie, 52w-bereik, marktkap."""
    rijen = []
    for t in tickers:
        q = marktdata.fast_quote(t)
        koers, vorige, hoog = q.get("koers"), q.get("vorige"), q.get("jaar_hoog")
        rijen.append({
            "Ticker": t,
            "Koers": koers,
            "Valuta": q.get("valuta") or "",
            "Dag (%)": (koers / vorige - 1) * 100 if koers and vorige else None,
            "52w laag": q.get("jaar_laag"),
            "52w hoog": hoog,
            "Tov 52w-hoog (%)": (koers / hoog - 1) * 100 if koers and hoog else None,
            "Earnings": marktdata.earnings_datum(t) or "—",
        })
    return pd.DataFrame(rijen)


@st.cache_data(ttl=3600, show_spinner="Events ophalen...")
def holding_events(holdings: tuple) -> tuple:
    """Voor de kalender: (earnings-tabel, nieuwslijst) van je holdings.
    `holdings` = tuple van (product, ticker)."""
    earnings, nieuws = [], []
    for product, ticker in holdings:
        if not ticker:
            continue
        ed = marktdata.earnings_datum(ticker)
        if ed:
            earnings.append({"Datum": ed, "Product": product, "Ticker": ticker})
        for n in marktdata.bedrijfsnieuws(ticker, maximaal=3):
            n = dict(n, product=product)
            nieuws.append(n)
    earnings_df = pd.DataFrame(earnings).sort_values("Datum") if earnings else pd.DataFrame()
    nieuws.sort(key=lambda x: x.get("datum") or "", reverse=True)
    return earnings_df, nieuws


@st.cache_data(ttl=86400, show_spinner="Dividendgroei ophalen...")
def dividendgroei_tabel(holdings: tuple) -> pd.DataFrame:
    """Per holding de dividendgroei (1/3/5-jaar CAGR) uit yfinance-historie.
    `holdings` = tuple van (product, ticker)."""
    rijen = []
    start = pd.Timestamp.today() - pd.DateOffset(years=11)
    for product, ticker in holdings:
        if not ticker:
            continue
        s = marktdata.dividend_historie(ticker, start=start)
        if s is None or len(s) == 0:
            continue
        per_jaar = s.groupby(s.index.year).sum()
        per_jaar.index = per_jaar.index.astype(int)
        afgesloten = per_jaar[per_jaar.index < pd.Timestamp.today().year]
        laatste = float(afgesloten.iloc[-1]) if len(afgesloten) else None
        rijen.append({
            "Product": product,
            "Laatste jaardiv/aandeel": laatste,
            "1j groei (%)": (analyse.dividendgroei(per_jaar, 1) or 0) * 100
            if analyse.dividendgroei(per_jaar, 1) is not None else None,
            "3j CAGR (%)": (analyse.dividendgroei(per_jaar, 3) or 0) * 100
            if analyse.dividendgroei(per_jaar, 3) is not None else None,
            "5j CAGR (%)": (analyse.dividendgroei(per_jaar, 5) or 0) * 100
            if analyse.dividendgroei(per_jaar, 5) is not None else None,
            "Jaren historie": int(len(per_jaar)),
        })
    return pd.DataFrame(rijen)


# ---------- Sidebar: CSV upload ----------

st.sidebar.title("📈 Portfolio Tracker")
st.sidebar.markdown(
    "Upload je **DEGIRO Account.csv**\n\n"
    "*(DEGIRO: Activiteit → Rekeningoverzicht → ruime periode → Exporteren → CSV)*\n\n"
    "Dit ene bestand bevat álles: transacties, dividend, kosten én stortingen/opnames."
)
account_upload = st.sidebar.file_uploader("Account.csv", type=["csv"])
gebruik_demo = st.sidebar.toggle("Demo-data gebruiken", value=account_upload is None)

if account_upload is not None and not gebruik_demo:
    account_bron, bron = account_upload, "jouw export"
elif gebruik_demo:
    account_bron, bron = Path(__file__).parent / "demo_account.csv", "demo-data"
else:
    st.info("Upload links je DEGIRO Account.csv, of zet de demo-data aan.")
    st.stop()

try:
    account_data = account.lees_account(account_bron, basis_fx())
except Exception as e:
    st.error(f"Kon Account.csv niet lezen: {e}")
    st.stop()

transacties = account_data.transacties
dividend_df = account_data.dividenden
echte_stromen = account_data.kasstromen
dividend_dag = account_data.dividend_per_dag      # netto dividend per dag (EUR)
kosten_dag = account_data.kosten_per_dag          # niet-transactiekosten per dag (EUR, negatief)
if not transacties:
    st.error("Geen transacties gevonden in dit bestand.")
    st.stop()

st.sidebar.caption(f"{len(transacties)} transacties · {len(dividend_df)} dividenden "
                   f"({bron}).")
if MULTIUSER:
    st.sidebar.caption("🔒 **Gastmodus**: je CSV en instellingen (watchlist, doelgewichten, "
                       "strategie, API-keys) leven alleen in deze browsersessie en worden "
                       "nergens opgeslagen.")

st.sidebar.divider()
st.sidebar.caption(
    "⚠️ *Dit is een hulpmiddel, **geen beleggings- of fiscaal advies**. Cijfers zijn "
    "indicatief en kunnen fouten bevatten (o.a. koersen via Yahoo Finance, ±15 min "
    "vertraagd). Controleer belangrijke beslissingen zelf en raadpleeg zo nodig een "
    "professional.*"
)

# ---------- Verwerken ----------

# ISIN-wijzigingen en herboekingen opruimen (Shell, Eurocommercial, e.d.),
# vóór alle verdere verwerking — anders ontstaan neppe verkopen.
transacties, hernoemd = koppel_corporate_actions(transacties)
if hernoemd:
    st.sidebar.caption(f"{len(hernoemd)} ISIN-wijziging(en) samengevoegd "
                       "(bijv. Royal Dutch Shell → Shell plc).")

ruwe_transacties = transacties
unieke_isins = tuple(sorted({(tx.isin, tx.product) for tx in transacties}))
isin_naar_ticker, koersen, valutas, fx, product_info = laad_marktdata(unieke_isins)

posities, verkopen = verwerk_transacties(transacties)

koers_overrides = lees_pref("koers_overrides")
cache_sleutel = f"{len(transacties)}-{sorted(isin_naar_ticker.items())}-{sorted(koers_overrides.items())}"
waarde_df, geprijsde_isins, prijzen_eur, dekking_df = laad_historie(
    transacties, cache_sleutel, isin_naar_ticker, koers_overrides)

# Koershiaten: posities die (deels) zonder marktkoers gewaardeerd zijn.
# 'onopgeloste_hiaten' = nog géén override → waarschuwing. 'gap_df' = alle
# gaps (ook met override) → bewerkbare lijst in de Data-tab.
if not dekking_df.empty and "Gap" in dekking_df.columns:
    # Alleen posities die we ook echt waarderen (in geprijsde_isins) of met een
    # override — zo blijven waardeloze €0-fracties (netto 0) buiten de lijst.
    relevant = dekking_df["ISIN"].isin(geprijsde_isins) | dekking_df["Override (EUR)"].notna()
    gap_df = dekking_df[(dekking_df["Gap"] == True) & relevant].copy()
    onopgeloste_hiaten = dekking_df[(dekking_df["Hiaat"] == True) & relevant].copy()
else:
    gap_df = onopgeloste_hiaten = pd.DataFrame()

# Waarschuw alléén voor posities die je NU nog aanhoudt én die we niet konden
# waarderen (geen ticker én geen transactiekoers). Gesloten posities en
# waardeloze niet-verhandelbare fracties (netto 0) negeren we — die tellen
# toch niet mee en zouden anders onnodig alarm slaan.
niet_gevonden = sorted({
    p.product for p in posities
    if p.aantal > 1e-6 and p.isin not in geprijsde_isins
})
if niet_gevonden:
    st.sidebar.warning(
        "Geen koersdata voor: " + ", ".join(niet_gevonden) +
        ". Deze open posities tellen niet mee in waarde en rendement. "
        "Corrigeer de ticker in de tab **🔧 Data**."
    )
stromen = analyse.geldstromen(transacties, alleen_isins=geprijsde_isins)
# Echte stortingen/opnames komen uit Account.csv. Mocht dat bestand geen
# stortingsregels bevatten, dan valt de Rendement-tab terug op een schatting.
heeft_account = len(echte_stromen) > 0
stortingen = analyse.geschatte_stortingen(
    [tx for tx in transacties if tx.isin in geprijsde_isins])
# Geschatte cashstand door de tijd, zodat dips in de effectenwaarde (na een
# verkoop) zichtbaar worden opgevangen door cash.
cash_hist = (analyse.cash_historie(transacties, echte_stromen, dividend_df, waarde_df.index,
                                   eind_cash=account_data.huidige_cash)
             if not waarde_df.empty else pd.Series(dtype=float))

# Totale accountwaarde (effecten + cash) en de externe geldstromen — op
# module-niveau zodat zowel de header (YTD) als de Prestaties-tab ze gebruiken.
waarde_serie = waarde_df["waarde"] if not waarde_df.empty else pd.Series(dtype=float)
totaal_hist = (waarde_serie + cash_hist) if (len(cash_hist) and not waarde_df.empty) else waarde_serie
externe = echte_stromen if heeft_account else stromen

# Handmatige strategie-tags per positie (zelf in te stellen in de Data-tab).
strategie_map = lees_pref("strategie")


def koers_eur_nu(ticker) -> float | None:
    """Actuele koers in EUR (lokale koers x wisselkoers)."""
    koers = koersen.get(ticker)
    if koers is None:
        return None
    return koers * fx.get(valutas.get(ticker, "EUR"), 1.0)


# Actuele waarde per open positie berekenen — alles in EUR.
rijen = []
for p in posities:
    ticker = isin_naar_ticker.get(p.isin)
    koers_eur = koers_eur_nu(ticker) if ticker else None
    waarde_nu = p.aantal * koers_eur if koers_eur is not None else None
    info = product_info.get(ticker, {}) if ticker else {}
    rijen.append({
        "Product": p.product,
        "ISIN": p.isin,
        "Type": info.get("type", "Onbekend"),
        "Sector": info.get("sector", "Onbekend"),
        "Industrie": info.get("industrie", "Onbekend"),
        "Land": info.get("land", "Onbekend"),
        "Werelddeel": info.get("werelddeel", "Onbekend"),
        "Valuta": valutas.get(ticker, "Onbekend") if ticker else "Onbekend",
        "Beurs": marktdata.beurs_van_ticker(ticker) if ticker else "Onbekend",
        "Marktkap": marktdata.marktkap_categorie(info.get("marktkap_usd")),
        "Strategie": strategie_map.get(p.isin, "Onbekend"),
        "Beta": info.get("beta"),
        "Aantal": p.aantal,
        "GAK (EUR)": p.gak_eur,
        "Koers (EUR)": koers_eur,
        "Waarde (EUR)": waarde_nu,
        "Inleg (EUR)": p.kosten_eur,
        "Resultaat (EUR)": (waarde_nu - p.kosten_eur) if waarde_nu is not None else None,
        "Resultaat (%)": ((waarde_nu / p.kosten_eur - 1) * 100) if waarde_nu and p.kosten_eur else None,
    })
posities_df = pd.DataFrame(rijen)
if not posities_df.empty:
    # Grootste positie bovenaan; posities zonder koersdata onderaan.
    posities_df = posities_df.sort_values("Waarde (EUR)", ascending=False, na_position="last")
    totaal_bekend = posities_df["Waarde (EUR)"].sum(skipna=True)
    posities_df["Gewicht (%)"] = (posities_df["Waarde (EUR)"] / totaal_bekend * 100
                                  if totaal_bekend else None)
    # Kostenbasis-gewicht (PDT art. 234): aandeel van je inleg per positie.
    totaal_inleg_bekend = posities_df["Inleg (EUR)"].sum()
    posities_df["Kostenbasis (%)"] = (posities_df["Inleg (EUR)"] / totaal_inleg_bekend * 100
                                      if totaal_inleg_bekend else None)

totale_waarde = posities_df["Waarde (EUR)"].sum(skipna=True) if not posities_df.empty else 0.0
totale_inleg = posities_df["Inleg (EUR)"].sum() if not posities_df.empty else 0.0
ongerealiseerd = totale_waarde - totale_inleg
gerealiseerd = sum(v.resultaat_eur for v in verkopen)

# Beweging op de laatste koersdag, gesplitst in koersbeweging (rendement) en
# nieuwe inleg (aankoop/verkoop) — een aankoop laat de waarde stijgen maar is
# géén rendement.
totaal_delta = markt_delta = inleg_vandaag = storting_vandaag = None
if not waarde_df.empty and len(waarde_df) >= 2:
    laatste_dag = waarde_df.index[-1]
    totaal_delta = float(waarde_df["waarde"].iloc[-1] - waarde_df["waarde"].iloc[-2])
    inleg_vandaag = float(stromen.get(laatste_dag, 0.0)) if len(stromen) else 0.0
    markt_delta = totaal_delta - inleg_vandaag  # zuivere koersbeweging
    storting_vandaag = float(echte_stromen.get(laatste_dag, 0.0)) if len(echte_stromen) else 0.0

# Totaalresultaat incl. ontvangen dividend en alle kosten.
dividend_totaal = float(dividend_dag.sum()) if len(dividend_dag) else 0.0
kosten_overig_totaal = float(kosten_dag.sum()) if len(kosten_dag) else 0.0   # negatief
totaal_resultaat = ongerealiseerd + gerealiseerd + dividend_totaal + kosten_overig_totaal

# ---------- Vooruitkijkend dividend (gedeeld door de Dividend- en Kalender-tab) ----------
_div_rijen = []
for p in posities:
    ticker = isin_naar_ticker.get(p.isin)
    if not ticker:
        continue
    di = marktdata.dividend_info(ticker)
    rate = di.get("rate") or 0.0
    if rate <= 0:
        continue
    valuta = valutas.get(ticker, "EUR")
    per_jaar_eur = p.aantal * rate * fx.get(valuta, 1.0)
    waarde_eur = koers_eur_nu(ticker)
    waarde_pos = p.aantal * waarde_eur if waarde_eur else None
    kostenbasis_pos = p.aantal * p.gak_eur if p.gak_eur else None
    _div_rijen.append({
        "Product": p.product, "Aantal": p.aantal, "Div/aandeel": rate, "Valuta": valuta,
        "Verwacht/jaar (EUR)": per_jaar_eur,
        "Yield (%)": (per_jaar_eur / waarde_pos * 100) if waarde_pos else None,
        "YoC (%)": (per_jaar_eur / kostenbasis_pos * 100) if kostenbasis_pos else None,
        "Ex-datum": di.get("ex_datum") or "—",
    })
if _div_rijen:
    div_kalender = pd.DataFrame(_div_rijen).sort_values("Verwacht/jaar (EUR)", ascending=False)
    verwacht_totaal = float(div_kalender["Verwacht/jaar (EUR)"].sum())   # = PADI
    portfolio_yield = (verwacht_totaal / totale_waarde * 100) if totale_waarde else None
    portfolio_yoc = (verwacht_totaal / totale_inleg * 100) if totale_inleg else None
else:
    div_kalender = pd.DataFrame()
    verwacht_totaal, portfolio_yield, portfolio_yoc = 0.0, None, None

# ---------- Kop met totalen ----------

st.title("📈 Portfolio Tracker")
laatste_update = waarde_df.index[-1].strftime("%d-%m-%Y") if not waarde_df.empty else "—"
st.caption(f"Koersen via Yahoo Finance (±15 min vertraagd) · alle bedragen in EUR · "
           f"laatste koersdag: {laatste_update}")

# YTD-rendement (money-weighted) voor in de header.
ytd_rend = None
if not totaal_hist.empty:
    _nu = totaal_hist.index[-1]
    ytd_rend = analyse.rendement(totaal_hist, externe,
                                 pd.Timestamp(year=_nu.year, month=1, day=1), _nu, "MWRR")

k1, k2, k3, k4, k5 = st.columns(5)
# k1 — focus op de DAG: koersbeweging sinds gisteren (groen/rood + pijl).
k1.metric("Portfoliowaarde", euro(totale_waarde),
          delta=euro_delta(markt_delta, " vandaag") if markt_delta is not None else None,
          help=f"Waarde van je effecten nu. De pijl = koersbeweging op de laatste koersdag "
               f"({laatste_update}) t.o.v. de dag ervoor — groen = winst, rood = verlies. "
               "Een storting/aankoop telt hier niet als beweging mee.")
k5.metric("📅 Rendement dit jaar (YTD)", procent(ytd_rend * 100) if ytd_rend is not None else "—",
          delta_color="off",
          help="Money-weighted rendement sinds 1 januari (PDT's standaardmethode) — het getal "
               "dat PDT rechtsboven toont.")
# k2/k3 — resultaat t.o.v. je inleg (sinds aankoop).
k2.metric("Totaal resultaat", euro(totaal_resultaat),
          delta=procent(totaal_resultaat / totale_inleg * 100 if totale_inleg else None),
          help="Totale winst/verlies t.o.v. je inleg, sinds je eerste aankoop "
               "(ongerealiseerd + gerealiseerd + dividend − kosten).")
k3.metric("Ongerealiseerd resultaat", euro(ongerealiseerd),
          delta=procent(ongerealiseerd / totale_inleg * 100 if totale_inleg else None),
          help="Winst/verlies t.o.v. je inleg op de posities die je nu nog in bezit hebt "
               "(huidige koerswaarde − wat je ervoor betaalde).")
k4.metric("Gerealiseerd + dividend", euro(gerealiseerd + dividend_totaal),
          help="Gerealiseerd resultaat op verkochte posities plus alle ontvangen dividend "
               "(sinds je eerste transactie).")

# Uitleg + de dag-opsplitsing (koers vs storting/aankoop) als die er was.
dag_uitleg = (f"**Beweging** (pijl bij Portfoliowaarde) is t.o.v. de vorige koersdag; "
              f"**resultaat-%** is t.o.v. je inleg sinds aankoop.")
if inleg_vandaag:
    richting = "bijgekocht" if inleg_vandaag > 0 else "verkocht"
    dag_uitleg += (f" Op {laatste_update} veranderde je waarde {euro(totaal_delta)} = "
                   f"{euro(markt_delta)} koers + {euro(abs(inleg_vandaag))} {richting}.")
if storting_vandaag and storting_vandaag > 0:
    dag_uitleg += f" Je stortte die dag {euro(storting_vandaag)} (geen rendement)."
st.caption(dag_uitleg)

# Melding bij koershiaten: posities die (deels) zonder echte marktkoers
# gewaardeerd zijn (gedelist/overgenomen of aangehouden vóór de koersdata begon).
if not onopgeloste_hiaten.empty:
    namen_hiaat = ", ".join(sorted(onopgeloste_hiaten["Product"].astype(str).unique()))
    st.warning(
        f"⚠️ **Koersdata ontbreekt (deels) voor {len(onopgeloste_hiaten)} aandeel(en):** "
        f"{namen_hiaat}. Deze zijn geschat op basis van je transactieprijzen. Vul de echte "
        "koers handmatig in onder **🔧 Data → Koershiaten** voor een exacte historische waarde."
    )

(tab_portfolio, tab_prestaties, tab_dividend, tab_kalender,
 tab_watchlist, tab_assistent, tab_acties) = st.tabs(
    ["📊 Portfolio", "📈 Prestaties", "🪙 Dividend", "📅 Kalender",
     "👁️ Watchlist", "🤖 Assistent", "⚙️ Acties"]
)
# Sub-tabs onder "Acties" (gebonden aan de tab_acties-container, dus bruikbaar
# in latere top-level with-blokken).
with tab_acties:
    sub_aankopen, sub_verkocht, sub_kosten, sub_data = st.tabs(
        ["🛒 Aankopen", "💰 Verkocht", "💸 Kosten", "🔧 Data"])

# ---------- Tab 1: Portfolio ----------

with tab_portfolio:
    if posities_df.empty:
        st.info("Geen open posities.")
    else:
        # Beweging van vandaag per positie op basis van de KOERS (niet de
        # waarde) — anders telt een bijgekochte positie de aankoop mee als
        # 'stijging'. De koersreeks is onafhankelijk van het aantal stuks.
        stijger, daler = None, None
        if not prijzen_eur.empty and len(prijzen_eur) >= 2:
            bewegingen = {}
            for p in posities:
                if p.isin not in prijzen_eur.columns:
                    continue
                reeks = prijzen_eur[p.isin].dropna()
                if len(reeks) >= 2 and reeks.iloc[-2] > 0:
                    bewegingen[p.product] = (reeks.iloc[-1] / reeks.iloc[-2] - 1) * 100
            if bewegingen:
                stijger = max(bewegingen.items(), key=lambda kv: kv[1])
                daler = min(bewegingen.items(), key=lambda kv: kv[1])

        met_resultaat = posities_df.dropna(subset=["Resultaat (%)"])
        c1, c2, c3 = st.columns(3)
        if len(met_resultaat) >= 1:
            beste = met_resultaat.loc[met_resultaat["Resultaat (%)"].idxmax()]
            slechtste = met_resultaat.loc[met_resultaat["Resultaat (%)"].idxmin()]
            c1.metric("🏆 Beste positie (totaal)", beste["Product"], procent(beste["Resultaat (%)"]))
            c2.metric("📉 Zwakste positie (totaal)", slechtste["Product"],
                      procent(slechtste["Resultaat (%)"]))
        grootste = posities_df.iloc[0]
        c3.metric("⚖️ Grootste positie", grootste["Product"],
                  f"{grootste['Gewicht (%)']:.1f}% van portfolio", delta_color="off")

        c4, c5, c6 = st.columns(3)
        if stijger:
            c4.metric("🚀 Stijger vandaag (koers)", stijger[0], procent(stijger[1]),
                      help="Koersbeweging op de laatste dag, los van bijkopen/verkopen.")
        if daler:
            c5.metric("🪂 Daler vandaag (koers)", daler[0], procent(daler[1]),
                      help="Koersbeweging op de laatste dag, los van bijkopen/verkopen.")
        c6.metric("🧺 Aantal posities", f"{len(posities_df)}",
                  f"waarvan {int((posities_df['Type'] == 'ETF').sum())} ETF('s)", delta_color="off")

        st.subheader("Verdeling")
        st.caption("Klik door de verdelingen — net als in PDT.")
        verdeling = posities_df.dropna(subset=["Waarde (EUR)"])
        DIMENSIES = [
            ("Sector", "per sector"), ("Industrie", "per industrie"),
            ("Marktkap", "marktkap"), ("Werelddeel", "per werelddeel"),
            ("Land", "per land"), ("Valuta", "per valuta"),
            ("Beurs", "per beurs"), ("Type", "effecttype"),
            ("Strategie", "per strategie"),
        ]
        if verdeling.empty:
            st.info("Geen posities met koersdata om te verdelen.")
        else:
            for (dim, titel), vt in zip(DIMENSIES, st.tabs([d for d, _ in DIMENSIES])):
                with vt:
                    verdeling_blok(verdeling, dim, titel)

        st.subheader("Open posities")
        toon = posities_df[["Product", "Aantal", "GAK (EUR)", "Koers (EUR)", "Gewicht (%)",
                            "Kostenbasis (%)", "Waarde (EUR)", "Inleg (EUR)",
                            "Resultaat (EUR)", "Resultaat (%)"]]
        st.dataframe(
            stijl_pos_neg(toon.style.format({
                "Aantal": "{:.0f}",
                "GAK (EUR)": "€ {:.2f}",
                "Koers (EUR)": "€ {:.2f}",
                "Gewicht (%)": "{:.1f}%",
                "Kostenbasis (%)": "{:.1f}%",
                "Waarde (EUR)": "€ {:,.2f}",
                "Inleg (EUR)": "€ {:,.2f}",
                "Resultaat (EUR)": "€ {:+,.2f}",
                "Resultaat (%)": "{:+.1f}%",
            }, na_rep="—"), ["Resultaat (EUR)", "Resultaat (%)"]),
            use_container_width=True, hide_index=True,
            height=min(520, 60 + 36 * len(toon)),
        )

        # Concentratiesignalen (voorheen de Inzichten-tab).
        st.subheader("Signalen")
        signalen = []
        grootste_pos = verdeling.iloc[0]
        if grootste_pos["Gewicht (%)"] > 25:
            signalen.append(("warning",
                f"**Concentratierisico:** {grootste_pos['Product']} is "
                f"{grootste_pos['Gewicht (%)']:.0f}% van je portfolio. "
                "Als vuistregel houden veel beleggers maximaal 10-25% per positie aan."))
        sect = verdeling.groupby("Sector")["Waarde (EUR)"].sum()
        if len(sect):
            top_sector, top_sector_pct = sect.idxmax(), sect.max() / sect.sum() * 100
            if top_sector_pct > 40 and top_sector != "Onbekend":
                signalen.append(("warning",
                    f"**Sectorconcentratie:** {top_sector_pct:.0f}% van je portfolio zit in "
                    f"{top_sector}. Een dip in die sector raakt je dan hard."))
        n_aandelen = int((verdeling["Type"] == "Aandeel").sum())
        if 0 < n_aandelen < 8 and int((verdeling["Type"] == "ETF").sum()) == 0:
            signalen.append(("info",
                f"Je hebt {n_aandelen} losse aandelen en geen breed gespreide ETF's. "
                "Met minder dan ~8-10 aandelen weegt bedrijfsspecifiek nieuws zwaar mee."))
        if not signalen:
            signalen.append(("success", "Geen opvallende concentratierisico's gevonden. 👍"))
        for soort, tekst in signalen:
            getattr(st, soort)(tekst)
        st.caption("Dit zijn vuistregels, geen beleggingsadvies.")

        # ----- Rebalancing-hulp met doelgewichten -----
        st.divider()
        st.subheader("⚖️ Rebalancing")
        st.caption("Stel een doelgewicht (%) per positie in. Ik laat zien hoe ver je nu afwijkt "
                   "en welk bedrag je moet bij-/verkopen om je doelverdeling te bereiken.")
        doelen = lees_pref("doelgewichten")
        with st.form("rebalancing"):
            nieuwe_doelen = {}
            for _, r in verdeling.iterrows():
                rc1, rc2, rc3 = st.columns([3, 1, 1])
                rc1.markdown(f"**{r['Product']}**  \nnu {r['Gewicht (%)']:.1f}%")
                rc2.markdown(f"{euro_kort(r['Waarde (EUR)'])}")
                nieuwe_doelen[r["ISIN"]] = rc3.number_input(
                    "doel %", min_value=0.0, max_value=100.0, step=1.0,
                    value=float(doelen.get(r["ISIN"], round(r["Gewicht (%)"], 1))),
                    key=f"doel_{r['ISIN']}", label_visibility="collapsed",
                )
            som_doel = sum(nieuwe_doelen.values())
            st.caption(f"Som van je doelgewichten: **{som_doel:.1f}%** "
                       f"{'✅' if abs(som_doel - 100) < 0.5 else '⚠️ (streef naar 100%)'}")
            if st.form_submit_button("💾 Doelgewichten opslaan", type="primary"):
                pref_zet_doelgewichten(nieuwe_doelen)
                st.success("Doelgewichten opgeslagen.")
                st.rerun()

        if doelen:
            reb_rijen = []
            for _, r in verdeling.iterrows():
                doel = doelen.get(r["ISIN"])
                if doel is None:
                    continue
                huidig = float(r["Gewicht (%)"])
                verschil_eur = (doel - huidig) / 100 * totale_waarde
                reb_rijen.append({
                    "Product": r["Product"], "Huidig (%)": huidig, "Doel (%)": doel,
                    "Drift (%)": huidig - doel,
                    "Actie (EUR)": verschil_eur,
                })
            if reb_rijen:
                reb_df = pd.DataFrame(reb_rijen).sort_values("Drift (%)", key=abs, ascending=False)
                st.dataframe(
                    stijl_pos_neg(reb_df.style.format({
                        "Huidig (%)": "{:.1f}%", "Doel (%)": "{:.1f}%",
                        "Drift (%)": "{:+.1f}%", "Actie (EUR)": "€ {:+,.0f}",
                    }), ["Drift (%)", "Actie (EUR)"]),
                    use_container_width=True, hide_index=True,
                )
                st.caption("**Actie (EUR)**: positief = bijkopen, negatief = verkopen om op je "
                           "doelgewicht te komen. Op basis van je huidige portfoliowaarde "
                           f"({euro(totale_waarde)}). Geen beleggingsadvies.")

# ---------- Tab 2: Prestaties (rendement, risico, benchmark) ----------

with tab_prestaties:
    if waarde_df.empty:
        st.info("Nog geen koershistorie beschikbaar.")
    else:
        waarde = waarde_df["waarde"]

        st.subheader("Rendement per periode")
        totaal_hist = waarde + cash_hist if len(cash_hist) else waarde
        externe = echte_stromen if heeft_account else stromen
        # Rendement op de TOTALE accountwaarde (effecten + cash) met alleen
        # stortingen/opnames als geldstroom — precies zoals PDT (art. 106/108).
        # Dividend en kosten zitten al in de cash, dus niet apart meegeven.

        # Vaste methode: MWRR (de standaardmethode van Portfolio Dividend Tracker).
        methode = "MWRR"
        prim = analyse.rendement_per_periode(totaal_hist, externe, methode=methode)
        cagr = analyse.cagr_per_periode(totaal_hist, externe, methode=methode)

        def kaart(naam, r):
            if r is None:
                return f'<div class="periode-kaart"><div class="p-label">{naam}</div><div class="p-waarde p-grijs">—</div></div>'
            kleur = "p-groen" if r >= 0 else "p-rood"
            return (f'<div class="periode-kaart"><div class="p-label">{naam}</div>'
                    f'<div class="p-waarde {kleur}">{procent(r * 100)}</div></div>')

        st.markdown("**💶 Rendement (MWRR, money-weighted)** — cumulatief over de periode")
        st.markdown('<div class="periode-rij">' + "".join(kaart(n, r) for n, r in prim.items()) + "</div>",
                    unsafe_allow_html=True)
        st.markdown("**📅 CAGR (geannualiseerd)** — gemiddeld rendement per jaar (alleen vanaf 1 jaar)")
        st.markdown('<div class="periode-rij">' + "".join(kaart(n, r) for n, r in cagr.items()) + "</div>",
                    unsafe_allow_html=True)
        st.caption(
            "Money-weighted (MWRR) op je **totale accountwaarde** (effecten + cash) met "
            "stortingen/opnames als geldstroom — exact PDT's standaardmethode. Voor lange "
            "periodes is de **CAGR** (gemiddeld per jaar) het best vergelijkbare getal."
        )

        st.divider()
        st.subheader("Bekijk een periode")
        eind_datum = waarde.index[-1]
        begin_datum = waarde.index[0]
        presets = {
            "1W": eind_datum - pd.Timedelta(weeks=1),
            "1M": eind_datum - pd.DateOffset(months=1),
            "3M": eind_datum - pd.DateOffset(months=3),
            "6M": eind_datum - pd.DateOffset(months=6),
            "YTD": pd.Timestamp(year=eind_datum.year, month=1, day=1),
            "1J": eind_datum - pd.DateOffset(years=1),
            # Max begint vóór de eerste storting, zodat álle inleg meetelt
            # (anders verdwijnt een storting van vlak vóór je eerste aankoop).
            "Max": analyse.volledige_start(totaal_hist, externe),
        }
        keuze = st.segmented_control("Periode", list(presets) + ["Aangepast"],
                                     default="Max", label_visibility="collapsed")
        if keuze == "Aangepast":
            bereik = st.date_input(
                "Kies een periode",
                value=(begin_datum.date(), eind_datum.date()),
                min_value=begin_datum.date(), max_value=eind_datum.date(),
            )
            if isinstance(bereik, tuple) and len(bereik) == 2:
                start_sel, eind_sel = pd.Timestamp(bereik[0]), pd.Timestamp(bereik[1])
            else:
                st.stop()
        else:
            # Niet terugklemmen naar de eerste koersdag: vóór die dag is de
            # beginwaarde gewoon 0 en telt de eerste aankoop als inleg.
            start_sel = presets.get(keuze or "Max", analyse.volledige_start(totaal_hist, externe))
            eind_sel = eind_datum

        cijfers = analyse.periode_cijfers(waarde, stromen, start_sel, eind_sel,
                                          dividend_dag, kosten_dag)
        if cijfers is None:
            st.info("Te weinig datapunten in deze periode.")
        else:
            r_prim = analyse.rendement(totaal_hist, externe, start_sel, eind_sel, methode)
            r_tw = analyse.twr_rendement(totaal_hist, externe, start_sel, eind_sel)  # tijdgewogen
            r_cagr = analyse.cagr_van(r_prim, start_sel, eind_sel)
            # Secundair getal: CAGR als de periode ≥1 jaar is, anders de TWR.
            if r_cagr is not None:
                secundair = f"{procent(r_cagr * 100)} per jaar (CAGR)"
            elif r_tw is not None:
                secundair = f"{procent(r_tw * 100)} tijdgewogen"
            else:
                secundair = None
            m1, m2, m3 = st.columns(3)
            m1.metric("📈 Rendement (MWRR)",
                      procent(r_prim * 100 if r_prim is not None else None),
                      delta=secundair, delta_color="off",
                      help="Money-weighted rendement over de gekozen periode (PDT's methode). "
                           "Voor periodes langer dan een jaar toont de grijze regel de CAGR "
                           "(gemiddeld per jaar) — het best vergelijkbare getal.")
            m2.metric("💶 Totaalresultaat", euro(cijfers["resultaat"]),
                      delta=(f"w.v. {euro_kort(cijfers['dividend'])} dividend"
                             if cijfers.get("dividend") else None),
                      delta_color="off",
                      help="Koerswinst + ontvangen dividend − kosten in deze periode.")
            m3.metric("Waarde begin → eind",
                      f"{euro_kort(cijfers['begin_waarde'])} → {euro_kort(cijfers['eind_waarde'])}")

            m4, m5, m6 = st.columns(3)
            if heeft_account and len(echte_stromen):
                in_periode = echte_stromen[(echte_stromen.index > start_sel) &
                                           (echte_stromen.index <= eind_sel)]
                gestort_sel = float(in_periode[in_periode > 0].sum())
                opgenomen_sel = float(-in_periode[in_periode < 0].sum())
                netto_sel = gestort_sel - opgenomen_sel
                m4.metric("🏦 Gestort", euro(gestort_sel),
                          delta=f"netto {euro_delta(netto_sel)}" if netto_sel else None,
                          delta_color="off",
                          help="Vers geld van je bank in deze periode (uit Account.csv). "
                               "De grijze regel is netto = gestort − opgenomen.")
                m5.metric("🏧 Opgenomen", euro(opgenomen_sel),
                          help="Geld teruggestort naar je bank in deze periode (uit Account.csv).")
            else:
                gestort_sel = float(stortingen[(stortingen.index > start_sel) &
                                               (stortingen.index <= eind_sel)].sum()) if len(stortingen) else 0.0
                herbelegd = max(cijfers["aankopen"] - gestort_sel, 0.0)
                m4.metric("🏦 Gestort (geschat)", euro(gestort_sel),
                          help="Geschat vers geld van je bank. Afgeleid uit een kassaldo, want "
                               "stortingen staan niet in Transactions.csv. Upload Account.csv "
                               "voor exacte cijfers.")
                m5.metric("🔁 Aankopen w.v. herbelegd", euro(cijfers["aankopen"]),
                          delta=f"{euro_kort(herbelegd)} herbelegd", delta_color="off",
                          help="Totaal gekocht in deze periode. Het herbelegde deel is met "
                               "opbrengst van eerdere verkopen betaald — geen nieuwe storting.")
            m6.metric("💸 Verkopen", euro(cijfers["verkopen"]),
                      help="Totaal verkocht (effecten) in deze periode.")
            st.caption(
                f"Effecten: begin {euro(cijfers['begin_waarde'])} + aankopen "
                f"{euro(cijfers['aankopen'])} − verkopen {euro(cijfers['verkopen'])} + koerswinst "
                f"{euro(cijfers['koerswinst'])} = eindwaarde {euro(cijfers['eind_waarde'])}.  "
                f"Totaalresultaat = koerswinst {euro(cijfers['koerswinst'])} + dividend "
                f"{euro(cijfers['dividend'])} − kosten {euro(abs(cijfers['kosten']))} = "
                f"{euro(cijfers['resultaat'])}."
            )

            # ----- Risico- & prestatie-metrics over de gekozen periode -----
            st.markdown("##### 📐 Risico & prestatie")
            REFERENTIE_BM = "MSCI World"   # PDT's standaard-benchmark (MSCI All World)
            dagrend_port = analyse.dagrendementen(
                totaal_hist[(totaal_hist.index >= start_sel) & (totaal_hist.index <= eind_sel)],
                externe)
            vol = analyse.volatiliteit(dagrend_port)
            shrp = analyse.sharpe(dagrend_port)
            turn = analyse.turnover(transacties, totaal_hist, start_sel, eind_sel)
            twr_idx_sel = analyse.twr_index(
                totaal_hist[(totaal_hist.index >= start_sel) & (totaal_hist.index <= eind_sel)], externe)
            maxdd = analyse.max_drawdown(twr_idx_sel)
            bm_reeks = laad_benchmark(REFERENTIE_BM, str(start_sel.date()))
            bm_reeks = bm_reeks[(bm_reeks.index >= start_sel) & (bm_reeks.index <= eind_sel)].dropna()
            bm_dagrend = bm_reeks.pct_change().dropna()
            b = analyse.beta(dagrend_port, bm_dagrend)
            bm_rend = float(bm_reeks.iloc[-1] / bm_reeks.iloc[0] - 1) if len(bm_reeks) >= 2 else None
            a = analyse.alfa(r_tw, bm_rend)   # tijdgewogen vs index = eerlijke vergelijking
            r1, r2, r3, r4, r5, r6 = st.columns(6)
            r1.metric("📉 Volatiliteit", procent(vol * 100) if vol is not None else "—",
                      delta_color="off",
                      help="Geannualiseerde beweeglijkheid van je dagrendement (std × √252). "
                           "Hoger = grotere schommelingen (PDT art. 226).")
            r2.metric("⚡ Sharpe", f"{shrp:.2f}" if shrp is not None else "—", delta_color="off",
                      help="Rendement per eenheid risico (gem. dagrendement ÷ volatiliteit, "
                           "geannualiseerd). Vuistregel: >1 goed, >2 erg goed.")
            r3.metric(f"β Beta", f"{b:.2f}" if b is not None else "—", delta_color="off",
                      help=f"Gevoeligheid t.o.v. {REFERENTIE_BM}: 1 = beweegt mee, <1 = rustiger, "
                           ">1 = heftiger (PDT art. 222).")
            r4.metric(f"α Alfa", procent(a * 100) if a is not None else "—", delta_color="off",
                      help=f"Je tijdgewogen rendement minus dat van {REFERENTIE_BM} over deze periode. "
                           "Positief = je versloeg de index (PDT art. 214).")
            r5.metric("🌊 Max. drawdown", procent(maxdd * 100) if maxdd is not None else "—",
                      delta_color="off",
                      help="Grootste daling vanaf een top tot het daaropvolgende dal in deze "
                           "periode (tijdgewogen, dus zonder effect van stortingen).")
            r6.metric("🔄 Turnover", procent(turn * 100).replace("+", "") if turn is not None else "—",
                      delta_color="off",
                      help="MIN(verkopen, aankopen) ÷ gemiddelde portfoliowaarde. Hoe actief "
                           "je handelde in deze periode (PDT art. 223).")

            # Underwater-grafiek: hoe diep en hoe lang je onder je vorige top zat.
            dd_serie = analyse.drawdown_serie(twr_idx_sel)
            if len(dd_serie) >= 2:
                with st.expander("🌊 Drawdown door de tijd (underwater)"):
                    figdd = go.Figure(go.Scatter(
                        x=dd_serie.index, y=dd_serie * 100, fill="tozeroy",
                        line=dict(color=ROOD, width=1), fillcolor="rgba(248,113,113,0.25)",
                    ))
                    figdd.update_layout(
                        margin=dict(t=10, b=10), height=260,
                        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                        xaxis=dict(gridcolor="#1d2738"),
                        yaxis=dict(gridcolor="#1d2738", ticksuffix="%", zeroline=True,
                                   zerolinecolor="#3a4a63"),
                    )
                    st.plotly_chart(figdd, use_container_width=True)
                    st.caption("Hoe ver je portefeuille onder de hoogste stand tot dan toe zat. "
                               "Dieper/langer onder water = pijnlijker verlies.")

            masker = (waarde.index >= start_sel) & (waarde.index <= eind_sel)
            selectie = waarde[masker]
            cash_sel = cash_hist[masker] if len(cash_hist) else pd.Series(0.0, index=selectie.index)
            netto_bron = echte_stromen if (heeft_account and len(echte_stromen)) else stortingen
            netto_label = "Netto gestort (cumulatief)" if (heeft_account and len(echte_stromen)) \
                else "Gestort (geschat, cumulatief)"
            gestort_cum = netto_bron.cumsum().reindex(waarde.index, method="ffill").fillna(0.0)
            gestort_lijn = gestort_cum[masker]

            # Gestapeld: effecten (groen) + cash (blauw) = totale accountwaarde.
            # Zo zie je dat een dip in effecten na een verkoop wordt opgevangen
            # door cash, in plaats van dat geld 'verdwijnt'.
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=selectie.index, y=selectie, name="Effecten",
                line=dict(color=GROEN, width=2), stackgroup="acc",
                fillcolor="rgba(74, 222, 128, 0.15)",
            ))
            fig.add_trace(go.Scatter(
                x=cash_sel.index, y=cash_sel, name="Cash (geschat)",
                line=dict(color="#60a5fa", width=1), stackgroup="acc",
                fillcolor="rgba(96, 165, 250, 0.25)",
            ))
            fig.add_trace(go.Scatter(
                x=gestort_lijn.index, y=gestort_lijn, name=netto_label,
                line=dict(color=GRIJS, width=1.5, dash="dash"),
            ))
            st.plotly_chart(grafiek_layout(fig), use_container_width=True)
            st.caption("De groene vlak is je effectenwaarde, het blauwe je geschatte cash. "
                       "Samen vormen ze je totale accountwaarde — bij een verkoop zie je "
                       "effecten dalen en cash stijgen, zodat de top vlak blijft.")

            # ----- Prestatie (%) vs benchmarks -----
            st.markdown("##### 📊 Prestatie vs benchmark")
            keuze_bm = st.multiselect(
                "Vergelijk met", list(marktdata.BENCHMARKS),
                default=["AEX"], label_visibility="collapsed",
            )
            # Tijdgewogen koersprestatie van je EFFECTEN (koop/verkoop ge-de-flowd).
            # Bewust niet de totale accountwaarde + stortingen: die cash-reconstructie
            # gaf losse dagsprongen (artefacten) waardoor de lijn onrealistisch piekte.
            tw_index = analyse.twr_index(waarde, stromen)
            tw_sel = tw_index[(tw_index.index >= start_sel) & (tw_index.index <= eind_sel)]
            perf = go.Figure()
            if len(tw_sel) >= 2:
                basis = tw_sel.iloc[0]
                perf.add_trace(go.Scatter(
                    x=tw_sel.index, y=(tw_sel / basis - 1) * 100, name="Mijn portfolio",
                    line=dict(color=GROEN, width=2.5),
                ))
            for i, bm in enumerate(keuze_bm):
                reeks = laad_benchmark(bm, str(start_sel.date()))
                reeks = reeks[(reeks.index >= start_sel) & (reeks.index <= eind_sel)].dropna()
                if len(reeks) >= 2:
                    perf.add_trace(go.Scatter(
                        x=reeks.index, y=(reeks / reeks.iloc[0] - 1) * 100, name=bm,
                        line=dict(color=KLEUREN[(i + 1) % len(KLEUREN)], width=1.5),
                    ))
            perf.update_layout(
                margin=dict(t=10, b=10), height=380, hovermode="x unified",
                legend=dict(orientation="h", y=1.05),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#1d2738"),
                yaxis=dict(gridcolor="#1d2738", ticksuffix="%", zeroline=True, zerolinecolor="#3a4a63"),
            )
            st.plotly_chart(perf, use_container_width=True)
            st.caption("Prestatie in % vanaf het begin van de gekozen periode (time-weighted), "
                       "naast de geselecteerde indices. Indices in eigen valuta.")

        st.divider()
        st.subheader("Resultaat per jaar")
        per_jaar = analyse.resultaat_per_jaar(waarde, stromen, dividend_dag, kosten_dag,
                                              totaal_hist, externe)
        if not per_jaar.empty:
            grafiek_kolom, tabel_kolom = st.columns([2, 3])
            with grafiek_kolom:
                fig_jaar = go.Figure(go.Bar(
                    x=per_jaar["Jaar"].astype(str), y=per_jaar["Resultaat (EUR)"],
                    marker_color=[GROEN if r >= 0 else ROOD for r in per_jaar["Resultaat (EUR)"]],
                    text=[euro_kort(r) for r in per_jaar["Resultaat (EUR)"]],
                    textposition="outside",
                ))
                fig_jaar.update_xaxes(type="category")
                st.plotly_chart(grafiek_layout(fig_jaar, hoogte=320), use_container_width=True)
            with tabel_kolom:
                st.dataframe(
                    stijl_pos_neg(per_jaar.style.format({
                        "Beginwaarde": "€ {:,.0f}",
                        "Aankopen": "€ {:,.0f}",
                        "Verkopen": "€ {:,.0f}",
                        "Eindwaarde": "€ {:,.0f}",
                        "Resultaat (EUR)": "€ {:+,.0f}",
                        "Rendement (%)": "{:+.1f}%",
                    }, na_rep="—"), ["Resultaat (EUR)", "Rendement (%)"]),
                    use_container_width=True, hide_index=True,
                )
            st.caption("Een jaar met veel **verkopen** (en opgenomen cash) is geen verliesjaar: "
                       "de verkoopopbrengst zit in 'Verkopen', niet in het resultaat. "
                       "Rendement (%) is money-weighted (Modified Dietz).")

        st.divider()
        st.subheader("🧩 Bijdrage aan je resultaat")
        st.caption("Welke posities trokken je totale resultaat (in euro's) en welke drukten het? "
                   "Ongerealiseerd + gerealiseerd + ontvangen dividend, per product.")
        bijdrage = analyse.bijdrage_analyse(posities_df, verkopen, dividend_df)
        if bijdrage.empty:
            st.caption("Nog geen resultaat om toe te rekenen.")
        else:
            top = pd.concat([bijdrage.head(8), bijdrage.tail(8)]).drop_duplicates("Product")
            top = top.sort_values("Totaal")
            figb = go.Figure(go.Bar(
                x=top["Totaal"], y=top["Product"], orientation="h",
                marker_color=[GROEN if v >= 0 else ROOD for v in top["Totaal"]],
                text=[euro_kort(v) for v in top["Totaal"]], textposition="auto",
            ))
            figb.update_layout(
                margin=dict(t=10, b=10, l=10, r=10), height=max(280, 26 * len(top)),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#1d2738", tickprefix="€ ", zeroline=True,
                           zerolinecolor="#3a4a63"),
                yaxis=dict(gridcolor="#1d2738"),
            )
            st.plotly_chart(figb, use_container_width=True)
            with st.expander("Tabel: bijdrage per product"):
                st.dataframe(
                    stijl_pos_neg(bijdrage.style.format({
                        "Ongerealiseerd": "€ {:+,.0f}", "Gerealiseerd": "€ {:+,.0f}",
                        "Dividend": "€ {:+,.0f}", "Totaal": "€ {:+,.0f}",
                    }), ["Ongerealiseerd", "Gerealiseerd", "Dividend", "Totaal"]),
                    use_container_width=True, hide_index=True,
                )

        st.divider()
        st.subheader("🎢 Scenario / stress-test")
        st.caption("Schat de impact van een marktbeweging op je portefeuille via de **beta** per "
                   "positie (hoe heftig een aandeel meebeweegt met de markt). Posities zonder "
                   "bekende beta nemen 1,0 aan. Een benadering, geen voorspelling.")
        schok = st.slider("Marktbeweging", min_value=-50, max_value=30, value=-20, step=5,
                          format="%d%%") / 100
        stress_df, totaal_delta = analyse.stress_test(posities_df, schok)
        if stress_df.empty:
            st.caption("Geen posities met waarde om te stress-testen.")
        else:
            s1, s2, s3 = st.columns(3)
            s1.metric("Huidige waarde", euro(totale_waarde))
            s2.metric("Geschatte verandering", euro(totaal_delta),
                      delta=procent(totaal_delta / totale_waarde * 100) if totale_waarde else None,
                      delta_color="off")
            s3.metric("Waarde na scenario", euro(totale_waarde + totaal_delta), delta_color="off")
            figs = go.Figure(go.Bar(
                x=stress_df["Verwachte verandering (EUR)"], y=stress_df["Product"], orientation="h",
                marker_color=[GROEN if v >= 0 else ROOD for v in stress_df["Verwachte verandering (EUR)"]],
            ))
            figs.update_layout(
                margin=dict(t=10, b=10, l=10, r=10), height=max(260, 24 * len(stress_df)),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#1d2738", tickprefix="€ ", zeroline=True, zerolinecolor="#3a4a63"),
                yaxis=dict(gridcolor="#1d2738"),
            )
            st.plotly_chart(figs, use_container_width=True)

        st.divider()
        st.subheader("🗓️ Maandrendement")
        dagrend_full = analyse.dagrendementen(totaal_hist, externe)
        maand_pivot = analyse.maand_rendement(dagrend_full)
        if maand_pivot.empty:
            st.caption("Nog te weinig historie voor een maandoverzicht.")
        else:
            MAAND_NL = {1: "jan", 2: "feb", 3: "mrt", 4: "apr", 5: "mei", 6: "jun",
                        7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec"}
            kolommen = [c for c in range(1, 13) if c in maand_pivot.columns]
            z = maand_pivot[kolommen].values * 100
            tekst = [[f"{v:+.1f}" if pd.notna(v) else "" for v in rij] for rij in z]
            fig_hm = go.Figure(go.Heatmap(
                z=z, x=[MAAND_NL[c] for c in kolommen],
                y=[str(j) for j in maand_pivot.index],
                text=tekst, texttemplate="%{text}", textfont=dict(size=11, color="#e2e8f0"),
                colorscale=[[0, ROOD], [0.5, "#141b29"], [1, GROEN]], zmid=0,
                showscale=False, xgap=3, ygap=3,
                hovertemplate="%{y} %{x}: %{z:.2f}%<extra></extra>",
            ))
            fig_hm.update_layout(
                margin=dict(t=10, b=10), height=70 + 34 * len(maand_pivot),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(side="top", fixedrange=True),
                yaxis=dict(autorange="reversed", fixedrange=True),
            )
            st.plotly_chart(fig_hm, use_container_width=True)
            st.caption("Tijdgewogen rendement per maand (%) — groen positief, rood negatief. "
                       "Handig om seizoenspatronen en uitschieters te spotten.")

        st.divider()
        st.subheader("🕰️ Tijdmachine")
        st.caption("Kies een datum en zie wat je toen in bezit had — en wat dat mandje "
                   "nú waard zou zijn als je sindsdien niets had gedaan.")
        datum_keuze = st.slider(
            "Datum", min_value=begin_datum.date(), max_value=eind_datum.date(),
            value=(begin_datum + (eind_datum - begin_datum) / 2).date(),
            format="DD-MM-YYYY", label_visibility="collapsed",
        )
        tm = analyse.tijdmachine(transacties, prijzen_eur, pd.Timestamp(datum_keuze))
        if tm.empty:
            st.info("Op deze datum had je nog geen posities (met koersdata).")
        else:
            waarde_toen = tm["Waarde toen (EUR)"].sum()
            watif_nu = tm["Waarde nu indien vastgehouden (EUR)"].sum()
            t1, t2, t3 = st.columns(3)
            t1.metric(f"Waarde op {datum_keuze.strftime('%d-%m-%Y')}", euro(waarde_toen))
            t2.metric("Nú waard als je had vastgehouden", euro(watif_nu),
                      procent((watif_nu / waarde_toen - 1) * 100 if waarde_toen else None))
            t3.metric("Werkelijke portfoliowaarde nu", euro(totale_waarde), delta_color="off",
                      help="Let op: niet 1-op-1 vergelijkbaar met het wat-als-bedrag, "
                           "want je hebt sindsdien mogelijk geld in- of uitgelegd.")
            st.dataframe(
                stijl_pos_neg(tm.style.format({
                    "Aantal toen": "{:.0f}",
                    "Koers toen (EUR)": "€ {:.2f}",
                    "Waarde toen (EUR)": "€ {:,.2f}",
                    "Koers nu (EUR)": "€ {:.2f}",
                    "Waarde nu indien vastgehouden (EUR)": "€ {:,.2f}",
                    "Sindsdien (%)": "{:+.1f}%",
                }, na_rep="—"), ["Sindsdien (%)"]),
                use_container_width=True, hide_index=True,
            )

        with st.expander("Waarde per product door de tijd"):
            product_kolommen = [c for c in waarde_df.columns if c != "waarde"]
            laatste = waarde_df[product_kolommen].iloc[-1].sort_values(ascending=False)
            top = list(laatste.head(8).index)
            rest = [c for c in product_kolommen if c not in top]
            plot_df = waarde_df[top].copy()
            if rest:
                plot_df["Overig"] = waarde_df[rest].sum(axis=1)
            modus = st.radio("Weergave", ["Absoluut (€)", "Aandeel (%)"],
                             horizontal=True, label_visibility="collapsed", key="prod_modus")
            data = plot_df
            if modus.startswith("Aandeel"):
                rijtot = plot_df.sum(axis=1).replace(0, pd.NA)
                data = plot_df.div(rijtot, axis=0) * 100
            fig2 = go.Figure()
            for i, c in enumerate(data.columns):
                fig2.add_trace(go.Scatter(
                    x=data.index, y=data[c], name=c, mode="lines",
                    line=dict(width=0.5, color=KLEUREN[i % len(KLEUREN)]), stackgroup="prod",
                    hovertemplate=("%{fullData.name}: € %{y:,.0f}<extra></extra>"
                                   if modus.startswith("Absoluut")
                                   else "%{fullData.name}: %{y:.1f}%<extra></extra>"),
                ))
            fig2.update_layout(
                margin=dict(t=10, b=10), height=480, hovermode="x unified",
                legend=dict(orientation="h", y=-0.18, font=dict(size=11)),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#1d2738"),
                yaxis=dict(gridcolor="#1d2738",
                           tickprefix="€ " if modus.startswith("Absoluut") else "",
                           ticksuffix="" if modus.startswith("Absoluut") else "%"),
            )
            st.plotly_chart(fig2, use_container_width=True)
            st.caption("Gestapelde waarde per product (grootste 8 + 'Overig'). Beweeg over de "
                       "grafiek voor álle waarden op die dag — ook producten die op €0 staan. "
                       "Wissel naar 'Aandeel (%)' om de verschuiving in samenstelling te zien.")

# ---------- Tab: Dividend ----------

with tab_dividend:
    # 1. Vooruitkijkend overzicht (uit yfinance), op basis van je actuele posities.
    st.subheader("Verwacht dividend (komende 12 maanden)")
    st.caption("Schatting op basis van het huidige dividend per aandeel (Yahoo Finance) "
               "en je actuele posities. ETF's en groeiaandelen keren vaak niets uit.")
    if not div_kalender.empty:
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("💰 PADI (verwacht dividend/jaar)", euro(verwacht_totaal),
                  delta=f"≈ {euro_kort(verwacht_totaal / 12)}/maand", delta_color="off",
                  help="Projected Annual Dividend Income: het verwachte jaarlijkse dividend van "
                       "je huidige posities (Σ aantal × verwacht dividend per aandeel) — PDT art. 293.")
        d2.metric("📈 Portfolio-yield", procent(portfolio_yield) if portfolio_yield else "—",
                  delta_color="off",
                  help="Verwacht dividend ÷ huidige portfoliowaarde (PDT art. 244).")
        d3.metric("🎯 Yield on Cost", procent(portfolio_yoc) if portfolio_yoc else "—",
                  delta_color="off",
                  help="Verwacht dividend ÷ je totale inleg (kostenbasis). Laat zien wat je "
                       "dividend oplevert op de prijs die je ooit betaalde (PDT art. 244).")
        d4.metric("🧮 Posities met dividend", f"{len(div_kalender)}", delta_color="off")
        st.dataframe(
            div_kalender.style.format({
                "Aantal": "{:.0f}", "Div/aandeel": "{:.2f}",
                "Verwacht/jaar (EUR)": "€ {:,.2f}", "Yield (%)": "{:.1f}%", "YoC (%)": "{:.1f}%",
            }, na_rep="—"),
            use_container_width=True, hide_index=True,
        )
        st.caption("De maand-voor-maand kalender en de PADI-projectie vind je onder de tab "
                   "**📅 Kalender**.")
    else:
        st.info("Geen van je huidige posities keert (volgens Yahoo) dividend uit.")

    # 2. Ontvangen dividend (uit Account.csv).
    st.divider()
    st.subheader("Ontvangen dividend")
    if dividend_df.empty:
        st.info("Geen dividend gevonden in je Account.csv.")
    else:
        totaal_bruto = dividend_df["Bruto (EUR)"].sum()
        totaal_belasting = dividend_df["Belasting (EUR)"].sum()
        totaal_netto = dividend_df["Netto (EUR)"].sum()
        div_jaar = analyse.dividend_per_jaar(dividend_df)
        groei = analyse.dividendgroei(div_jaar)
        o1, o2, o3, o4 = st.columns(4)
        o1.metric("Totaal ontvangen (netto)", euro(totaal_netto))
        o2.metric("Bruto", euro(totaal_bruto))
        o3.metric("Ingehouden belasting", euro(totaal_belasting))
        o4.metric("📊 Dividendgroei (3-jr CAGR)", procent(groei * 100) if groei is not None else "—",
                  delta_color="off",
                  help="Samengestelde jaarlijkse groei van je ontvangen dividend over de laatste "
                       "afgesloten jaren (PDT art. 245). Let op: dit beweegt mee met aankopen/"
                       "verkopen, dus het weerspiegelt ook veranderingen in je portefeuille.")

        div = dividend_df.copy()
        div["Jaar"] = pd.to_datetime(div["Datum"]).dt.year
        per_jaar_div = div.groupby("Jaar")["Netto (EUR)"].sum().reset_index()
        linker, rechter = st.columns([2, 3])
        with linker:
            cum = per_jaar_div["Netto (EUR)"].cumsum()
            figd = go.Figure()
            figd.add_trace(go.Bar(
                x=per_jaar_div["Jaar"].astype(str), y=per_jaar_div["Netto (EUR)"],
                name="Per jaar", marker_color=GROEN,
                text=[euro_kort(v) for v in per_jaar_div["Netto (EUR)"]], textposition="outside",
            ))
            figd.add_trace(go.Scatter(
                x=per_jaar_div["Jaar"].astype(str), y=cum, name="Cumulatief",
                mode="lines+markers", line=dict(color="#fbbf24", width=2.5),
            ))
            figd.update_xaxes(type="category")
            figd.update_layout(legend=dict(orientation="h", y=1.12))
            st.plotly_chart(grafiek_layout(figd, hoogte=320), use_container_width=True)
        with rechter:
            per_product_div = div.groupby("Product")["Netto (EUR)"].sum().sort_values(
                ascending=False).reset_index()
            st.dataframe(
                per_product_div.style.format({"Netto (EUR)": "€ {:,.2f}"}),
                use_container_width=True, hide_index=True, height=300,
            )

        st.markdown("##### Dividend per kalendermaand (seizoen)")
        _MND = {1: "jan", 2: "feb", 3: "mrt", 4: "apr", 5: "mei", 6: "jun",
                7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec"}
        div["Maand"] = pd.to_datetime(div["Datum"]).dt.month
        per_maand = div.groupby("Maand")["Netto (EUR)"].sum().reindex(range(1, 13), fill_value=0.0)
        figm = go.Figure(go.Bar(
            x=[_MND[m] for m in range(1, 13)], y=per_maand.values, marker_color="#60a5fa",
            text=[euro_kort(v) if v else "" for v in per_maand.values], textposition="outside",
        ))
        st.plotly_chart(grafiek_layout(figm, hoogte=280), use_container_width=True)
        st.caption("Totaal ontvangen netto dividend per kalendermaand (alle jaren opgeteld) — "
                   "laat zien in welke maanden je doorgaans het meeste binnenkrijgt.")

        st.markdown("##### Recente uitkeringen")
        st.dataframe(
            dividend_df.head(50).style.format({
                "Bruto (EUR)": "€ {:,.2f}", "Belasting (EUR)": "€ {:,.2f}", "Netto (EUR)": "€ {:,.2f}",
            }),
            use_container_width=True, hide_index=True,
        )

    # 3. Dividendgroei per aandeel (1/3/5-jaar CAGR uit yfinance-historie).
    st.divider()
    st.subheader("📈 Dividendgroei per aandeel")
    st.caption("Samengestelde jaarlijkse groei van het dividend per aandeel (uit Yahoo-historie). "
               "Hoog én stabiel = een sterke dividendgroeier.")
    holdings = tuple((p.product, isin_naar_ticker.get(p.isin))
                     for p in posities if isin_naar_ticker.get(p.isin))
    groei_df = dividendgroei_tabel(holdings)
    if groei_df.empty:
        st.info("Geen dividendhistorie gevonden voor je huidige posities.")
    else:
        st.dataframe(
            stijl_pos_neg(groei_df.style.format({
                "Laatste jaardiv/aandeel": "{:.3f}", "1j groei (%)": "{:+.1f}%",
                "3j CAGR (%)": "{:+.1f}%", "5j CAGR (%)": "{:+.1f}%", "Jaren historie": "{:.0f}",
            }, na_rep="—"), ["1j groei (%)", "3j CAGR (%)", "5j CAGR (%)"]),
            use_container_width=True, hide_index=True,
        )

    # 5. Dividendveiligheid-score per aandeel.
    st.divider()
    st.subheader("🛡️ Dividendveiligheid")
    st.caption("Heuristische inschatting van hoe houdbaar het dividend is — op basis van payout "
               "ratio, dividendgroei (3-jr) en yield. Hulpmiddel, geen garantie of advies.")
    groei_lookup = {}
    if not groei_df.empty:
        for _, r in groei_df.iterrows():
            g = r.get("3j CAGR (%)")
            groei_lookup[r["Product"]] = (g / 100) if g is not None and not pd.isna(g) else None

    def _yield_frac(y):  # yfinance geeft yield soms als fractie (0.03), soms als % (3.0)
        if y is None or pd.isna(y):
            return None
        y = float(y)
        return y / 100 if y > 1 else y

    veilig = []
    for p in posities:
        t = isin_naar_ticker.get(p.isin)
        info = product_info.get(t, {}) if t else {}
        payout, yld = info.get("payout_ratio"), _yield_frac(info.get("div_yield"))
        if not (payout or yld):
            continue
        score, label, reden = analyse.dividend_veiligheid_score(
            payout, groei_lookup.get(p.product), yld)
        veilig.append({"Product": p.product, "Score": score, "Oordeel": label,
                       "Payout (%)": payout * 100 if payout else None,
                       "Yield (%)": yld * 100 if yld else None, "Toelichting": reden})
    if veilig:
        vdf = pd.DataFrame(veilig).sort_values("Score", ascending=False)
        st.dataframe(
            vdf.style.format({"Score": "{:.0f}", "Payout (%)": "{:.0f}%",
                              "Yield (%)": "{:.1f}%"}, na_rep="—"),
            use_container_width=True, hide_index=True,
        )
        st.caption("Score 75-100 = **Sterk**, 50-74 = **Redelijk**, <50 = **Zwak**. "
                   "Payout ratio = welk deel van de winst als dividend wordt uitgekeerd "
                   "(hoger = minder marge).")
    else:
        st.caption("Geen payout/yield-data gevonden voor je dividend-posities.")

    # 4. Dividendbelasting terugvragen (NL wet- en regelgeving).
    if not dividend_df.empty:
        st.divider()
        st.subheader("🧾 Dividendbelasting terugvragen")
        bel = dividend_df.copy()
        bel["NL"] = bel["ISIN"].astype(str).str.startswith("NL")
        nl_belasting = -float(bel[bel["NL"]]["Belasting (EUR)"].sum())     # ingehouden = negatief
        buit_belasting = -float(bel[~bel["NL"]]["Belasting (EUR)"].sum())
        b1, b2, b3 = st.columns(3)
        b1.metric("🇳🇱 NL dividendbelasting", euro(nl_belasting),
                  help="15% Nederlandse dividendbelasting. Volledig verrekenbaar met je "
                       "inkomstenbelasting via je aangifte.")
        b2.metric("🌍 Buitenlandse bronbelasting", euro(buit_belasting),
                  help="In het buitenland ingehouden dividendbelasting. Tot het verdragstarief "
                       "(meestal 15%) verrekenbaar/terug te vragen.")
        b3.metric("Totaal ingehouden", euro(nl_belasting + buit_belasting), delta_color="off")
        st.info(
            "**Vuistregel (geen fiscaal advies):** de **Nederlandse 15%** dividendbelasting "
            "krijg je terug/verrekend via je aangifte inkomstenbelasting (het is een voorheffing). "
            "**Buitenlandse** bronbelasting is tot het verdragstarief (vaak 15%) te verrekenen; het "
            "deel bóven het verdragstarief vraag je terug bij de fiscus van dat land. Laat je voor "
            "je eigen situatie informeren door de Belastingdienst of een fiscalist."
        )

# ---------- Tab: Kalender (vooruitkijkend dividend + PADI-projectie) ----------

with tab_kalender:
    st.subheader("📅 Dividendkalender")
    if div_kalender.empty:
        st.info("Geen verwachte dividenden (volgens Yahoo) voor je huidige posities.")
    else:
        k1, k2, k3 = st.columns(3)
        k1.metric("PADI (verwacht/jaar)", euro(verwacht_totaal))
        k2.metric("Gemiddeld per maand", euro(verwacht_totaal / 12))
        k3.metric("Portfolio-yield", procent(portfolio_yield) if portfolio_yield else "—",
                  delta_color="off")

        # Yahoo's ex-dividenddatum is soms de eerstvolgende, maar vaak de laatst
        # bekende (in het verleden). Splits daarom expliciet op vandaag.
        ed = div_kalender[div_kalender["Ex-datum"] != "—"].copy()
        ed["_dt"] = pd.to_datetime(ed["Ex-datum"], errors="coerce")
        ed = ed.dropna(subset=["_dt"])
        vandaag_ts = pd.Timestamp.today().normalize()
        kol = ["Ex-datum", "Product", "Aantal", "Verwacht/jaar (EUR)", "Yield (%)"]
        fmt = {"Aantal": "{:.0f}", "Verwacht/jaar (EUR)": "€ {:,.2f}", "Yield (%)": "{:.1f}%"}

        aankomend = ed[ed["_dt"] >= vandaag_ts].sort_values("_dt")
        st.markdown("##### Aankomende ex-dividenddatums")
        if not aankomend.empty:
            st.dataframe(aankomend[kol].style.format(fmt, na_rep="—"),
                         use_container_width=True, hide_index=True)
        else:
            st.caption("Geen aankomende ex-dividenddatums bekend bij Yahoo "
                       "(Yahoo geeft vaak alleen de laatst gepasseerde datum door).")

        verleden = ed[ed["_dt"] < vandaag_ts].sort_values("_dt", ascending=False)
        if not verleden.empty:
            st.markdown("##### Laatst gepasseerde ex-dividenddatums")
            st.dataframe(verleden[kol].style.format(fmt, na_rep="—"),
                         use_container_width=True, hide_index=True)

        # PADI-projectie: hoe groeit je dividendinkomen als je blijft inleggen?
        st.divider()
        st.subheader("🔮 PADI-projectie")
        st.caption(
            "Schat hoe je jaarlijkse dividendinkomen (PADI) groeit. Formule (PDT art. 293): "
            "*volgend jaar = dit jaar × (1 + groei) + maandinleg × 12 × yield*."
        )
        groei_div = analyse.dividendgroei(analyse.dividend_per_jaar(dividend_df)) \
            if not dividend_df.empty else None
        p1, p2, p3 = st.columns(3)
        maand_inleg = p1.number_input("Maandelijkse inleg (EUR)", min_value=0.0, value=250.0, step=50.0)
        groei_default = float(min(max((groei_div * 100) if groei_div and groei_div > 0 else 5.0, 0.0), 30.0))
        groei_pct = p2.number_input("Verwachte dividendgroei (%/jaar)", min_value=0.0,
                                    max_value=30.0, value=round(groei_default, 1), step=0.5)
        jaren_vooruit = p3.slider("Jaren vooruit", min_value=1, max_value=30, value=10)
        yld = (portfolio_yield / 100) if portfolio_yield else 0.0
        proj = analyse.padi_projectie(verwacht_totaal, groei_pct / 100, maand_inleg, yld, jaren_vooruit)
        eind_padi = proj["PADI (EUR)"].iloc[-1]
        g1, g2 = st.columns([1, 2])
        g1.metric(f"PADI over {jaren_vooruit} jaar", euro(eind_padi),
                  delta=f"≈ {euro_kort(eind_padi / 12)}/maand", delta_color="off")
        figp = go.Figure(go.Bar(x=proj["Jaar"], y=proj["PADI (EUR)"], marker_color=GROEN))
        figp.update_layout(
            margin=dict(t=10, b=10), height=300,
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(title="Jaar vanaf nu", gridcolor="#1d2738"),
            yaxis=dict(gridcolor="#1d2738", tickprefix="€ "),
        )
        g2.plotly_chart(figp, use_container_width=True)
        st.caption("Aanname: je herbelegt dividend en blijft maandelijks inleggen tegen je "
                   "huidige portfolio-yield. Een schatting, geen garantie.")

    # ----- Bedrijfsevents + nieuws van je eigen holdings -----
    _holdings_ev = tuple((p.product, isin_naar_ticker.get(p.isin))
                         for p in posities if isin_naar_ticker.get(p.isin))
    earnings_df, nieuws = holding_events(_holdings_ev)

    st.divider()
    st.subheader("🏢 Aankomende bedrijfsevents (earnings)")
    if earnings_df.empty:
        st.caption("Geen earnings-datums gevonden bij Yahoo voor je holdings.")
    else:
        vandaag = pd.Timestamp.today().normalize()
        e = earnings_df.copy()
        e["wanneer"] = pd.to_datetime(e["Datum"], errors="coerce")
        komend = e[e["wanneer"] >= vandaag].sort_values("wanneer")
        toon_e = komend if not komend.empty else e.sort_values("wanneer")
        st.dataframe(toon_e[["Datum", "Product", "Ticker"]],
                     use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("📰 Bedrijfsnieuws van je holdings")
    if not nieuws:
        st.caption("Geen recent nieuws gevonden bij Yahoo voor je holdings.")
    else:
        for n in nieuws[:25]:
            titel, link = n.get("titel"), n.get("link")
            kop = f"[{titel}]({link})" if link else titel
            st.markdown(f"- **{n.get('product','')}** · {n.get('datum') or ''} · "
                        f"*{n.get('bron') or ''}*  \n  {kop}")

    st.divider()
    st.subheader("🌍 Macro-economische events")
    _fmp_key = lees_pref("config").get("fmp_api_key", "")
    macro = marktdata.macro_events_api(_fmp_key)
    macro_bron = "live via Financial Modeling Prep" if macro else "indicatieve lijst (bewerkbaar)"
    if not macro:
        macro = marktdata.lees_macro_curated()
    if macro:
        mdf = pd.DataFrame(macro).sort_values("datum")
        st.dataframe(mdf, use_container_width=True, hide_index=True)
    st.caption(f"Bron: {macro_bron}. Voor een **live** agenda (ECB/Fed/CPI/banenrapporten) plak je "
               "hieronder een gratis API-key van financialmodelingprep.com. Zonder key toon ik de "
               "bewerkbare lijst uit `macro_events.json` (pas die gerust zelf aan).")
    with st.expander("🔑 Live macro-agenda koppelen (gratis API-key)"):
        key_in = st.text_input("Financial Modeling Prep API-key", value=_fmp_key,
                               type="password", key="fmp_key_in")
        if st.button("Key opslaan", key="fmp_key_btn"):
            pref_zet_config("fmp_api_key", key_in.strip())
            st.cache_data.clear()
            st.success("API-key opgeslagen. De live macro-agenda wordt nu opgehaald.")
            st.rerun()


# ---------- Tab: Watchlist ----------

with tab_watchlist:
    st.subheader("👁️ Watchlist")
    st.caption("Volg aandelen die je (nog) niet bezit. Voeg toe met hun Yahoo-ticker "
               "(bijv. `AAPL`, `ASML.AS`, `SHELL.L`).")
    wl = lees_pref("watchlist")
    wc1, wc2 = st.columns([4, 1])
    nieuw_wl = wc1.text_input("Ticker toevoegen", key="wl_add",
                              placeholder="bijv. NVDA of ADYEN.AS", label_visibility="collapsed")
    if wc2.button("➕ Toevoegen", key="wl_add_btn", use_container_width=True) and nieuw_wl.strip():
        pref_voeg_watchlist(nieuw_wl)
        st.rerun()

    if not wl:
        st.info("Je watchlist is nog leeg. Voeg hierboven een ticker toe.")
    else:
        wdf = watchlist_quotes(tuple(wl))
        st.dataframe(
            stijl_pos_neg(wdf.style.format({
                "Koers": "{:.2f}", "Dag (%)": "{:+.1f}%",
                "52w laag": "{:.2f}", "52w hoog": "{:.2f}", "Tov 52w-hoog (%)": "{:+.1f}%",
            }, na_rep="—"), ["Dag (%)", "Tov 52w-hoog (%)"]),
            use_container_width=True, hide_index=True,
        )
        kies = st.selectbox("Koersgrafiek (1 jaar)", wl, key="wl_chart")
        reeks = marktdata.koers_historie([kies], start=pd.Timestamp.today() - pd.DateOffset(years=1))
        if not reeks.empty:
            kol = kies if kies in reeks.columns else reeks.columns[0]
            serie = reeks[kol].dropna()
            if len(serie) >= 2:
                kleur = GROEN if serie.iloc[-1] >= serie.iloc[0] else ROOD
                figw = go.Figure(go.Scatter(x=serie.index, y=serie, line=dict(color=kleur, width=2)))
                figw.update_layout(margin=dict(t=10, b=10), height=320,
                                   paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                   xaxis=dict(gridcolor="#1d2738"), yaxis=dict(gridcolor="#1d2738"))
                st.plotly_chart(figw, use_container_width=True)
        weg = st.selectbox("Verwijderen uit watchlist", ["—"] + wl, key="wl_del")
        if st.button("🗑️ Verwijderen", key="wl_del_btn") and weg != "—":
            pref_verwijder_watchlist(weg)
            st.rerun()
        st.caption("Koersen ±15 min vertraagd (Yahoo Finance). Earnings-datum waar beschikbaar.")


# ---------- Tab: Assistent (chat met je portfolio via Claude) ----------

def _portfolio_context() -> str:
    """Compacte tekstsnapshot van de portefeuille als context voor de assistent."""
    d = [f"Datum laatste koersdag: {laatste_update}",
         f"Portfoliowaarde (effecten): {euro(totale_waarde)}",
         f"Cash: {euro(account_data.huidige_cash)}",
         f"Totaal resultaat: {euro(totaal_resultaat)} "
         f"(ongerealiseerd {euro(ongerealiseerd)}, gerealiseerd {euro(gerealiseerd)}, "
         f"dividend {euro(dividend_totaal)}, kosten {euro(kosten_overig_totaal)})"]
    if ytd_rend is not None:
        d.append(f"YTD-rendement (money-weighted): {ytd_rend * 100:.1f}%")
    if len(echte_stromen):
        d.append(f"Totaal gestort: {euro(float(echte_stromen[echte_stromen > 0].sum()))}, "
                 f"opgenomen: {euro(float(-echte_stromen[echte_stromen < 0].sum()))}")
    if verwacht_totaal:
        d.append(f"Verwacht dividend/jaar (PADI): {euro(verwacht_totaal)}; "
                 f"portfolio-yield {procent(portfolio_yield) if portfolio_yield else '—'}")
    if not posities_df.empty:
        d.append("\nOpen posities — product | aantal | waarde | gewicht% | resultaat€ | "
                 "resultaat% | sector | land | GAK:")
        for _, r in posities_df.iterrows():
            res = euro_kort(r["Resultaat (EUR)"]) if pd.notna(r["Resultaat (EUR)"]) else "—"
            rpct = f"{r['Resultaat (%)']:.0f}%" if pd.notna(r["Resultaat (%)"]) else "—"
            d.append(f"- {r['Product']} | {r['Aantal']:.0f} | {euro_kort(r['Waarde (EUR)'])} | "
                     f"{r['Gewicht (%)']:.1f}% | {res} | {rpct} | {r['Sector']} | "
                     f"{r['Land']} | {euro_kort(r['GAK (EUR)'])}")
    djaar = analyse.dividend_per_jaar(dividend_df)
    if len(djaar):
        d.append("\nNetto dividend per jaar: "
                 + ", ".join(f"{j}: {euro_kort(b)}" for j, b in djaar.items()))
    return "\n".join(d)


with tab_assistent:
    st.subheader("🤖 Chat met je portfolio")
    st.caption("Stel vragen over je eigen portefeuille — bv. *welke positie drukt mijn "
               "rendement?*, *hoeveel dividend verwacht ik?*, *ben ik te geconcentreerd?* "
               "Antwoorden zijn gebaseerd op je geüploade data. Geen beleggingsadvies.")
    _eigen_key = lees_pref("config").get("anthropic_api_key", "")
    _key = BACKEND_ANTHROPIC_KEY or _eigen_key
    _via_backend = bool(BACKEND_ANTHROPIC_KEY)   # jouw key → dag-limiet toepassen

    if anthropic is None:
        st.error("De `anthropic`-bibliotheek is niet geïnstalleerd in de venv "
                 "(`.venv\\Scripts\\python.exe -m pip install anthropic`).")
    elif not _key:
        # Optie (b): geen backend-key ingesteld → gebruiker vult eigen key in.
        st.info("Plak je Anthropic API-key (van console.anthropic.com) om de assistent te activeren.")
        with st.expander("🔑 API-key instellen"):
            k = st.text_input("Anthropic API-key", type="password", key="anthropic_key_in")
            if st.button("Opslaan", key="anthropic_key_btn") and k.strip():
                pref_zet_config("anthropic_api_key", k.strip())
                st.success("Opgeslagen.")
                st.rerun()
    else:
        # Dag-limiet: alleen als we JOUW backend-key gebruiken (elke vraag kost jou geld).
        vandaag = pd.Timestamp.today().date().isoformat()
        teller = st.session_state.setdefault("chat_teller", {})
        gebruikt = teller.get(vandaag, 0)
        over_limiet = _via_backend and gebruikt >= CHAT_LIMIET_PER_DAG
        if _via_backend:
            st.caption(f"🎟️ Nog **{max(0, CHAT_LIMIET_PER_DAG - gebruikt)}** van "
                       f"{CHAT_LIMIET_PER_DAG} vragen vandaag.")

        if "chat_historie" not in st.session_state:
            st.session_state.chat_historie = []
        for m in st.session_state.chat_historie:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])

        if over_limiet:
            st.warning(f"Je hebt vandaag je {CHAT_LIMIET_PER_DAG} vragen gebruikt. "
                       "Morgen zijn ze weer beschikbaar.")
        vraag = st.chat_input("Vraag iets over je portfolio…", disabled=over_limiet)
        if vraag and not over_limiet:
            st.session_state.chat_historie.append({"role": "user", "content": vraag})
            with st.chat_message("user"):
                st.markdown(vraag)
            systeem = (
                "Je bent een behulpzame assistent die vragen beantwoordt over de "
                "beleggingsportefeuille van de gebruiker. Antwoord in het Nederlands, kort en "
                "concreet. Baseer je UITSLUITEND op de onderstaande portfolio-data; verzin geen "
                "cijfers en zeg eerlijk wanneer iets niet in de data staat. Geef geen "
                "beleggingsadvies; feitelijke observaties en algemene uitleg mogen wel.\n\n"
                "=== PORTFOLIO-DATA ===\n" + _portfolio_context()
            )
            berichten = [{"role": m["role"], "content": m["content"]}
                         for m in st.session_state.chat_historie]
            try:
                client = anthropic.Anthropic(api_key=_key)

                def _stroom():
                    with client.messages.stream(
                        model="claude-opus-4-8", max_tokens=2000,
                        thinking={"type": "adaptive"},
                        system=systeem, messages=berichten,
                    ) as stream:
                        for tekst in stream.text_stream:
                            yield tekst

                with st.chat_message("assistant"):
                    antwoord = st.write_stream(_stroom())
                st.session_state.chat_historie.append({"role": "assistant", "content": antwoord})
                if _via_backend:  # alleen jouw key telt mee voor de limiet
                    teller[vandaag] = gebruikt + 1
                    st.rerun()
            except Exception as e:
                st.error(f"Kon de assistent niet bereiken: {e}")
        if st.session_state.chat_historie:
            if st.button("🗑️ Gesprek wissen"):
                st.session_state.chat_historie = []
                st.rerun()


# ---------- Acties → Aankopen ----------

with sub_aankopen:
    aankopen = [tx for tx in transacties if tx.aantal > 0]
    if not aankopen:
        st.info("Geen aankopen gevonden.")
    else:
        st.subheader("Alle aankopen")
        st.caption("**Sinds aankoop**: koersontwikkeling sinds jouw aankoopkoers "
                   "(in de notering van het product zelf, dus zonder valuta-effect).")
        rijen = []
        for tx in sorted(aankopen, key=lambda t: t.datum, reverse=True):
            ticker = isin_naar_ticker.get(tx.isin)
            koers_nu = koersen.get(ticker) if ticker else None
            rijen.append({
                "Datum": tx.datum.date(),
                "Product": tx.product,
                "Aantal": tx.aantal,
                "Aankoopkoers": tx.koers,
                "Valuta": tx.valuta,
                "Bedrag (EUR)": -tx.totaal_eur,
                "Koers nu": koers_nu,
                "Sinds aankoop (%)": (koers_nu / tx.koers - 1) * 100 if koers_nu and tx.koers else None,
            })
        aankopen_df = pd.DataFrame(rijen)
        st.dataframe(
            stijl_pos_neg(aankopen_df.style.format({
                "Aantal": "{:.0f}",
                "Aankoopkoers": "{:.2f}",
                "Bedrag (EUR)": "€ {:,.2f}",
                "Koers nu": "{:.2f}",
                "Sinds aankoop (%)": "{:+.1f}%",
            }, na_rep="—"), ["Sinds aankoop (%)"]),
            use_container_width=True, hide_index=True,
        )

        beste_koop = aankopen_df.dropna(subset=["Sinds aankoop (%)"])
        if len(beste_koop) >= 2:
            top = beste_koop.loc[beste_koop["Sinds aankoop (%)"].idxmax()]
            flop = beste_koop.loc[beste_koop["Sinds aankoop (%)"].idxmin()]
            a1, a2 = st.columns(2)
            a1.metric("🎯 Beste aankoop", f"{top['Product']} ({top['Datum'].strftime('%d-%m-%Y')})",
                      procent(top["Sinds aankoop (%)"]))
            a2.metric("🙈 Minste aankoop", f"{flop['Product']} ({flop['Datum'].strftime('%d-%m-%Y')})",
                      procent(flop["Sinds aankoop (%)"]))

# ---------- Acties → Verkocht ----------

with sub_verkocht:
    if not verkopen:
        st.info("Nog geen verkopen gevonden in je transacties.")
    else:
        st.subheader("Verkochte posities")
        koersen_eur = {t: koers_eur_nu(t) for t in koersen}
        verkocht_df = analyse.verkochte_posities_analyse(verkopen, isin_naar_ticker, koersen_eur)
        st.dataframe(
            stijl_pos_neg(verkocht_df.drop(columns=["ISIN"]).style.format({
                "Verkocht (stuks)": "{:.0f}",
                "Opbrengst (EUR)": "€ {:,.2f}",
                "Kostprijs (EUR)": "€ {:,.2f}",
                "Resultaat (EUR)": "€ {:+,.2f}",
                "Gem. verkoopkoers (EUR)": "€ {:.2f}",
                "Koers nu (EUR)": "€ {:.2f}",
                "Koers sinds verkoop (%)": "{:+.1f}%",
            }, na_rep="—"), ["Resultaat (EUR)", "Koers sinds verkoop (%)"]),
            use_container_width=True, hide_index=True,
        )
        st.caption(
            "**Koers sinds verkoop**: koersontwikkeling ná jouw (gemiddelde) verkoopkoers, "
            "in de notering van het product zelf. Negatief = de koers is gedaald, "
            "dus achteraf een goede verkoop. 🎯"
        )

        st.subheader("Alle verkooptransacties")
        detail = pd.DataFrame([{
            "Datum": v.datum.date(),
            "Product": v.product,
            "Aantal": v.aantal,
            "Verkoopkoers": v.koers,
            "Valuta": v.valuta,
            "Opbrengst (EUR)": v.opbrengst_eur,
            "Kostprijs (EUR)": v.kostprijs_eur,
            "Resultaat (EUR)": v.resultaat_eur,
            "Resultaat (%)": v.resultaat_pct,
        } for v in sorted(verkopen, key=lambda v: v.datum, reverse=True)])
        st.dataframe(
            stijl_pos_neg(detail.style.format({
                "Aantal": "{:.0f}",
                "Verkoopkoers": "{:.2f}",
                "Opbrengst (EUR)": "€ {:,.2f}",
                "Kostprijs (EUR)": "€ {:,.2f}",
                "Resultaat (EUR)": "€ {:+,.2f}",
                "Resultaat (%)": "{:+.1f}%",
            }, na_rep="—"), ["Resultaat (EUR)", "Resultaat (%)"]),
            use_container_width=True, hide_index=True,
        )

# ---------- Acties → Kosten ----------

with sub_kosten:
    st.subheader("💸 Kostenoverzicht")
    st.caption("Wat je beleggen aan kosten heeft gekost. Transactiekosten zitten al verwerkt "
               "in je aan-/verkoopprijs; hier maak ik ze apart zichtbaar.")
    tx_kosten = (pd.Series([abs(t.kosten_eur) for t in transacties],
                           index=[pd.Timestamp(t.datum.date()) for t in transacties])
                 .groupby(level=0).sum())
    tx_kosten_tot = float(tx_kosten.sum())
    overig_tot = abs(kosten_overig_totaal)
    rente_tot = float(account_data.rente_totaal)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Totale kosten", euro(tx_kosten_tot + overig_tot),
              help="Transactiekosten + aansluitings-/ADR-kosten sinds je eerste transactie.")
    c2.metric("Transactiekosten", euro(tx_kosten_tot))
    c3.metric("Aansluit-/ADR-kosten", euro(overig_tot))
    c4.metric("Rente (saldo)", euro(rente_tot), delta_color="off",
              help="Ontvangen/betaalde rente uit Account.csv.")

    jr_tx = tx_kosten.groupby(tx_kosten.index.year).sum()
    jr_ov = (kosten_dag.abs().groupby(kosten_dag.index.year).sum()
             if len(kosten_dag) else pd.Series(dtype=float))
    jaren = sorted(set(jr_tx.index) | set(jr_ov.index))
    if jaren:
        kdf = pd.DataFrame({
            "Jaar": jaren,
            "Transactiekosten (EUR)": [float(jr_tx.get(j, 0.0)) for j in jaren],
            "Aansluit-/ADR (EUR)": [float(jr_ov.get(j, 0.0)) for j in jaren],
        })
        kdf["Totaal (EUR)"] = kdf["Transactiekosten (EUR)"] + kdf["Aansluit-/ADR (EUR)"]
        links, rechts = st.columns([3, 2])
        with links:
            figk = go.Figure()
            figk.add_trace(go.Bar(x=kdf["Jaar"].astype(str), y=kdf["Transactiekosten (EUR)"],
                                  name="Transactiekosten", marker_color="#fb923c"))
            figk.add_trace(go.Bar(x=kdf["Jaar"].astype(str), y=kdf["Aansluit-/ADR (EUR)"],
                                  name="Aansluit-/ADR", marker_color="#f472b6"))
            figk.update_layout(barmode="stack", margin=dict(t=10, b=10), height=320,
                               legend=dict(orientation="h", y=1.1),
                               paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                               xaxis=dict(type="category", gridcolor="#1d2738"),
                               yaxis=dict(gridcolor="#1d2738", tickprefix="€ "))
            st.plotly_chart(figk, use_container_width=True)
        with rechts:
            st.dataframe(kdf.style.format({
                "Transactiekosten (EUR)": "€ {:,.2f}", "Aansluit-/ADR (EUR)": "€ {:,.2f}",
                "Totaal (EUR)": "€ {:,.2f}"}), use_container_width=True, hide_index=True, height=320)

    # Kosten per product (transactiekosten).
    prod_kosten = {}
    for t in transacties:
        prod_kosten[t.product] = prod_kosten.get(t.product, 0.0) + abs(t.kosten_eur)
    prod_kosten = {p: k for p, k in prod_kosten.items() if k > 0.005}
    if prod_kosten:
        st.markdown("##### Transactiekosten per product")
        pk = pd.DataFrame(sorted(prod_kosten.items(), key=lambda x: -x[1]),
                          columns=["Product", "Transactiekosten (EUR)"])
        st.dataframe(pk.style.format({"Transactiekosten (EUR)": "€ {:,.2f}"}),
                     use_container_width=True, hide_index=True)
    if tx_kosten_tot + overig_tot > 0 and totale_inleg:
        kosten_pct = (tx_kosten_tot + overig_tot) / totale_inleg * 100
        st.caption(f"Je totale kosten zijn ≈ {kosten_pct:.2f}% van je huidige inleg "
                   f"({euro(totale_inleg)}).")


# ---------- Acties → Data (controle & correctie) ----------

with sub_data:
    st.subheader("Klopt de koppeling met de koersdata?")
    st.caption(
        "Hier zie je per product welke Yahoo-ticker de app gebruikt en wat dat voor "
        "de huidige waarde betekent. Klopt een waarde niet (bijv. te hoog), dan is "
        "meestal de ticker of de valuta verkeerd. Corrigeer 'm hieronder."
    )

    controle_rijen = []
    for p in posities:
        ticker = isin_naar_ticker.get(p.isin)
        koers_eur = koers_eur_nu(ticker) if ticker else None
        dek = dekking_df[dekking_df["ISIN"] == p.isin] if not dekking_df.empty else pd.DataFrame()
        vanaf = dek["Marktdata vanaf"].iloc[0] if len(dek) and "Marktdata vanaf" in dek else None
        controle_rijen.append({
            "Product": p.product,
            "ISIN": p.isin,
            "Ticker": ticker or "❌ niet gevonden",
            "Valuta": valutas.get(ticker, "?") if ticker else "?",
            "Aantal": p.aantal,
            "Koers (EUR)": koers_eur,
            "Waarde (EUR)": p.aantal * koers_eur if koers_eur is not None else None,
            "Koersdata vanaf": vanaf,
        })
    controle_df = pd.DataFrame(controle_rijen)
    if not controle_df.empty:
        st.dataframe(
            controle_df.style.format({
                "Aantal": "{:.0f}", "Koers (EUR)": "€ {:.2f}", "Waarde (EUR)": "€ {:,.2f}",
            }, na_rep="—"),
            use_container_width=True, hide_index=True,
        )

    st.markdown("##### ✏️ Ticker corrigeren")
    st.caption("Pak het juiste symbool van [Yahoo Finance](https://finance.yahoo.com) "
               "(bijv. `ASML.AS` voor Amsterdam, `SHELL.L` voor Londen). Leeg laten = "
               "product negeren. Daarna herstart de app automatisch.")
    keuze_isin = st.selectbox(
        "Product", options=[r["ISIN"] for r in controle_rijen],
        format_func=lambda i: next(r["Product"] for r in controle_rijen if r["ISIN"] == i),
    )
    huidige = isin_naar_ticker.get(keuze_isin) or ""
    nieuwe_ticker = st.text_input("Yahoo-ticker", value=huidige, key="ticker_input")
    if st.button("Opslaan", type="primary"):
        marktdata.zet_ticker(keuze_isin, nieuwe_ticker)
        st.cache_data.clear()
        st.success(f"Ticker voor {keuze_isin} opgeslagen als '{nieuwe_ticker}'. App herlaadt…")
        st.rerun()

    st.divider()
    st.markdown("##### 🎯 Strategie per positie")
    st.caption("Geef elke positie een strategie-label (zoals in PDT). Dit verschijnt als "
               "klikbare verdeling onder **📊 Portfolio → Strategie**.")
    strat_isin = st.selectbox(
        "Positie", options=[r["ISIN"] for r in controle_rijen],
        format_func=lambda i: next(r["Product"] for r in controle_rijen if r["ISIN"] == i),
        key="strat_isin",
    )
    huidige_strat = strategie_map.get(strat_isin, "Onbekend")
    nieuwe_strat = st.selectbox(
        "Strategie", marktdata.STRATEGIE_OPTIES,
        index=marktdata.STRATEGIE_OPTIES.index(huidige_strat)
        if huidige_strat in marktdata.STRATEGIE_OPTIES else 0,
        key="strat_keuze",
    )
    if st.button("Strategie opslaan", type="primary", key="strat_opslaan"):
        pref_zet_strategie(strat_isin, nieuwe_strat)
        st.success(f"Strategie voor {strat_isin} opgeslagen als '{nieuwe_strat}'. App herlaadt…")
        st.rerun()

    st.divider()
    st.markdown("##### 📋 Koershiaten (ontbrekende marktkoers)")
    if gap_df.empty:
        st.success("Geen koershiaten: van al je posities is marktkoers beschikbaar. 👍")
    else:
        st.caption(
            "Voor deze aandelen mist (een deel van) de marktkoers — meestal omdat ze gedelist/"
            "overgenomen zijn of omdat je ze aanhield vóórdat de koersdata begon. Ik vul nu mijn "
            "**beste schatting** in (op basis van je transactieprijzen). Weet je de echte koers? "
            "Vul 'm hieronder in als **EUR per stuk** voor de ontbrekende periode. "
            "Een ✅ betekent dat je al een handmatige koers hebt opgegeven."
        )
        with st.form("koershiaten"):
            nieuwe_ov = {}
            for _, r in gap_df.iterrows():
                isin = r["ISIN"]
                mv = r.get("Marktdata vanaf")
                periode = f"vóór {mv}" if mv else "volledige looptijd (gedelist)"
                # Mijn huidige schatting: de gebruikte koers rond de eerste transactie.
                schatting = None
                if isin in prijzen_eur.columns:
                    reeks = prijzen_eur[isin].dropna()
                    if len(reeks):
                        schatting = float(reeks.iloc[0])
                huidige_ov = koers_overrides.get(isin)
                vink = " ✅" if huidige_ov else ""
                c1, c2, c3 = st.columns([3, 2, 2])
                c1.markdown(f"**{r['Product']}**{vink}  \n`{isin}` · mist koers {periode}")
                c2.markdown(f"mijn schatting:  \n**{euro(schatting)}** /stuk" if schatting else "—")
                nieuwe_ov[isin] = c3.number_input(
                    "Jouw koers (0 = mijn schatting)", min_value=0.0, format="%.4f",
                    value=float(huidige_ov) if huidige_ov else 0.0,
                    key=f"ov_{isin}",
                )
            if st.form_submit_button("💾 Overrides opslaan", type="primary"):
                for isin, koers in nieuwe_ov.items():
                    # >0 = jouw handmatige koers; 0 = laat mijn schatting staan.
                    pref_zet_koers_override(isin, koers if koers > 0 else None)
                st.cache_data.clear()
                st.success("Koersen opgeslagen. App herlaadt…")
                st.rerun()

    if not dekking_df.empty:
        with st.expander("Details koersdekking per product"):
            st.dataframe(dekking_df, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("##### 🏦 Exacte stortingen & opnames (Account.csv)")
    st.info(
        "De rendementen kloppen al zonder dit, maar **exacte** stortingen en opnames "
        "staan alleen in de **Account.csv** van DEGIRO (Activiteit → Rekeningoverzicht → "
        "Exporteren). Nu schat de app je stortingen uit een kassaldo. Wil je dat ik "
        "Account.csv inlees voor precieze cijfers én dividend-tracking? Zeg het maar."
    )
