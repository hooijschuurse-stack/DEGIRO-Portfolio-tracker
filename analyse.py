"""
Analyses: portfoliowaarde door de tijd en rendement per periode.

Voor het rendement gebruiken we de 'Modified Dietz'-methode. Die corrigeert
voor geld dat je tussentijds inlegt of opneemt, zodat een storting of opname
niet als 'winst' of 'verlies' meetelt. Dit is dezelfde aanpak die
professionele tools gebruiken.

Belangrijk voor de consistentie: producten waarvoor géén koersdata gevonden
is, laten we óók buiten de geldstromen. Anders telt hun aankoop wel als
inleg maar nooit als waarde, en slaat het rendement nergens meer op.
"""

import pandas as pd

from degiro import Transactie
import marktdata


def waarde_historie(
    transacties: list[Transactie],
    isin_naar_ticker: dict[str, str],
    overrides: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, set[str], pd.DataFrame, pd.DataFrame]:
    """
    Bereken per dag de totale portfoliowaarde in EUR.

    BEWUST GEEN aandelensplit-correctie: DEGIRO's transactie-aantallen zijn al
    de werkelijke aantallen (splits en bonusaandelen staan in de export), en
    yfinance's onaangepaste koersen (auto_adjust=False) lopen daarmee in de pas.
    Zelf "splits" toepassen uit yfinance.splits corrumpeert juist de aantallen,
    want die lijst bevat ook stockdividenden en herwaarderingen.

    Geeft terug:
    - DataFrame met kolom 'waarde' (totaal) en per product een kolom
    - de set ISIN-codes die daadwerkelijk meegenomen zijn (met koersdata);
      gebruik diezelfde set voor de geldstromen!
    - DataFrame met de dagkoers per ISIN, omgerekend naar EUR
    - DataFrame met de datadekking per product (voor de Data-controle)
    """
    if not transacties:
        leeg = pd.DataFrame()
        return leeg, set(), leeg, leeg

    start = min(tx.datum for tx in transacties).date()
    tickers = sorted({t for t in isin_naar_ticker.values() if t})
    koersen = marktdata.koers_historie(tickers, start=start)
    if koersen.empty:
        leeg = pd.DataFrame()
        return leeg, set(), leeg, leeg
    koersen = koersen.ffill()

    # Wisselkoersen per valuta alvast ophalen (alleen voor niet-EUR tickers).
    fx: dict[str, pd.Series | None] = {}
    for ticker in tickers:
        valuta = marktdata.valuta_van(ticker)
        if valuta not in fx:
            fx[valuta] = marktdata.fx_naar_eur_historie(valuta, start=start)

    per_product = {}
    prijzen_eur = {}
    dekking = []
    gebruikt: set[str] = set()
    for isin, ticker in isin_naar_ticker.items():
        txs = [tx for tx in transacties if tx.isin == isin]
        if not txs:
            continue
        naam = txs[-1].product
        eerste_tx = min(tx.datum for tx in txs).date()

        # Aantal stukken in bezit per dag (cumulatief). Met een LIJST, niet een
        # dict-comprehension, anders verdwijnen meerdere transacties op één dag.
        mutaties = pd.Series(
            [tx.aantal for tx in txs],
            index=[pd.Timestamp(tx.datum.date()) for tx in txs],
        ).groupby(level=0).sum()
        aantal = mutaties.cumsum().reindex(koersen.index, method="ffill").fillna(0.0)

        # Je eigen transactiekoersen in EUR (per dag), als prijsanker.
        impliciet = pd.Series({
            pd.Timestamp(tx.datum.date()): abs(tx.waarde_eur) / abs(tx.aantal)
            for tx in txs if tx.aantal and tx.waarde_eur
        }).groupby(level=0).last()

        override = (overrides or {}).get(isin)
        heeft_koers = ticker and ticker in koersen.columns and not koersen[ticker].dropna().empty
        if heeft_koers:
            prijs = koersen[ticker]
            valuta = marktdata.valuta_van(ticker)
            fx_serie = fx.get(valuta)
            prijs_eur = prijs if fx_serie is None else prijs * fx_serie.reindex(koersen.index).ffill()
            echte = koersen[ticker].dropna()
            markt_vanaf = echte.index[0].date()
            # Gat vóór de marktdata (positie aangehouden vóór de koers begint):
            # vul met handmatige override als die er is, anders je transactiekoers.
            gap_vulling = override if override else impliciet.reindex(prijs_eur.index, method="ffill")
            prijs_eur = prijs_eur.fillna(gap_vulling)
            markt_tot = echte.index[-1].date()
        elif len(impliciet) >= 1 or override:
            # GEEN ticker (gedelist/overgenomen, bv. Just Eat Takeaway). Override
            # heeft voorrang; anders je transactiekoersen, tijd-geïnterpoleerd,
            # zodat de positie zijn waarde behoudt i.p.v. op €0 te vallen.
            if override:
                prijs_eur = pd.Series(float(override), index=koersen.index)
            else:
                basis = impliciet.reindex(koersen.index.union(impliciet.index)).sort_index()
                prijs_eur = (basis.interpolate(method="time").reindex(koersen.index)
                             .ffill().bfill())
            markt_vanaf, markt_tot = None, None
        else:
            dekking.append({"Product": naam, "ISIN": isin, "Ticker": ticker or "",
                            "Eerste transactie": eerste_tx, "Marktdata vanaf": None,
                            "Marktdata t/m": None, "Gap": True, "Hiaat": True,
                            "Override (EUR)": override})
            continue

        gebruikt.add(isin)
        per_product[naam] = (aantal * prijs_eur).fillna(0.0)
        prijzen_eur[isin] = prijs_eur
        # Gap = positie aangehouden vóór de marktdata begon, of helemaal geen
        # marktdata (gedelist). Hiaat = gap die nog NIET met een override is
        # opgelost (dat bepaalt de waarschuwing; de gap-lijst blijft bewerkbaar).
        gap = markt_vanaf is None or markt_vanaf > eerste_tx
        dekking.append({"Product": naam, "ISIN": isin, "Ticker": ticker or "(transactiekoers)",
                        "Eerste transactie": eerste_tx, "Marktdata vanaf": markt_vanaf,
                        "Marktdata t/m": markt_tot, "Gap": gap,
                        "Hiaat": gap and override is None, "Override (EUR)": override})

    df = pd.DataFrame(per_product)
    df["waarde"] = df.sum(axis=1)
    return df, gebruikt, pd.DataFrame(prijzen_eur), pd.DataFrame(dekking)


