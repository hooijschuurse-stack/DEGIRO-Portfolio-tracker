"""
DEGIRO Account.csv (Rekeningoverzicht) — de enige databron.

Anders dan Transactions.csv bevat dit bestand álles: koop/verkoop, dividend,
dividendbelasting, transactiekosten, rente én stortingen/opnames. We halen er
drie dingen uit:

1. **transacties** — als `Transactie`-objecten (zelfde vorm als degiro.py), zodat
   de rest van de app (FIFO, splits, ISIN-merge, rendement) ongewijzigd werkt.
2. **dividenden** — bruto, ingehouden belasting en netto per uitkering.
3. **kasstromen** — echte stortingen (+) en opnames (-).

Formaat-eigenaardigheden van Account.csv:
- Kolom 'Mutatie' bevat de **valuta** (EUR/USD); de naamloze kolom ernaast het
  **bedrag**. (Bij Transactions.csv is dat net andersom.)
- Aantal en koers van een trade staan in de **omschrijving** ("Koop 3 @ 79,67 USD").
- Bij een trade in vreemde valuta staat het EUR-bedrag in de 'Valuta Debitering'-
  /'Creditering'-regel met hetzelfde Order Id; we groeperen daarom per order en
  tellen alle EUR-poten op om het echte EUR-totaal (incl. kosten) te krijgen.
"""

from dataclasses import dataclass, field
from datetime import datetime
import io
import re

import pandas as pd

from degiro import Transactie

# "Koop 3 @ 79,67 USD", evt. met prefix "OVERNAME:", "STOCK DIVIDEND:",
# "WIJZIGING ISIN:". Aantal/koers in Nederlands formaat (1.234,56).
_TRADE = re.compile(
    r"(?:OVERNAME:|STOCK DIVIDEND:|WIJZIGING ISIN:)?\s*"
    r"(Koop|Verkoop)\s+([\d.,]+)\s*@\s*([\d.,]+)\s*([A-Za-z]+)",
    re.IGNORECASE,
)

# 'reservation ideal' hoort erbij: dat is een iDEAL-storting die nog niet als
# 'iDEAL Deposit' is afgewikkeld (settlement valt soms buiten de export). Door
# het SIGNED mee te tellen vallen afgewikkelde reserveringen (+X dan −X) vanzelf
# weg en blijft alleen verse, nog niet-afgewikkelde inleg over.
_DEPOSIT = ("ideal deposit", "ideal storting", "flatex storting", "reservation ideal")
_WITHDRAWAL = ("flatex withdrawal", "flatex terugstorting", "sepa instant terugstorting")
_KOSTEN = ("transactiekosten", "aansluitingskosten", "externe kosten", "connectiekosten")
# Kosten die NIET bij een transactie horen (aansluitings-/ADR-kosten). De
# transactiekosten zitten al in de aankoop-/verkoopprijs en mogen niet dubbel.
_KOSTEN_OVERIG = ("aansluitingskosten", "externe kosten", "connectiekosten", "adr", "gdr")


@dataclass
class AccountData:
    transacties: list[Transactie] = field(default_factory=list)
    dividenden: pd.DataFrame = field(default_factory=pd.DataFrame)
    kasstromen: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    dividend_per_dag: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    kosten_per_dag: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    kosten_totaal: float = 0.0
    rente_totaal: float = 0.0
    huidige_cash: float = 0.0      # meest recente EUR-saldo (DEGIRO-rekening)


def _nl_getal(s) -> float:
    """Nederlands getal: '1.234,56' -> 1234.56, '79,67' -> 79.67, '1.448' -> 1448."""
    if s is None:
        return 0.0
    s = str(s).strip()
    if s == "" or s.lower() == "nan":
        return 0.0
    return float(s.replace(".", "").replace(",", "."))


def _vind(kolommen, *aliassen):
    for naam in kolommen:
        for a in aliassen:
            if str(naam).strip().lower() == a.lower():
                return naam
    return None


