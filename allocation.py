"""
allocation.py — Méthodes d'allocation market-neutral.

Méthodes :
  1. Equal Weight  (EW)
  2. Equal Risk Contribution (ERC)

Contrainte market-neutral : Σ w_long = +1,  Σ w_short = -1
"""

import pandas as pd
import numpy as np
from scipy.optimize import minimize


# ═══════════════════════════════════════════════════════════════════
#  1. Equal Weight
# ═══════════════════════════════════════════════════════════════════

def equal_weight(long_list: list, short_list: list) -> pd.Series:
    """
    Allocation Equal Weight market-neutral.
    - Chaque long  : +1 / n_long
    - Chaque short : -1 / n_short

    Returns : Series(ticker → weight)
    """
    weights = {}
    n_long  = len(long_list)
    n_short = len(short_list)

    if n_long > 0:
        w_l = 1.0 / n_long
        for t in long_list:
            weights[t] = w_l

    if n_short > 0:
        w_s = -1.0 / n_short
        for t in short_list:
            weights[t] = w_s

    return pd.Series(weights)


# ═══════════════════════════════════════════════════════════════════
#  2. Equal Risk Contribution (ERC)
# ═══════════════════════════════════════════════════════════════════

def _erc_weights_long_only(cov: pd.DataFrame, tickers: list) -> np.ndarray:
    """
    Résout le problème ERC (equal risk contribution) pour un groupe
    de tickers (long seulement, poids positifs normalisés à 1).

    min  Σ_i Σ_j ( w_i * (Σ w)_i - w_j * (Σ w)_j )^2

    Utilise plusieurs points de départ pour éviter les minima locaux.
    """
    n = len(tickers)
    if n == 0:
        return np.array([])
    if n == 1:
        return np.array([1.0])

    # Sous-matrice covariance
    cov_sub = cov.loc[tickers, tickers].values

    def objective(w):
        sigma = w @ cov_sub @ w
        if sigma <= 0:
            return 1e12
        # risk contributions
        marginal = cov_sub @ w
        rc = w * marginal
        rc_target = sigma / n
        return np.sum((rc - rc_target) ** 2)

    bounds = [(1e-6, 1.0)] * n
    constraints = {"type": "eq", "fun": lambda w: np.sum(w) - 1.0}

    # Point de départ 1 : inverse de la volatilité (plus naturel pour ERC)
    vols = np.sqrt(np.diag(cov_sub))
    vols = np.where(vols > 0, vols, 1e-8)
    w_invvol = (1.0 / vols)
    w_invvol = w_invvol / w_invvol.sum()

    # Point de départ 2 : equal weight
    w_ew = np.ones(n) / n

    best_res = None
    best_fun = np.inf

    for w0 in [w_invvol, w_ew]:
        res = minimize(objective, w0, method="SLSQP",
                       bounds=bounds, constraints=constraints,
                       options={"maxiter": 1000, "ftol": 1e-15})
        if res.fun < best_fun:
            best_fun = res.fun
            best_res = res

    if best_res is not None and best_fun < 1e10:
        w_out = best_res.x
        w_out = np.maximum(w_out, 0)
        return w_out / w_out.sum()
    else:
        # fallback : inverse vol (mieux que equal weight)
        return w_invvol


def erc_weights(returns: pd.DataFrame,
                long_list: list,
                short_list: list,
                window: int = 12) -> pd.Series:
    """
    Allocation ERC market-neutral.
    On calcule la matrice de covariance sur les `window` dernières observations,
    puis on optimise séparément pour le côté long et le côté short.

    Les tickers avec données manquantes dans la fenêtre de covariance sont
    exclus de l'optimisation ERC et reçoivent un poids equal-weight.

    Returns : Series(ticker → weight), avec Σ long = +1 et Σ short = -1.
    """
    # Si pas assez de données → fallback equal weight
    if len(returns) < max(window, 3):
        return equal_weight(long_list, short_list)

    weights = {}

    def _erc_side(ticker_list, sign=1.0):
        """Calcule les poids ERC pour un côté (long ou short).
        Exclut les tickers avec NaN dans la fenêtre, les remplace par EW."""
        n_total = len(ticker_list)
        if n_total == 0:
            return

        # Fenêtre de rendements pour ce côté
        sub = returns[ticker_list].iloc[-window:]

        # Séparer : tickers avec données complètes vs tickers avec NaN
        nan_counts = sub.isna().sum()
        valid_tickers   = nan_counts[nan_counts == 0].index.tolist()
        excluded_tickers = nan_counts[nan_counts > 0].index.tolist()

        if len(valid_tickers) < 2:
            # Pas assez de tickers propres → equal weight pour tous
            w_ew = sign / n_total
            for t in ticker_list:
                weights[t] = w_ew
            return

        # Covariance propre (sans NaN)
        cov = sub[valid_tickers].cov()

        # ERC sur les tickers valides
        w_erc = _erc_weights_long_only(cov, valid_tickers)

        if not excluded_tickers:
            # Tous valides : normaliser à |1|
            for i, t in enumerate(valid_tickers):
                weights[t] = sign * w_erc[i]
        else:
            # Mix : ERC pour les valides, EW pour les exclus
            n_valid = len(valid_tickers)
            n_excl = len(excluded_tickers)
            share_erc = n_valid / n_total
            share_ew = n_excl / n_total

            for i, t in enumerate(valid_tickers):
                weights[t] = sign * w_erc[i] * share_erc
            w_ew_each = share_ew / n_excl
            for t in excluded_tickers:
                weights[t] = sign * w_ew_each

    # Long : poids positifs, somme = +1
    _erc_side(long_list, sign=1.0)
    # Short : poids négatifs, somme = -1
    _erc_side(short_list, sign=-1.0)

    return pd.Series(weights)


# ═══════════════════════════════════════════════════════════════════
#  3. Dispatch par nom
# ═══════════════════════════════════════════════════════════════════

ALLOC_FUNCS = {
    "equal_weight": lambda long, short, **kw: equal_weight(long, short),
    "erc":          lambda long, short, **kw: erc_weights(kw["returns"], long, short),
}


def allocate(method: str, long_list: list, short_list: list, **kwargs) -> pd.Series:
    """Retourne les poids market-neutral pour la méthode choisie."""
    return ALLOC_FUNCS[method](long_list, short_list, **kwargs)