def geldstromen(transacties: list[Transactie], alleen_isins: set[str] | None = None) -> pd.Series:
    """
    Aan-/verkoopstromen per dag, in EUR, vanuit het oogpunt van de
    *effectenportefeuille*. Positief = geld erin (aankoop), negatief = geld
    eruit (verkoop). Dit zijn de grenzen waarop we het rendement neutraliseren;
    of een verkoop daarna werd herbelegd of opgenomen maakt voor het
    beleggingsrendement niet uit.

    Met `alleen_isins` beperk je de stromen tot producten die ook in de
    waarde-historie zitten — essentieel voor een kloppend rendement.
    """
    relevant = [
        tx for tx in transacties
        if alleen_isins is None or tx.isin in alleen_isins
    ]
    if not relevant:
        return pd.Series(dtype=float)
    # Lijst i.p.v. dict-comprehension: meerdere transacties op dezelfde dag
    # mogen niet verloren gaan.
    stromen = pd.Series(
        [-tx.totaal_eur for tx in relevant],
        index=[pd.Timestamp(tx.datum.date()) for tx in relevant],
    ).groupby(level=0).sum()
    return stromen.sort_index()


def geschatte_stortingen(transacties: list[Transactie]) -> pd.Series:
    """
    Schat per dag de *echte* stortingen (vers geld van je bank), door een
    kassaldo bij te houden. De DEGIRO Transactions.csv bevat geen
    stortingen/opnames, dus we leiden ze af:

    - een verkoop laat cash achter op de rekening (saldo stijgt);
    - een aankoop wordt eerst uit dat saldo betaald (= herbelegging);
    - kan het saldo de aankoop niet dekken, dan is het tekort een storting.

    Opnames kunnen we hieruit niet betrouwbaar afleiden (cash die je opneemt
    vs. cash die blijft staan zien er identiek uit). Voor exacte
    stortingen/opnames is de Account.csv van DEGIRO nodig.
    """
    saldo = 0.0
    stortingen: dict[pd.Timestamp, float] = {}
    for tx in sorted(transacties, key=lambda t: t.datum):
        dag = pd.Timestamp(tx.datum.date())
        if tx.totaal_eur < 0:  # aankoop: geld eruit
            kosten = -tx.totaal_eur
            uit_saldo = min(saldo, kosten)
            saldo -= uit_saldo
            tekort = kosten - uit_saldo
            if tekort > 0:
                stortingen[dag] = stortingen.get(dag, 0.0) + tekort
        else:  # verkoop: geld erbij
            saldo += tx.totaal_eur
    if not stortingen:
        return pd.Series(dtype=float)
    return pd.Series(stortingen).sort_index()


def twr_rendement(waarde: pd.Series, stromen: pd.Series, start, eind) -> float | None:
    """
    Time-weighted return (als fractie) over [start, eind].

    De TWR kettingt de dagelijkse rendementen aaneen en haalt het effect van
    elke in-/uitstroom eruit: r_dag = (waarde_eind - stroom) / waarde_begin.
    Daardoor is hij volledig ongevoelig voor stortingen, opnames én
    herbeleggingen — hij meet puur hoe je beleggingen presteerden.
    """
    start = pd.Timestamp(start)
    eind = pd.Timestamp(eind)
    waarde = waarde.dropna()
    venster = waarde[(waarde.index >= start) & (waarde.index <= eind)]
    if len(venster) < 2:
        return None

    factor = 1.0
    geldig = False
    vorige = float(venster.iloc[0])
    for datum, v in venster.iloc[1:].items():
        stroom = float(stromen.get(datum, 0.0)) if len(stromen) else 0.0
        if vorige > 1e-6:
            factor *= (float(v) - stroom) / vorige
            geldig = True
        vorige = float(v)
    return factor - 1.0 if geldig else None


def cash_historie(transacties, kasstromen, dividenden_df, index, eind_cash=0.0) -> pd.Series:
    """
    Reconstrueer de cashstand door de tijd (geschat), in EUR.

    Cash beweegt door: stortingen (+), opnames (−), aankopen (−) en verkopen (+)
    en ontvangen dividend (+). Zo zie je dat een dip in de effectenwaarde (na een
    verkoop) wordt opgevangen door cash i.p.v. dat geld 'verdwijnt'.

    Let op: dit is een schatting. flatexDEGIRO houdt cash deels in een aparte
    geldrekening en een geldmarktfonds; die rente/koers is niet exact af te
    leiden. We verankeren daarom op `eind_cash` (je actuele saldo uit Account.csv),
    zodat effecten + cash op je echte portefeuillewaarde uitkomt.
    """
    delen = []
    if kasstromen is not None and len(kasstromen):
        delen.append(kasstromen)
    if transacties:
        delen.append(pd.Series(
            [t.totaal_eur for t in transacties],
            index=[pd.Timestamp(t.datum.date()) for t in transacties],
        ).groupby(level=0).sum())
    if dividenden_df is not None and not dividenden_df.empty:
        delen.append(pd.Series(
            dividenden_df["Netto (EUR)"].values,
            index=pd.to_datetime(dividenden_df["Datum"]),
        ).groupby(level=0).sum())
    if not delen:
        return pd.Series(0.0, index=index)
    alle = pd.concat(delen).groupby(level=0).sum().sort_index()
    cash = alle.cumsum().reindex(index, method="ffill").fillna(0.0)
    cash = cash - float(cash.iloc[-1]) + eind_cash   # anker op actueel saldo
    return cash.clip(lower=0)                         # cash kan niet negatief zijn