def lees_account(bestand, fx_nu: dict[str, float] | None = None) -> AccountData:
    """
    Lees een DEGIRO Account.csv en geef trades, dividenden en kasstromen terug.
    `fx_nu` (valuta -> EUR per eenheid) wordt gebruikt voor dividend/kasstromen
    in vreemde valuta zonder eigen wisselkoers in het bestand.
    """
    fx_nu = fx_nu or {}
    if hasattr(bestand, "read"):
        inhoud = bestand.read()
        if isinstance(inhoud, bytes):
            inhoud = inhoud.decode("utf-8-sig")
        df = pd.read_csv(io.StringIO(inhoud), dtype=str)
    else:
        df = pd.read_csv(bestand, dtype=str, encoding="utf-8-sig")

    kol = list(df.columns)
    k_datum = _vind(kol, "Datum", "Date")
    k_oms = _vind(kol, "Omschrijving", "Description")
    k_isin = _vind(kol, "ISIN")
    k_prod = _vind(kol, "Product")
    k_fx = _vind(kol, "FX")
    k_order = _vind(kol, "Order Id", "OrderId", "Order ID")
    k_mut = _vind(kol, "Mutatie", "Mutation")
    if not (k_datum and k_oms and k_mut):
        raise ValueError(f"Geen DEGIRO Account.csv (kolommen ontbreken). Gevonden: {kol}")
    # Bedrag staat in de naamloze kolom direct ná 'Mutatie' (die de valuta bevat).
    mut_idx = kol.index(k_mut)
    k_bedrag = kol[mut_idx + 1] if mut_idx + 1 < len(kol) else None
    # Saldo: kolom 'Saldo' bevat de valuta, de naamloze kolom erná het bedrag.
    k_saldo = _vind(kol, "Saldo", "Balance")
    sal_idx = kol.index(k_saldo) if k_saldo else -1
    k_saldo_bedrag = kol[sal_idx + 1] if 0 <= sal_idx + 1 < len(kol) else None

    # Meest recente EUR-saldo (= huidige DEGIRO-cash). Bestand is nieuw→oud,
    # dus de eerste EUR-saldoregel is de actuele stand.
    huidige_cash = 0.0
    if k_saldo and k_saldo_bedrag:
        for _, r in df.iterrows():
            if str(r[k_saldo]).strip() == "EUR" and pd.notna(r[k_saldo_bedrag]):
                huidige_cash = _nl_getal(r[k_saldo_bedrag])
                break

    # Alle regels normaliseren naar dicts.
    regels = []
    for _, r in df.iterrows():
        oms = str(r[k_oms]).strip() if pd.notna(r[k_oms]) else ""
        if not oms or oms.lower() == "nan":
            continue
        try:
            datum = datetime.strptime(str(r[k_datum]).strip(), "%d-%m-%Y")
        except (ValueError, TypeError):
            continue
        regels.append({
            "datum": datum,
            "oms": oms,
            "isin": str(r[k_isin]).strip() if k_isin and pd.notna(r[k_isin]) else "",
            "product": str(r[k_prod]).strip() if k_prod and pd.notna(r[k_prod]) else "",
            "valuta": str(r[k_mut]).strip() if pd.notna(r[k_mut]) else "EUR",
            "bedrag": _nl_getal(r[k_bedrag]) if k_bedrag else 0.0,
            "fx": _nl_getal(r[k_fx]) if k_fx and pd.notna(r[k_fx]) else 0.0,
            "order": str(r[k_order]).strip() if k_order and pd.notna(r[k_order]) else "",
        })

    data = AccountData()
    data.transacties = _bouw_transacties(regels)
    data.dividenden = _bouw_dividenden(regels, fx_nu)
    data.kasstromen = _bouw_kasstromen(regels)
    data.dividend_per_dag = _per_dag(
        (x, _naar_eur(x, fx_nu)) for x in regels
        if ("dividend" in x["oms"].lower() or "kapitaalsuitkering" in x["oms"].lower())
    )
    data.kosten_per_dag = _per_dag(
        (x, _naar_eur(x, fx_nu)) for x in regels
        if any(k in x["oms"].lower() for k in _KOSTEN_OVERIG)
    )
    data.kosten_totaal = sum(
        x["bedrag"] for x in regels
        if x["valuta"] == "EUR" and any(k in x["oms"].lower() for k in _KOSTEN))
    data.rente_totaal = sum(
        x["bedrag"] for x in regels if "interest" in x["oms"].lower() or "rente" in x["oms"].lower())
    data.huidige_cash = huidige_cash
    return data


def _per_dag(paren) -> pd.Series:
    """Bouw een dagreeks (EUR) uit (regel, bedrag)-paren; telt per dag op."""
    per = {}
    for x, eur in paren:
        dag = pd.Timestamp(x["datum"].date())
        per[dag] = per.get(dag, 0.0) + eur
    return pd.Series(per).sort_index() if per else pd.Series(dtype=float)


