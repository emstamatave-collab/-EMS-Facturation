"""Calcul indicatif du prix de vente depuis le prix d'achat TVH.

Les frais de transport et d'import sont saisis séparément.
Le prix proposé peut être remplacé manuellement dans le devis.
"""
from decimal import Decimal, ROUND_HALF_UP


def coefficient_tvh(prix_achat):
    prix = Decimal(str(prix_achat))
    if not prix.is_finite() or prix < 0:
        raise ValueError("Prix d'achat invalide")
    if prix <= 50:
        return Decimal("3.5")
    if prix <= 100:
        return Decimal("3")
    if prix <= 150:
        return Decimal("2.5")
    if prix <= 350:
        return Decimal("2")
    return Decimal("1.5")


def prix_vente_conseille(prix_achat):
    prix = Decimal(str(prix_achat))
    return (prix * coefficient_tvh(prix)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