def twr_index(waarde: pd.Series, stromen: pd.Series) -> pd.Series:
    """
    Cumulatieve time-weighted groei-index (start 1.0), dag op dag geketend en
    gecorrigeerd voor in-/uitstromen. Hiermee teken je je prestatie (%) door de
    tijd, vergelijkbaar met een beursindex.
    """
    waarde = waarde.dropna()
    factor = 1.0
    vorige = None
    data = {}
    for datum, v in waarde.items():
        if vorige is not None and vorige > 1e-6:
            stroom = float(stromen.get(datum, 0.0)) if len(stromen) else 0.0
            factor *= (float(v) - stroom) / vorige
        data[datum] = factor
        vorige = float(v)
    return pd.Series(data)


def _som_in_periode(serie, start, eind) -> float:
    if serie is None or len(serie) == 0:
        return 0.0
    deel = serie[(serie.index > start) & (serie.index <= eind)]
    return float(deel.sum()) if len(deel) else 0.0


def mwrr(waarde: pd.Series, stromen: pd.Series, start, eind,
         extra: pd.Series | None = None) -> float | None:
    """
    Money-Weighted Rate of Return (MWRR) over [start, eind] — de échte
    geld-gewogen rente met compounding (IRR), de standaardmethode van o.a.
    Portfolio Dividend Tracker. Lost op naar R:

        eindwaarde = beginwaarde·(1+R) + Σ stroom_i·(1+R)^(resterende fractie)

    `stromen`: stortingen (+) / opnames (−). `extra` (dividend − kosten) telt
    eenmalig mee in de eindwaarde als die niet al in de waardereeks zit.
    Geeft het periode-rendement (niet geannualiseerd), net als PDT.
    """
    start = pd.Timestamp(start)
    eind = pd.Timestamp(eind)
    waarde = waarde.dropna()
    voor = waarde[waarde.index <= start]
    begin = float(voor.iloc[-1]) if len(voor) else 0.0
    tot = waarde[waarde.index <= eind]
    if tot.empty:
        return None
    eindw = float(tot.iloc[-1]) + _som_in_periode(extra, start, eind)
    duur = (eind - start).days or 1
    per = stromen[(stromen.index > start) & (stromen.index <= eind)]
    flows = [(max((eind - d).days, 0) / duur, float(b)) for d, b in per.items()]

    def f(R):
        return begin * (1 + R) + sum(F * (1 + R) ** w for w, F in flows) - eindw

    lo, hi = -0.9999, 1000.0
    flo, fhi = f(lo), f(hi)
    if flo == 0:
        return lo
    if flo * fhi > 0:  # geen tekenwissel → val terug op Modified Dietz
        return modified_dietz(waarde, stromen, start, eind, extra)
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def modified_dietz(waarde: pd.Series, stromen: pd.Series, start, eind,
                   extra: pd.Series | None = None) -> float | None:
    """
    Money-weighted totaalrendement (Modified Dietz) over [start, eind].

    Dit is wat de meeste portfolio-trackers (o.a. Portfolio Dividend Tracker)
    tonen: het rendement op het geld dat je daadwerkelijk had ingelegd, gewogen
    naar hoe lang elke in-/uitstroom belegd was.

        rendement = (eind - begin - netto_stroom + extra) / (begin + Σ gewicht·stroom)

    `extra` is rendement dat niet in de koerswaarde zit: ontvangen dividend (+)
    en niet-transactiekosten (−). Zo geeft het getal het volledige plaatje.
    """
    start = pd.Timestamp(start)
    eind = pd.Timestamp(eind)
    waarde = waarde.dropna()
    if waarde.empty or eind <= start:
        return None
    voor = waarde[waarde.index <= start]
    begin = float(voor.iloc[-1]) if len(voor) else 0.0
    tot = waarde[waarde.index <= eind]
    if tot.empty:
        return None
    eind_w = float(tot.iloc[-1])
    periode = stromen[(stromen.index > start) & (stromen.index <= eind)]
    netto = float(periode.sum()) if len(periode) else 0.0
    duur = (eind - start).days or 1
    gewogen = sum(b * max((eind - d).days, 0) / duur for d, b in periode.items())
    noemer = begin + gewogen
    if noemer <= 1e-6:
        return None
    return (eind_w - begin - netto + _som_in_periode(extra, start, eind)) / noemer


def simple_rendement(waarde: pd.Series, stromen: pd.Series, start, eind,
                     extra: pd.Series | None = None) -> float | None:
    """
    PDT's 'Simple'-methode: corrigeert voor kasstromen zonder tijdsweging.

        rendement = (eind − begin − netto storting/opname) / begin

    Heeft een positieve beginwaarde nodig; voor 'Alles' (begin = 0) is deze
    methode niet gedefinieerd en geeft None.
    """
    start, eind = pd.Timestamp(start), pd.Timestamp(eind)
    waarde = waarde.dropna()
    voor = waarde[waarde.index <= start]
    begin = float(voor.iloc[-1]) if len(voor) else 0.0
    if begin <= 1e-6:
        return None
    tot = waarde[waarde.index <= eind]
    if tot.empty:
        return None
    eindw = float(tot.iloc[-1]) + _som_in_periode(extra, start, eind)
    per = stromen[(stromen.index > start) & (stromen.index <= eind)]
    netto = float(per.sum()) if len(per) else 0.0
    return (eindw - begin - netto) / begin


