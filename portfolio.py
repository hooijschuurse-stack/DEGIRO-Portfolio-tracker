"""
Positie-engine: bouwt uit de transactielijst de huidige posities en de
gerealiseerde (verkochte) posities op, volgens het FIFO-principe
(first in, first out: je oudste stukken worden als eerste verkocht).
"""

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime

from degiro import Transactie


def koppel_corporate_actions(transacties: list[Transactie]) -> tuple[list[Transactie], dict[str, str]]:
    """
    Ruim 'wash'-transacties op die DEGIRO inboekt bij ISIN-wijzigingen en
    herboekingen (geen echte aan-/verkopen).

    Herkenning: twee transacties op dezelfde dag, met gelijk aantal, gelijke
    waarde in EUR (tot op de cent) en tegengesteld teken. Dat is geen handel,
    maar een administratieve overboeking. Voorbeelden in de praktijk:
    - Royal Dutch Shell (GB00B03MLX29) → Shell plc (GB00BP6MXD84)
    - Eurocommercial ISIN-wijziging
    - Takeaway intern omgeboekt via een tijdelijk instrument

    - Zelfde ISIN  → puur een herboeking: beide regels verwijderen.
    - Andere ISIN  → naamwijziging: alle transacties van de oude ISIN
      omhangen naar de nieuwe (zodat de kostprijs meeloopt) en het paar zelf
      verwijderen. Zonder dit ontstaat een neppe 'verkoop' met fantoomwinst en
      een verkeerde gemiddelde aankoopkoers.

    Geeft de opgeschoonde lijst terug plus een {oude_isin: nieuwe_isin}-map.
    """
    per_dag: dict = defaultdict(list)
    for idx, tx in enumerate(transacties):
        per_dag[tx.datum.date()].append(idx)

    verwijderen: set[int] = set()
    hernoem: dict[str, str] = {}
    for idxs in per_dag.values():
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                ia, ib = idxs[x], idxs[y]
                if ia in verwijderen or ib in verwijderen:
                    continue
                a, b = transacties[ia], transacties[ib]
                if (abs(abs(a.aantal) - abs(b.aantal)) < 1e-6
                        and a.aantal * b.aantal < 0
                        and abs(a.waarde_eur) > 0
                        and abs(abs(a.waarde_eur) - abs(b.waarde_eur)) < 0.01):
                    verwijderen.add(ia)
                    verwijderen.add(ib)
                    if a.isin != b.isin:
                        nieuw = a.isin if a.aantal > 0 else b.isin
                        oud = b.isin if a.aantal > 0 else a.isin
                        hernoem[oud] = nieuw

    def eindbestemming(isin: str) -> str:
        gezien = set()
        while isin in hernoem and isin not in gezien:
            gezien.add(isin)
            isin = hernoem[isin]
        return isin

    opgeschoond = []
    for idx, tx in enumerate(transacties):
        if idx in verwijderen:
            continue
        doel = eindbestemming(tx.isin)
        if doel != tx.isin:
            tx = replace(tx, isin=doel)
        opgeschoond.append(tx)
    return opgeschoond, hernoem


@dataclass
class Verkoop:
    """Eén verkooptransactie, met de bijbehorende kostprijs (gemiddelde-kostprijs)."""
    datum: datetime
    product: str
    isin: str
    aantal: float
    koers: float           # verkoopkoers per stuk (lokale valuta)
    valuta: str
    opbrengst_eur: float   # netto opbrengst in EUR (na kosten)
    kostprijs_eur: float   # wat deze stukken gemiddeld gekost hebben (incl. kosten)

    @property
    def resultaat_eur(self) -> float:
        return self.opbrengst_eur - self.kostprijs_eur

    @property
    def resultaat_pct(self) -> float:
        if self.kostprijs_eur == 0:
            return 0.0
        return self.resultaat_eur / self.kostprijs_eur * 100


@dataclass
class Positie:
    """
    Een huidige (open) positie in één product, gewaardeerd op
    **gemiddelde-kostprijs** (de methode van Portfolio Dividend Tracker, art. 282):
    de GAK is het totale aankoopbedrag gedeeld door het aantal stuks, en een
    verkoop verlaagt de kostbasis met die gemiddelde prijs (niet FIFO). Bij een
    volledige verkoop start de berekening opnieuw.
    """
    product: str
    isin: str
    valuta: str
    aantal: float = 0.0
    kosten_eur: float = 0.0          # gemiddelde-kostbasis van de huidige stukken
    eerste_aankoop: datetime | None = None

    @property
    def gak_eur(self) -> float:
        """Gemiddelde aankoopkoers in EUR (incl. kosten)."""
        return self.kosten_eur / self.aantal if self.aantal else 0.0


def verwerk_transacties(transacties: list[Transactie]) -> tuple[list[Positie], list[Verkoop]]:
    """
    Loop chronologisch door alle transacties en geef terug:
    - de open posities van dit moment (gemiddelde-kostprijs, PDT-methode)
    - alle verkopen, met de gemiddelde-kostprijs als kostbasis en het resultaat
    """
    posities: dict[str, Positie] = {}
    verkopen: list[Verkoop] = []

    for tx in transacties:
        pos = posities.get(tx.isin)
        if pos is None:
            pos = Positie(product=tx.product, isin=tx.isin, valuta=tx.valuta)
            posities[tx.isin] = pos
        # Productnaam en valuta kunnen wisselen tussen exports; pak de meest recente.
        pos.product = tx.product
        pos.valuta = tx.valuta

        if tx.aantal > 0:
            # (Her)opening van een positie → start de kostprijs-telling opnieuw.
            if pos.aantal <= 1e-9:
                pos.eerste_aankoop = tx.datum
                pos.aantal = 0.0
                pos.kosten_eur = 0.0
            # Koop: totaal_eur is negatief (geld eruit), kostprijs is dus -totaal.
            pos.aantal += tx.aantal
            pos.kosten_eur += -tx.totaal_eur
        elif tx.aantal < 0:
            # Verkoop tegen de gemiddelde kostprijs van dat moment.
            gem = pos.kosten_eur / pos.aantal if pos.aantal > 1e-9 else 0.0
            verkocht = min(-tx.aantal, pos.aantal) if pos.aantal > 0 else -tx.aantal
            kostprijs = gem * verkocht
            pos.aantal -= verkocht
            pos.kosten_eur -= kostprijs
            if pos.aantal <= 1e-9:   # volledig verkocht → reset
                pos.aantal = 0.0
                pos.kosten_eur = 0.0
                pos.eerste_aankoop = None
            verkopen.append(Verkoop(
                datum=tx.datum,
                product=tx.product,
                isin=tx.isin,
                aantal=-tx.aantal,
                koers=tx.koers,
                valuta=tx.valuta,
                opbrengst_eur=tx.totaal_eur,
                kostprijs_eur=kostprijs,
            ))

    open_posities = [p for p in posities.values() if p.aantal > 1e-9]
    open_posities.sort(key=lambda p: p.kosten_eur, reverse=True)
    return open_posities, verkopen
