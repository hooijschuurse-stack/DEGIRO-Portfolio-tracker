"""
DEGIRO Transactions.csv parser.

Leest de transactie-export van DEGIRO (Activiteit > Transacties > Exporteren > CSV)
en zet elke regel om naar een nette Transactie. Werkt met zowel de Nederlandse
als de Engelse export.

Het DEGIRO-formaat heeft een eigenaardigheid: valutakolommen hebben geen naam.
De kolom rechts van "Koers", "Lokale waarde" en "Totaal" bevat de valuta (EUR/USD/...).
Daar houden we hieronder rekening mee.
"""

from dataclasses import dataclass
from datetime import datetime
import io

import pandas as pd


@dataclass
class Transactie:
    datum: datetime
    product: str
    isin: str
    aantal: float          # positief = koop, negatief = verkoop
    koers: float           # koers per stuk in lokale valuta
    valuta: str            # valuta van de koers (EUR, USD, ...)
    waarde_eur: float      # waarde van de transactie in EUR (negatief bij koop)
    kosten_eur: float      # transactiekosten in EUR (negatief)
    totaal_eur: float      # totaal in EUR incl. kosten (negatief bij koop)


# Per veld de kolomnamen waaronder DEGIRO het kan exporteren (NL en EN).
_ALIASSEN = {
    "datum": ["Datum", "Date"],
    "tijd": ["Tijd", "Time"],
    "product": ["Product"],
    "isin": ["ISIN"],
    "aantal": ["Aantal", "Quantity", "Number"],
    "koers": ["Koers", "Price"],
    "waarde": ["Waarde", "Value"],
    "totaal": ["Totaal", "Total"],
}


def _vind_kolom(kolommen, aliassen):
    """
    Zoek de echte kolomnaam die bij een van de aliassen hoort.
    DEGIRO heeft meerdere exportformaten: soms heet de kolom 'Totaal',
    soms 'Totaal EUR'. Daarom matchen we eerst exact, daarna op voorvoegsel.
    """
    for naam in kolommen:
        for alias in aliassen:
            if str(naam).strip().lower() == alias.lower():
                return naam
    for naam in kolommen:
        for alias in aliassen:
            if str(naam).strip().lower().startswith(alias.lower() + " "):
                return naam
    return None


def _vind_kosten_kolom(kolommen):
    """De kostenkolom heet 'Transactiekosten en/of ...' of 'Transaction and/or ...'."""
    for naam in kolommen:
        s = str(naam).strip().lower()
        if s.startswith("transactiekosten") or s.startswith("transaction"):
            return naam
    return None


def _naar_getal(x):
    """Zet een DEGIRO-getal om naar float. Kan '1.234,56', '1234.56' of leeg zijn."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return 0.0
    s = str(x).strip()
    if s == "" or s.lower() == "nan":
        return 0.0
    if "," in s and "." in s:
        # Nederlands formaat met duizendtallen: 1.234,56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def lees_transacties(bestand) -> list[Transactie]:
    """
    Lees een DEGIRO Transactions.csv en geef een lijst Transacties terug,
    gesorteerd op datum (oudste eerst).

    `bestand` mag een pad zijn of een geüpload bestand uit Streamlit.
    """
    if hasattr(bestand, "read"):
        inhoud = bestand.read()
        if isinstance(inhoud, bytes):
            inhoud = inhoud.decode("utf-8-sig")
        df = pd.read_csv(io.StringIO(inhoud), dtype=str)
    else:
        df = pd.read_csv(bestand, dtype=str, encoding="utf-8-sig")

    kolommen = list(df.columns)
    k = {veld: _vind_kolom(kolommen, aliassen) for veld, aliassen in _ALIASSEN.items()}
    k["kosten"] = _vind_kosten_kolom(kolommen)

    ontbreekt = [veld for veld in ("datum", "product", "isin", "aantal", "koers", "totaal") if k[veld] is None]
    if ontbreekt:
        raise ValueError(
            f"Dit lijkt geen DEGIRO Transactions.csv: kolommen niet gevonden: {ontbreekt}. "
            f"Gevonden kolommen: {kolommen}"
        )

    # De (naamloze) valutakolom staat direct rechts van de koerskolom.
    # In sommige exports ontbreekt die; dan valt de valuta terug op EUR.
    koers_idx = kolommen.index(k["koers"])
    valuta_kolom = None
    if koers_idx + 1 < len(kolommen):
        volgende = str(kolommen[koers_idx + 1]).strip()
        if volgende == "" or volgende.startswith("Unnamed"):
            valuta_kolom = kolommen[koers_idx + 1]

    transacties = []
    for _, rij in df.iterrows():
        isin = str(rij[k["isin"]]).strip() if pd.notna(rij[k["isin"]]) else ""
        if not isin or isin.lower() == "nan":
            continue  # lege regels overslaan

        tijd = "00:00"
        if k["tijd"] and pd.notna(rij[k["tijd"]]):
            tijd = str(rij[k["tijd"]]).strip()
        datum = datetime.strptime(f"{str(rij[k['datum']]).strip()} {tijd}", "%d-%m-%Y %H:%M")

        valuta = "EUR"
        if valuta_kolom is not None and pd.notna(rij[valuta_kolom]):
            v = str(rij[valuta_kolom]).strip()
            if v:
                valuta = v

        transacties.append(Transactie(
            datum=datum,
            product=str(rij[k["product"]]).strip(),
            isin=isin,
            aantal=_naar_getal(rij[k["aantal"]]),
            koers=_naar_getal(rij[k["koers"]]),
            valuta=valuta,
            waarde_eur=_naar_getal(rij[k["waarde"]]) if k["waarde"] else 0.0,
            kosten_eur=_naar_getal(rij[k["kosten"]]) if k["kosten"] else 0.0,
            totaal_eur=_naar_getal(rij[k["totaal"]]),
        ))

    transacties.sort(key=lambda t: t.datum)
    return transacties