def absolute_rendement(waarde: pd.Series, stromen: pd.Series, start, eind,
                       extra: pd.Series | None = None) -> float | None:
    """
    PDT's 'Absolute'-methode: winst gedeeld door het ingelegde geld, zonder
    tijdsweging.

        rendement = winst / (beginwaarde + stortingen in de periode)

    Daalt automatisch bij een nieuwe storting (de basis groeit) — daarom minder
    geschikt om periodes te vergelijken, maar wel intuïtief ('hoeveel % heb ik
    op mijn inleg verdiend').
    """
    start, eind = pd.Timestamp(start), pd.Timestamp(eind)
    waarde = waarde.dropna()
    voor = waarde[waarde.index <= start]
    begin = float(voor.iloc[-1]) if len(voor) else 0.0
    tot = waarde[waarde.index <= eind]
    if tot.empty:
        return None
    eindw = float(tot.iloc[-1]) + _som_in_periode(extra, start, eind)
    per = stromen[(stromen.index > start) & (stromen.index <= eind)]
    netto = float(per.sum()) if len(per) else 0.0
    stort = float(per[per > 0].sum()) if len(per) else 0.0
    basis = begin + stort
    if basis <= 1e-6:
        return None
    return (eindw - begin - netto) / basis


# De vier rendementsmethoden van Portfolio Dividend Tracker (art. 106).
RENDEMENT_METHODEN = ("MWRR", "TWRR", "Simple", "Absolute")


def rendement(waarde: pd.Series, stromen: pd.Series, start, eind,
              methode: str = "MWRR", extra: pd.Series | None = None) -> float | None:
    """Bereken het periode-rendement met de gekozen PDT-methode."""
    if methode == "TWRR":
        return twr_rendement(waarde, stromen, start, eind)
    if methode == "Simple":
        return simple_rendement(waarde, stromen, start, eind, extra)
    if methode == "Absolute":
        return absolute_rendement(waarde, stromen, start, eind, extra)
    return mwrr(waarde, stromen, start, eind, extra)   # MWRR (standaard)


def cagr_van(rendement: float | None, start, eind) -> float | None:
    """
    Geannualiseerd samengesteld rendement (CAGR, PDT art. 212) uit een
    cumulatief periode-rendement:  (1 + rendement)^(1/jaren) − 1.

    Alleen zinvol vanaf ~1 jaar; voor kortere periodes geven we None (anders zou
    je een kort rendement onrealistisch oprekken naar een jaarbasis).
    """
    if rendement is None or rendement <= -1:
        return None
    jaren = (pd.Timestamp(eind) - pd.Timestamp(start)).days / 365.25
    if jaren < 0.95:
        return None
    return (1 + rendement) ** (1 / jaren) - 1


def periode_cijfers(waarde: pd.Series, stromen: pd.Series, start, eind,
                    dividend: pd.Series | None = None,
                    kosten: pd.Series | None = None) -> dict | None:
    """
    Alle kerncijfers over [start, eind] in één keer, zodat de gebruiker de
    berekening kan narekenen:

    beginwaarde + aankopen - verkopen + koerswinst + dividend - kosten = eindwaarde(+)

    Het rendement (%) is money-weighted (Modified Dietz) en bevat het volledige
    plaatje: koerswinst, ontvangen dividend en niet-transactiekosten.
    (Transactiekosten zitten al in de aankoop-/verkoopprijs.)
    """
    start = pd.Timestamp(start)
    eind = pd.Timestamp(eind)
    waarde = waarde.dropna()
    if waarde.empty or eind <= start or waarde.index[-1] < start:
        return None

    voor_start = waarde[waarde.index <= start]
    begin_waarde = float(voor_start.iloc[-1]) if len(voor_start) else 0.0
    tot_eind = waarde[waarde.index <= eind]
    if tot_eind.empty:
        return None
    eind_waarde = float(tot_eind.iloc[-1])

    periode = stromen[(stromen.index > start) & (stromen.index <= eind)]
    aankopen = float(periode[periode > 0].sum()) if len(periode) else 0.0
    verkopen = float(-periode[periode < 0].sum()) if len(periode) else 0.0
    koerswinst = eind_waarde - begin_waarde - (aankopen - verkopen)
    div = _som_in_periode(dividend, start, eind)
    kost = _som_in_periode(kosten, start, eind)   # al negatief
    resultaat = koerswinst + div + kost

    extra = None
    if dividend is not None or kosten is not None:
        delen = [s for s in (dividend, kosten) if s is not None and len(s)]
        extra = pd.concat(delen).groupby(level=0).sum() if delen else None
    venster_start = voor_start.index[-1] if len(voor_start) else start
    return {
        "begin_waarde": begin_waarde,
        "eind_waarde": eind_waarde,
        "aankopen": aankopen,
        "verkopen": verkopen,
        "koerswinst": koerswinst,
        "dividend": div,
        "kosten": kost,
        "resultaat": resultaat,
        "rendement": modified_dietz(waarde, stromen, start, eind, extra=extra),   # totaal, money-weighted
        "rendement_twr": twr_rendement(waarde, stromen, venster_start, eind),     # koersprestatie
    }


def _extra_serie(dividend, kosten):
    delen = [s for s in (dividend, kosten) if s is not None and len(s)]
    return pd.concat(delen).groupby(level=0).sum() if delen else None


