"""Calcul de tarification TVH, sans effet sur les documents existants."""
from decimal import Decimal, ROUND_HALF_UP
from tarification_tvh import coefficient_tvh

def proposition_tvh(prix_achat_eur, devise="EUR", taux_eur_mga=None):
    achat = Decimal(str(prix_achat_eur))
    if not achat.is_finite() or achat < 0:
        raise ValueError("Prix TVH invalide")
    vente_eur = (achat * coefficient_tvh(achat)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if devise == "EUR":
        return vente_eur
    if devise != "MGA":
        raise ValueError("Devise non prise en charge")
    taux = Decimal(str(taux_eur_mga))
    if not taux.is_finite() or taux <= 0:
        raise ValueError("Taux EUR/MGA invalide")
    return (vente_eur * taux).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
