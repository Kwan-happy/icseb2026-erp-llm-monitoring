"""Pure sampling primitives for the synthetic "normal background" POs.

Implements every distribution in params.json (the FROZEN parameter file) key-for-key.
No Odoo/DB imports, no side effects beyond reading params.json — this module only draws
numbers. Determinism: everything flows through one numpy.random.default_rng(seed) instance
seeded from params.json["seed"], so the same seed reproduces byte-identical output.

Design mirrors params.json's "bottom-up" philosophy: a product catalog (standard_price per
product) and a vendor set are sampled once; each PO is then built from lines so
amount_total = sum(qty * unit_price) EMERGES rather than being asserted (see
params_derivation.md).
"""
import datetime
import json
import pathlib

import numpy as np

# ---------- params.json loading ----------
# Resolved relative to this file (same directory), in the spirit of extract.py's
# ROOT = Path(__file__).resolve().parents[1] pattern — params.json is a sibling here.
PARAMS_PATH = pathlib.Path(__file__).resolve().parent / "params.json"


def load_params(path=None):
    p = pathlib.Path(path) if path else PARAMS_PATH
    return json.loads(p.read_text(encoding="utf-8"))


class Samplers:
    """Stateful sampler bundle: one RNG + a catalog/vendor setup derived once at init,
    plus draw methods for each params.json distribution. All randomness flows through
    self.rng, so construction order of draws determines reproducibility."""

    def __init__(self, seed=None, params=None, params_path=None):
        self.params = params if params is not None else load_params(params_path)
        self.seed = seed if seed is not None else self.params["seed"]
        self.rng = np.random.default_rng(self.seed)

        self._build_catalog()          # product_standard_price -> self.catalog
        self._build_vendor_affinity()  # vendor_product_affinity -> category maps

    # ---------- product catalog (product_standard_price) ----------
    def _build_catalog(self):
        p = self.params["product_standard_price"]
        n = self.params["counts"]["n_products"]
        mu = np.log(p["median"])  # lognormal mean of the underlying normal = log(median)
        prices = self.rng.lognormal(mean=mu, sigma=p["sigma_log"], size=n)
        self.catalog = np.clip(prices, p["min"], None)  # floor only, no upper clip specified

    # ---------- vendor <-> product category affinity ----------
    def _build_vendor_affinity(self):
        aff = self.params["vendor_product_affinity"]
        n_vendors = self.params["counts"]["n_vendors"]
        n_cat = aff["n_categories"]
        ppc = aff["products_per_category"]
        assert n_cat * ppc == len(self.catalog), (
            "n_categories * products_per_category must equal n_products (params.json contract)")

        # product index -> category index (contiguous blocks of ppc products each)
        self.product_category = np.arange(len(self.catalog)) // ppc
        self.category_products = [
            np.where(self.product_category == c)[0] for c in range(n_cat)
        ]
        # "one_per_vendor_round_robin": vendor i's primary category = i mod n_categories
        self.vendor_primary_category = np.arange(n_vendors) % n_cat

    def pick_products(self, vendor_idx_array):
        """Vectorized product draw for an array of vendor indices (one draw per line).
        Each draw is in-category with prob in_category_prob, else uniformly cross-category
        (i.e. any product outside that vendor's primary-category block)."""
        aff = self.params["vendor_product_affinity"]
        in_cat_prob = aff["in_category_prob"]
        vendor_idx_array = np.asarray(vendor_idx_array)
        n = len(vendor_idx_array)
        out = np.empty(n, dtype=int)
        if n == 0:
            return out

        in_cat = self.rng.random(n) < in_cat_prob
        primary_cats = self.vendor_primary_category[vendor_idx_array]
        n_categories = len(self.category_products)
        all_products = np.arange(len(self.catalog))

        # Group by primary category (only n_categories iterations, not n) to stay vectorized.
        for c in range(n_categories):
            in_mask = in_cat & (primary_cats == c)
            cnt = int(in_mask.sum())
            if cnt:
                out[in_mask] = self.rng.choice(self.category_products[c], size=cnt)

            cross_mask = (~in_cat) & (primary_cats == c)
            cnt = int(cross_mask.sum())
            if cnt:
                allowed = np.setdiff1d(all_products, self.category_products[c])
                out[cross_mask] = self.rng.choice(allowed, size=cnt)
        return out

    # ---------- lines_per_po / qty_per_line: zero-truncated Poisson ----------
    def _trunc_poisson(self, lam, n, lo, hi):
        """Poisson(lam) CLIPPED to [lo, hi] — zeros are raised to `min`, not resampled.

        params.json says only `"dist": "poisson_truncated"` with min/max, which admits two
        readings: clip-to-min, or reject-and-redraw the zeros (the textbook zero-truncated
        Poisson). They are NOT equivalent — clipping leaves extra mass at 1 and yields a
        smaller PO. The frozen calibration in params_derivation.md ("Verified emergent
        distribution", seed 123, 300k POs) is reproduced by CLIPPING to 4 significant
        figures (median 4895 vs 4890, mean 7075 vs 7079, p75 9547 vs 9533, p90 15862 vs
        15834, p95 20875 vs 20872, 5.62% vs 5.6% above threshold); redrawing instead gives
        median 5678 / 6.8% above threshold. Clipping is therefore the interpretation the
        calibration uses."""
        return np.clip(self.rng.poisson(lam, size=n), lo, hi)

    def sample_lines_per_po(self, n):
        p = self.params["lines_per_po"]
        return self._trunc_poisson(p["lambda"], n, p["min"], p["max"])

    def sample_qty_per_line(self, n):
        p = self.params["qty_per_line"]
        return self._trunc_poisson(p["lambda"], n, p["min"], p["max"])

    # ---------- unit_price_noise: clipped normal multiplier ----------
    def sample_unit_price_noise(self, n):
        p = self.params["unit_price_noise"]
        vals = self.rng.normal(p["mean"], p["sd"], size=n)
        return np.clip(vals, p["clip_min"], p["clip_max"])

    # ---------- vendor_assignment: finite normalized Zipf ----------
    def vendor_weights(self):
        """Weights proportional to 1/rank^s for rank 1..n, normalized to sum to 1.
        Deliberately NOT numpy.random.zipf (unbounded, needs s>1) — this is a finite
        categorical distribution over exactly n=params vendors."""
        va = self.params["vendor_assignment"]
        n, s = va["n"], va["s"]
        ranks = np.arange(1, n + 1)
        w = 1.0 / np.power(ranks, s)
        return w / w.sum()

    def vendor_allocation(self, total):
        """Deterministic per-vendor PO-count allocation: largest-remainder rounding of
        weights * total. No RNG draw involved, so counts always sum EXACTLY to `total`
        (unlike sampling `total` independent categorical draws, which would only match
        the target shares approximately)."""
        w = self.vendor_weights()
        raw = w * total
        floor = np.floor(raw).astype(int)
        remainder = int(total - floor.sum())
        fracs = raw - floor
        # give the leftover POs to the vendors with the largest fractional remainder
        order = np.argsort(-fracs)
        floor[order[:remainder]] += 1
        return floor

    def sample_vendor_categorical(self, n_draws):
        """Alternative to vendor_allocation: n_draws independent categorical draws from
        the Zipf weights (matches the params.json dist name literally). Counts will only
        approximate the target shares — prefer vendor_allocation() when exact totals
        matter (e.g. driving order_timing so per-vendor PO counts are known upfront)."""
        w = self.vendor_weights()
        return self.rng.choice(len(w), size=n_draws, p=w)

    # ---------- order_timing: per-vendor exponential inter-arrivals ----------
    def order_timing_dates(self, vendor_counts):
        """For each vendor (given its allocated PO count), draw exponential inter-arrivals
        with mean = generation_period_days / that vendor's PO count, place them inside the
        generation_period window (wrapped via modulo so every draw lands inside the window
        regardless of tail draws), and return a sorted list of datetime.date per vendor."""
        gp = self.params["generation_period"]
        start = datetime.date.fromisoformat(gp["start_date"])
        end = datetime.date.fromisoformat(gp["end_date"])
        period_days = (end - start).days + 1

        result = []
        for count in vendor_counts:
            count = int(count)
            if count <= 0:
                result.append([])
                continue
            mean = period_days / count
            inter_arrivals = self.rng.exponential(mean, size=count)
            offsets = np.mod(np.cumsum(inter_arrivals), period_days)  # clip into window
            offsets.sort()
            result.append([start + datetime.timedelta(days=int(o)) for o in offsets])
        return result

    # ---------- bottom-up PO line building ----------
    def build_po_lines(self, vendor_idx, catalog=None):
        """Build one PO's line list for `vendor_idx`. Returns a list of dicts with
        product/qty/standard_price/unit_price; amount_total = sum(qty * unit_price)
        EMERGES from the caller summing these, per params.json's bottom-up design."""
        catalog = self.catalog if catalog is None else catalog
        n_lines = int(self.sample_lines_per_po(1)[0])

        product_idx = self.pick_products(np.full(n_lines, vendor_idx))
        qty = self.sample_qty_per_line(n_lines)
        noise = self.sample_unit_price_noise(n_lines)

        lines = []
        for i in range(n_lines):
            p_idx = int(product_idx[i])
            standard_price = float(catalog[p_idx])
            unit_price = round(standard_price * float(noise[i]), 2)
            lines.append({
                "product_idx": p_idx,
                "qty": int(qty[i]),
                "standard_price": standard_price,
                "unit_price": unit_price,
            })
        return lines