def volledige_start(waarde: pd.Series, stromen: pd.Series | None = None) -> pd.Timestamp:
    """
    Begin van de 'Alles'/'Max'-periode: één dag vóór de eerste waardedag ÉN vóór
    de eerste geldstroom.

    Cruciaal: je stort vaak geld een paar dagen vóór je eerste aankoop (om die te
    kunnen betalen). Als de periode pas bij de eerste koersdag begint, valt die
    eerste storting buiten de berekening terwijl de beginwaarde óók 0 is — dan
    'verdwijnt' dat beginkapitaal en explodeert het money-weighted rendement.
    Door vóór de eerste geldstroom te beginnen telt álle inleg mee.
    """
    w = waarde.dropna()
    vroegste = w.index[0]
    if stromen is not None and len(stromen):
        vroegste = min(vroegste, stromen.index[0])
    return vroegste - pd.Timedelta(days=1)


def _standaard_periodes(waarde: pd.Series,
                        stromen: pd.Series | None = None) -> tuple[pd.Timestamp, pd.Timestamp, dict]:
    """De vaste tijdsblokken + (nu, vooraf). 'Alles' begint vóór de eerste
    geldstroom zodat alle inleg meetelt (zie volledige_start)."""
    nu = waarde.dropna().index[-1]
    vooraf = volledige_start(waarde, stromen)
    periodes = {
        "1 week": nu - pd.Timedelta(weeks=1),
        "1 maand": nu - pd.DateOffset(months=1),
        "3 maanden": nu - pd.DateOffset(months=3),
        "Dit jaar": pd.Timestamp(year=nu.year, month=1, day=1),
        "1 jaar": nu - pd.DateOffset(years=1),
        "Alles": vooraf,
    }
    return nu, vooraf, periodes


def rendement_per_periode(waarde: pd.Series, stromen: pd.Series,
                          dividend: pd.Series | None = None,
                          kosten: pd.Series | None = None,
                          methode: str = "MWRR") -> dict[str, float | None]:
    """Totaalrendement (incl. dividend en kosten) over de standaard tijdsblokken,
    met de gekozen PDT-methode (MWRR/TWRR/Simple/Absolute)."""
    if waarde.dropna().empty:
        return {}
    nu, vooraf, periodes = _standaard_periodes(waarde, stromen)
    extra = _extra_serie(dividend, kosten)
    return {
        naam: rendement(waarde, stromen, max(start, vooraf), nu, methode, extra=extra)
        for naam, start in periodes.items()
    }


def cagr_per_periode(waarde: pd.Series, stromen: pd.Series,
                     dividend: pd.Series | None = None,
                     kosten: pd.Series | None = None,
                     methode: str = "MWRR") -> dict[str, float | None]:
    """Geannualiseerd (CAGR) rendement per tijdsblok; None voor periodes < 1 jaar."""
    if waarde.dropna().empty:
        return {}
    nu, vooraf, periodes = _standaard_periodes(waarde, stromen)
    extra = _extra_serie(dividend, kosten)
    uit = {}
    for naam, start in periodes.items():
        start = max(start, vooraf)
        r = rendement(waarde, stromen, start, nu, methode, extra=extra)
        uit[naam] = cagr_van(r, start, nu)
    return uit


def prestatie_per_periode(waarde: pd.Series, stromen: pd.Series) -> dict[str, float | None]:
    """Tijdgewogen prestatie (TWR) over de standaard tijdsblokken — vergelijkbaar
    met een beursindex, ongevoelig voor de timing/omvang van in- en uitleg."""
    if waarde.dropna().empty:
        return {}
    nu = waarde.dropna().index[-1]
    vooraf = waarde.dropna().index[0] - pd.Timedelta(days=1)
    periodes = {
        "1 week": nu - pd.Timedelta(weeks=1),
        "1 maand": nu - pd.DateOffset(months=1),
        "3 maanden": nu - pd.DateOffset(months=3),
        "Dit jaar": pd.Timestamp(year=nu.year, month=1, day=1),
        "1 jaar": nu - pd.DateOffset(years=1),
        "Alles": vooraf,
    }
    return {naam: twr_rendement(waarde, stromen, max(start, vooraf), nu)
            for naam, start in periodes.items()}


def resultaat_per_jaar(waarde: pd.Series, stromen: pd.Series,
                       dividend: pd.Series | None = None,
                       kosten: pd.Series | None = None,
                       totaal_waarde: pd.Series | None = None,
                       externe: pd.Series | None = None) -> pd.DataFrame:
    """
    Per kalenderjaar: beginwaarde, aankopen, verkopen, eindwaarde, resultaat
    (incl. dividend en kosten) en rendement.

    Euro-opbouw komt uit de effectenwaarde + koop/verkoop. Het rendement (%)
    wordt — indien `totaal_waarde` en `externe` (stortingen/opnames) zijn
    meegegeven — money-weighted op de TOTALE accountwaarde berekend, zodat het
    niet ontspoort in jaren met veel handelsturnover.
    """
    waarde = waarde.dropna()
    if waarde.empty:
        return pd.DataFrame()
    rijen = []
    for jaar in sorted(set(waarde.index.year)):
        start = pd.Timestamp(year=jaar, month=1, day=1)
        eind = min(pd.Timestamp(year=jaar, month=12, day=31), waarde.index[-1])
        cijfers = periode_cijfers(waarde, stromen, start, eind, dividend, kosten)
        if cijfers is None:
            continue
        if totaal_waarde is not None and externe is not None:
            rend = mwrr(totaal_waarde, externe, start, eind)
        else:
            rend = cijfers["rendement"]
        rijen.append({
            "Jaar": jaar,
            "Beginwaarde": cijfers["begin_waarde"],
            "Aankopen": cijfers["aankopen"],
            "Verkopen": cijfers["verkopen"],
            "Eindwaarde": cijfers["eind_waarde"],
            "Resultaat (EUR)": cijfers["resultaat"],
            "Rendement (%)": rend * 100 if rend is not None else None,
        })
    return pd.DataFrame(rijen)