def _bouw_transacties(regels: list[dict]) -> list[Transactie]:
    """Reconstrueer koop/verkoop-transacties, per Order Id gegroepeerd."""
    # Groepeer op order; regels zonder order krijgen een unieke sleutel.
    groepen: dict = {}
    for i, x in enumerate(regels):
        sleutel = x["order"] if x["order"] else f"__los_{i}"
        groepen.setdefault(sleutel, []).append(x)

    transacties = []
    for groep in groepen.values():
        trade_regels = [(x, _TRADE.match(x["oms"])) for x in groep]
        trade_regels = [(x, m) for x, m in trade_regels if m]
        if not trade_regels:
            continue

        eerste, m0 = trade_regels[0]
        # Teken PER regel: een order kan zowel een koop als verkoop bevatten
        # (bijv. de rebooking bij een overname: Koop 130 + Verkoop 130 = 0).
        aantal = sum(
            _nl_getal(m.group(2)) * (1.0 if m.group(1).lower() == "koop" else -1.0)
            for _, m in trade_regels
        )
        koers = _nl_getal(m0.group(3))
        lokale_valuta = m0.group(4).upper()

        # EUR-poten van de hele order: trade-bedrag (bij EUR), Valuta
        # Debitering/Creditering (bij vreemde valuta) én de kosten.
        eur_poten = sum(x["bedrag"] for x in groep if x["valuta"] == "EUR")
        fee = sum(x["bedrag"] for x in groep
                  if x["valuta"] == "EUR" and any(k in x["oms"].lower() for k in _KOSTEN))
        totaal_eur = eur_poten
        waarde_eur = totaal_eur - fee

        if aantal == 0:
            continue  # bijv. 'STOCK DIVIDEND: Koop 0 @ 0' — niets te boeken
        transacties.append(Transactie(
            datum=eerste["datum"], product=eerste["product"], isin=eerste["isin"],
            aantal=aantal, koers=koers, valuta=lokale_valuta,
            waarde_eur=waarde_eur, kosten_eur=fee, totaal_eur=totaal_eur,
        ))

    transacties.sort(key=lambda t: t.datum)
    return transacties


def _naar_eur(x: dict, fx_nu: dict) -> float:
    """Reken een mutatie om naar EUR (eigen FX-kolom eerst, anders huidige koers)."""
    if x["valuta"] == "EUR":
        return x["bedrag"]
    if x["fx"]:  # FX = vreemde valuta per EUR (bijv. 1,1545 USD/EUR)
        return x["bedrag"] / x["fx"]
    return x["bedrag"] * fx_nu.get(x["valuta"], 1.0)


def _bouw_dividenden(regels: list[dict], fx_nu: dict) -> pd.DataFrame:
    rijen = []
    for x in regels:
        s = x["oms"].lower()
        if "dividendbelasting" in s or ("dividend" in s and "tax" in s):
            soort = "belasting"
        elif "dividend" in s or "kapitaalsuitkering" in s:
            soort = "bruto"
        else:
            continue
        eur = _naar_eur(x, fx_nu)
        rijen.append({
            "Datum": x["datum"].date(), "Product": x["product"], "ISIN": x["isin"],
            "Bruto (EUR)": eur if soort == "bruto" else 0.0,
            "Belasting (EUR)": eur if soort == "belasting" else 0.0,
        })
    if not rijen:
        return pd.DataFrame()
    df = pd.DataFrame(rijen)
    samen = df.groupby(["Datum", "Product", "ISIN"], as_index=False).agg(
        **{"Bruto (EUR)": ("Bruto (EUR)", "sum"), "Belasting (EUR)": ("Belasting (EUR)", "sum")})
    samen["Netto (EUR)"] = samen["Bruto (EUR)"] + samen["Belasting (EUR)"]
    return samen.sort_values("Datum", ascending=False)


def _bouw_kasstromen(regels: list[dict]) -> pd.Series:
    """
    Echte externe stortingen (+) en opnames (-) per dag, in EUR.
    Cash sweeps, overboekingen naar de flatex-geldrekening, iDEAL-reserveringen
    en geldmarktfonds-mutaties zijn intern en tellen NIET mee.
    """
    per_dag: dict = {}
    for x in regels:
        s = x["oms"].lower()
        if not (any(w in s for w in _DEPOSIT) or any(w in s for w in _WITHDRAWAL)):
            continue
        # Signed bedrag: stortingen staan positief, opnames negatief, en
        # gepaarde interne 'Processed Flatex Withdrawal'-regels (+X/-X) heffen
        # elkaar vanzelf op.
        dag = pd.Timestamp(x["datum"].date())
        per_dag[dag] = per_dag.get(dag, 0.0) + x["bedrag"]
    per_dag = {d: b for d, b in per_dag.items() if abs(b) > 1e-9}
    if not per_dag:
        return pd.Series(dtype=float)
    return pd.Series(per_dag).sort_index()