# ---------- Risico- en prestatie-metrics (PDT art. 222/223/226/214) ----------

def dagrendementen(waarde: pd.Series, stromen: pd.Series) -> pd.Series:
    """
    Dagelijkse (tijdgewogen) rendementen van de portefeuille, gecorrigeerd voor
    in-/uitstromen:  r_t = (V_t − stroom_t) / V_{t-1} − 1.

    Basis voor volatiliteit en beta — net als de TWR-keten, maar dan de losse
    dagrendementen i.p.v. het product ervan.
    """
    waarde = waarde.dropna()
    rets, vorige = {}, None
    for datum, v in waarde.items():
        if vorige is not None and vorige > 1e-6:
            stroom = float(stromen.get(datum, 0.0)) if len(stromen) else 0.0
            rets[datum] = (float(v) - stroom) / vorige - 1.0
        vorige = float(v)
    return pd.Series(rets)


def volatiliteit(dagrend: pd.Series, handelsdagen: int = 252) -> float | None:
    """Geannualiseerde volatiliteit = standaarddeviatie van de dagrendementen
    × √252 (PDT art. 226)."""
    dagrend = dagrend.dropna()
    if len(dagrend) < 2:
        return None
    return float(dagrend.std(ddof=1)) * (handelsdagen ** 0.5)


def beta(port_rend: pd.Series, index_rend: pd.Series) -> float | None:
    """Beta t.o.v. een index = cov(port, index) / var(index), op basis van de
    overlappende dagrendementen (PDT art. 222)."""
    df = pd.concat([port_rend, index_rend], axis=1, join="inner").dropna()
    if len(df) < 2:
        return None
    var = float(df.iloc[:, 1].var(ddof=1))
    if var <= 1e-12:
        return None
    cov = float(df.cov().iloc[0, 1])
    return cov / var


def alfa(port_rendement: float | None, index_rendement: float | None) -> float | None:
    """Alfa = jouw periode-rendement − het rendement van de benchmark over
    dezelfde periode (PDT art. 214). Positief = je versloeg de index."""
    if port_rendement is None or index_rendement is None:
        return None
    return port_rendement - index_rendement


def sharpe(dagrend: pd.Series, handelsdagen: int = 252, rente: float = 0.0) -> float | None:
    """
    Sharpe-ratio: rendement per eenheid risico = (gem. dagrendement − rente) /
    std, geannualiseerd met √252. Risicovrije rente standaard 0. Hoger = meer
    rendement per stukje risico.
    """
    d = dagrend.dropna()
    if len(d) < 2:
        return None
    s = float(d.std(ddof=1))
    if s <= 1e-12:
        return None
    dag_rente = rente / handelsdagen
    return (float(d.mean()) - dag_rente) / s * (handelsdagen ** 0.5)


def drawdown_serie(groei_index: pd.Series) -> pd.Series:
    """
    Drawdown door de tijd (≤ 0): hoeveel je onder de hoogste stand tot dan toe
    zit. Werkt op een TWR-groei-index (dus zonder effect van stortingen).
    """
    g = groei_index.dropna()
    if g.empty:
        return pd.Series(dtype=float)
    return g / g.cummax() - 1.0


def max_drawdown(groei_index: pd.Series) -> float | None:
    """Grootste piek-tot-dal-daling (negatieve fractie) over de periode."""
    dd = drawdown_serie(groei_index)
    return float(dd.min()) if len(dd) else None


def maand_rendement(dagrend: pd.Series) -> pd.DataFrame:
    """
    Maandrendement (tijdgewogen) als pivot jaar×maand uit de dagrendementen:
    per maand de dagrendementen aaneengeschakeld. Voor de heatmap.
    """
    d = dagrend.dropna()
    if d.empty:
        return pd.DataFrame()
    maand = (1 + d).groupby([d.index.year, d.index.month]).prod() - 1
    df = maand.rename_axis(index=["Jaar", "Maand"]).reset_index(name="r")
    return df.pivot(index="Jaar", columns="Maand", values="r").sort_index(ascending=False)


def turnover(transacties: list[Transactie], waarde: pd.Series, start, eind) -> float | None:
    """
    Turnover = MIN(totale verkopen, totale aankopen) / gemiddelde portfoliowaarde
    × 100 (PDT art. 223). Maat voor hoe actief je handelt.
    """
    start, eind = pd.Timestamp(start), pd.Timestamp(eind)
    kopen = verkopen = 0.0
    for tx in transacties:
        d = pd.Timestamp(tx.datum.date())
        if d <= start or d > eind:
            continue
        if tx.totaal_eur < 0:       # aankoop: geld eruit
            kopen += -tx.totaal_eur
        else:                       # verkoop: geld erbij
            verkopen += tx.totaal_eur
    venster = waarde.dropna()
    venster = venster[(venster.index >= start) & (venster.index <= eind)]
    gem = float(venster.mean()) if len(venster) else 0.0
    if gem <= 1e-6:
        return None
    return min(kopen, verkopen) / gem


# ---------- Dividend-metrics (PDT art. 244/245/293) ----------

def dividend_per_jaar(dividend_df: pd.DataFrame) -> pd.Series:
    """Netto ontvangen dividend per kalenderjaar (EUR), uit Account.csv."""
    if dividend_df is None or dividend_df.empty:
        return pd.Series(dtype=float)
    d = dividend_df.copy()
    d["Jaar"] = pd.to_datetime(d["Datum"]).dt.year
    return d.groupby("Jaar")["Netto (EUR)"].sum().sort_index()


def dividendgroei(per_jaar: pd.Series, jaren: int = 3) -> float | None:
    """
    Dividendgroei als CAGR over de laatste `jaren` volledige jaren (PDT art. 245):
    (eind / begin)^(1/n) − 1. Gebruikt alleen afgesloten jaren (het lopende jaar
    is nog onvolledig en zou de groei vertekenen).
    """
    per_jaar = per_jaar.dropna()
    if len(per_jaar) < 2:
        return None
    huidig = pd.Timestamp.today().year
    afgesloten = per_jaar[per_jaar.index < huidig]
    if len(afgesloten) < 2:
        return None
    reeks = afgesloten.iloc[-(jaren + 1):]
    begin, eind = float(reeks.iloc[0]), float(reeks.iloc[-1])
    n = len(reeks) - 1
    if begin <= 1e-6 or n < 1:
        return None
    return (eind / begin) ** (1 / n) - 1


def padi_projectie(padi_nu: float, groei: float, maandelijkse_storting: float,
                   yield_pct: float, jaren: int = 10) -> pd.DataFrame:
    """
    Toekomstige PADI (Projected Annual Dividend Income, PDT art. 293):
        volgend = vorig × (1 + groei) + (maandstorting × 12 × yield)
    `groei` en `yield_pct` als fractie. Geeft een tabel jaar 0..n.
    """
    rijen = [{"Jaar": 0, "PADI (EUR)": padi_nu}]
    padi = padi_nu
    for j in range(1, jaren + 1):
        padi = padi * (1 + groei) + maandelijkse_storting * 12 * yield_pct
        rijen.append({"Jaar": j, "PADI (EUR)": padi})
    return pd.DataFrame(rijen)


def bezit_op_datum(transacties: list[Transactie], datum: pd.Timestamp) -> dict[str, float]:
    """Hoeveel stuks van elk product had je op deze datum in bezit?"""
    bezit: dict[str, float] = {}
    for tx in transacties:
        if pd.Timestamp(tx.datum.date()) <= datum:
            bezit[tx.isin] = bezit.get(tx.isin, 0.0) + tx.aantal
    return {isin: aantal for isin, aantal in bezit.items() if aantal > 1e-9}


def tijdmachine(
    transacties: list[Transactie],
    prijzen_eur: pd.DataFrame,
    datum: pd.Timestamp,
) -> pd.DataFrame:
    """
    Welke producten had je op `datum` in bezit, wat waren ze toen waard,
    en wat zou datzelfde mandje nú waard zijn als je niets had gedaan?
    """
    if prijzen_eur.empty:
        return pd.DataFrame()
    datum = pd.Timestamp(datum)
    namen = {tx.isin: tx.product for tx in transacties}
    bezit = bezit_op_datum(transacties, datum)

    rijen = []
    for isin, aantal in bezit.items():
        if isin not in prijzen_eur.columns:
            continue
        serie = prijzen_eur[isin].dropna()
        toen_serie = serie[serie.index <= datum]
        if toen_serie.empty or serie.empty:
            continue
        koers_toen = float(toen_serie.iloc[-1])
        koers_nu = float(serie.iloc[-1])
        rijen.append({
            "Product": namen.get(isin, isin),
            "Aantal toen": aantal,
            "Koers toen (EUR)": koers_toen,
            "Waarde toen (EUR)": aantal * koers_toen,
            "Koers nu (EUR)": koers_nu,
            "Waarde nu indien vastgehouden (EUR)": aantal * koers_nu,
            "Sindsdien (%)": (koers_nu / koers_toen - 1) * 100 if koers_toen else None,
        })
    df = pd.DataFrame(rijen)
    if not df.empty:
        df = df.sort_values("Waarde toen (EUR)", ascending=False)
    return df


def verkochte_posities_analyse(
    verkopen,
    isin_naar_ticker: dict[str, str],
    koersen_eur: dict[str, float],
) -> pd.DataFrame:
    """
    Vat alle verkopen samen per product en vergelijk de verkoopkoers met de
    koers van nu: was het achteraf een goed moment om te verkopen?

    Alles in EUR. `koersen_eur` is ticker -> huidige koers omgerekend naar EUR.
    De gemiddelde verkoopkoers leiden we af uit de EUR-opbrengst, zodat we niet
    per ongeluk een andere valuta (bijv. Britse pence bij ULVR.L) vergelijken
    met een verkoop die in euro's plaatsvond.
    """
    rijen = []
    per_isin: dict[str, list] = {}
    for v in verkopen:
        per_isin.setdefault(v.isin, []).append(v)

    for isin, lijst in per_isin.items():
        totaal_aantal = sum(v.aantal for v in lijst)
        opbrengst = sum(v.opbrengst_eur for v in lijst)
        gem_verkoopkoers_eur = opbrengst / totaal_aantal if totaal_aantal else None
        ticker = isin_naar_ticker.get(isin)
        koers_nu_eur = koersen_eur.get(ticker) if ticker else None
        sinds_verkoop = None
        if koers_nu_eur and gem_verkoopkoers_eur:
            sinds_verkoop = (koers_nu_eur / gem_verkoopkoers_eur - 1) * 100

        rijen.append({
            "Product": lijst[-1].product,
            "ISIN": isin,
            "Laatste verkoop": max(v.datum for v in lijst).date(),
            "Verkocht (stuks)": totaal_aantal,
            "Opbrengst (EUR)": opbrengst,
            "Kostprijs (EUR)": sum(v.kostprijs_eur for v in lijst),
            "Resultaat (EUR)": sum(v.resultaat_eur for v in lijst),
            "Gem. verkoopkoers (EUR)": gem_verkoopkoers_eur,
            "Koers nu (EUR)": koers_nu_eur,
            "Koers sinds verkoop (%)": sinds_verkoop,
        })

    df = pd.DataFrame(rijen)
    if not df.empty:
        df = df.sort_values("Resultaat (EUR)", ascending=False)
    return df


def stress_test(posities_df: pd.DataFrame, schok: float) -> tuple[pd.DataFrame, float]:
    """
    Schat de impact van een marktschok op je portefeuille via de beta per positie:
    verwachte verandering = schok × beta × waarde (CAPM-benadering). `schok` als
    fractie (−0.20 = markt −20%). Posities zonder bekende beta krijgen beta 1.0
    (bewegen mee met de markt). Geeft (tabel, totale_verandering_eur).
    """
    if posities_df is None or posities_df.empty:
        return pd.DataFrame(), 0.0
    bruik = posities_df.dropna(subset=["Waarde (EUR)"]).copy()
    if bruik.empty:
        return pd.DataFrame(), 0.0
    rijen = []
    for _, r in bruik.iterrows():
        beta = r.get("Beta")
        beta = float(beta) if beta is not None and not pd.isna(beta) else 1.0
        waarde = float(r["Waarde (EUR)"])
        verandering = schok * beta * waarde
        rijen.append({"Product": r["Product"], "Beta": beta, "Waarde (EUR)": waarde,
                      "Verwachte verandering (EUR)": verandering,
                      "Nieuwe waarde (EUR)": waarde + verandering})
    df = pd.DataFrame(rijen).sort_values("Verwachte verandering (EUR)")
    return df, float(df["Verwachte verandering (EUR)"].sum())


def dividend_veiligheid_score(payout_ratio, groei_3j, yield_fractie) -> tuple[int, str, str]:
    """
    Eenvoudige dividendveiligheid-heuristiek → (score 0-100, label, toelichting).
    Lagere payout ratio + positieve dividendgroei + niet-extreme yield = veiliger.
    Geen garantie; een hulpmiddel, geen advies.
    """
    score = 100
    redenen = []
    if payout_ratio is not None and not pd.isna(payout_ratio):
        pr = float(payout_ratio)
        if pr > 1.0:
            score -= 45; redenen.append(f"keert méér uit dan de winst (payout {pr*100:.0f}%)")
        elif pr > 0.8:
            score -= 25; redenen.append(f"hoge payout ratio ({pr*100:.0f}%)")
        elif pr > 0.6:
            score -= 10; redenen.append(f"matige payout ratio ({pr*100:.0f}%)")
    else:
        score -= 10; redenen.append("payout ratio onbekend")
    if groei_3j is not None:
        if groei_3j < -0.02:
            score -= 25; redenen.append(f"dalend dividend ({groei_3j*100:.0f}%/jr)")
        elif groei_3j > 0.03:
            score += 5; redenen.append(f"groeiend dividend (+{groei_3j*100:.0f}%/jr)")
    if yield_fractie is not None and not pd.isna(yield_fractie):
        y = float(yield_fractie)
        if y > 0.10:
            score -= 25; redenen.append(f"zeer hoge yield ({y*100:.1f}%) — kan risicosignaal zijn")
        elif y > 0.07:
            score -= 10; redenen.append(f"hoge yield ({y*100:.1f}%)")
    score = max(0, min(100, score))
    label = "Sterk" if score >= 75 else ("Redelijk" if score >= 50 else "Zwak")
    return score, label, "; ".join(redenen) or "geen bijzonderheden"


def bijdrage_analyse(posities_df: pd.DataFrame, verkopen,
                     dividend_df: pd.DataFrame) -> pd.DataFrame:
    """
    Volledige attributie van je totale resultaat per product:
    ongerealiseerd (open posities) + gerealiseerd (verkopen) + ontvangen dividend.
    Laat zien welke posities je rendement trokken en welke het drukten.
    """
    rijen: dict[str, dict] = {}

    def regel(naam):
        return rijen.setdefault(naam, {"Ongerealiseerd": 0.0, "Gerealiseerd": 0.0,
                                       "Dividend": 0.0})

    if posities_df is not None and not posities_df.empty:
        for _, r in posities_df.iterrows():
            res = r.get("Resultaat (EUR)")
            if res is not None and not pd.isna(res):
                regel(r["Product"])["Ongerealiseerd"] += float(res)
    for v in verkopen or []:
        regel(v.product)["Gerealiseerd"] += float(v.resultaat_eur)
    if dividend_df is not None and not dividend_df.empty:
        for _, r in dividend_df.iterrows():
            regel(r["Product"])["Dividend"] += float(r["Netto (EUR)"])

    if not rijen:
        return pd.DataFrame()
    df = pd.DataFrame([{"Product": p, **w} for p, w in rijen.items()])
    df["Totaal"] = df["Ongerealiseerd"] + df["Gerealiseerd"] + df["Dividend"]
    return df.sort_values("Totaal", ascending=False).reset_index(drop=True)


def sector_overzicht(posities_df: pd.DataFrame) -> pd.DataFrame:
    """
    Groepeer de open posities per sector: waarde, gewicht en resultaat.
    Verwacht een posities-DataFrame met kolommen Sector, Waarde (EUR),
    Inleg (EUR) en Resultaat (EUR).
    """
    bruikbaar = posities_df.dropna(subset=["Waarde (EUR)"])
    if bruikbaar.empty:
        return pd.DataFrame()
    totaal = bruikbaar["Waarde (EUR)"].sum()
    groepen = bruikbaar.groupby("Sector").agg(
        **{
            "Waarde (EUR)": ("Waarde (EUR)", "sum"),
            "Inleg (EUR)": ("Inleg (EUR)", "sum"),
            "Resultaat (EUR)": ("Resultaat (EUR)", "sum"),
            "Posities": ("Product", "count"),
        }
    ).reset_index()
    groepen["Gewicht (%)"] = groepen["Waarde (EUR)"] / totaal * 100
    groepen["Resultaat (%)"] = groepen["Resultaat (EUR)"] / groepen["Inleg (EUR)"] * 100
    return groepen.sort_values("Waarde (EUR)", ascending=False)
