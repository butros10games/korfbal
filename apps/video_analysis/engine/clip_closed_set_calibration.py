"""Measured acceptance of automatic closed-set appearance names, per descriptor.

A candidate name's raw margin is not a probability. These bins were fitted on
the five development benchmark clips only (four broadcast, pooled over the
automatic and 1/3/5-confirmation regimes; held-out clips never chose them):
each bin is the observation-weighted precision of every candidate published at
or above that margin. With the default 0.98 acceptance, names are published
from 0.15 (DINOv2-S, and DINOv2-S with the korfbal-v1 adapter) or 0.12
(DINOv2-g/14-reg). Wrong names then stayed at or below 2% of labelled player
observations on every development clip.

The single club development clip never reached 98% (about 93% for DINOv2-S
and DINOv2-g), so club footage publishes from the same margin with a lower
measured name precision. The korfbal adapter stays near 85% on that clip at
every margin, so its calibration applies to operated (broadcast) cameras only:
on a clip the linker judged a fixed camera it publishes no appearance names.
A calibration applies only to the appearance export whose checksum the run
records; any other descriptor stays uncalibrated (confirmations and numbers
only).
"""

from __future__ import annotations

from .clip_closed_set import RECIPE, Confidence


PROVENANCE = (
    "i1 2026-10-04: cumulative observation-weighted precision of non-anchored "
    "candidates at or above each margin; development clips dsc-top-2600, "
    "fortuna-dvo-3300, dsc-top-3300, fortuna-dvo-2600 (dts-apollo-600 measured "
    "separately); regimes automatic and 1/3/5 confirmations per player"
)
# DINOv2 ViT-g/14 with registers (checkpoint 746ecb8c...a283), the same three
# bands, measured with GPU descriptors (FP16, engine crops).
GIANT_BINS = (
    (0.0, 0.8135),
    (0.02, 0.8931),
    (0.03, 0.913),
    (0.04, 0.9277),
    (0.05, 0.938),
    (0.06, 0.954),
    (0.08, 0.9696),
    (0.1, 0.9778),
    (0.12, 0.9837),
    (0.15, 0.9852),
    (0.2, 0.9891),
    (0.3, 0.9935),
)
ADAPTER_PROVENANCE = (
    "z1 2026-10-04: same method and development clips as PROVENANCE, replays of "
    "t3code/vision-mvp ac5087666 with the adapter export's own descriptors"
)
# DINOv2 ViT-S/14 with the korfbal-v1 adapter (``clip_appearance.ADAPTER``),
# CPU export of the pinned vision runtime; 2,795 judged tracklets.
ADAPTER_BINS = (
    (0.0, 0.7595),
    (0.02, 0.8482),
    (0.03, 0.87),
    (0.04, 0.8919),
    (0.05, 0.9065),
    (0.06, 0.9168),
    (0.08, 0.9419),
    (0.1, 0.9617),
    (0.12, 0.9631),
    (0.15, 0.9803),
    (0.2, 0.985),
    (0.3, 0.9844),
)
ADAPTER_SHA256 = "5b53867fec5af686ca52ac960a6d84858d78ecbce6e8a295e4b9873e71f4cd03"
# Appearance export checksum (identity_linking.sha256) -> cumulative precision bins.
BINS: dict[str, tuple[tuple[float, float], ...]] = {
    # DINOv2 ViT-S/14 banded export of the pinned CPU vision runtime.
    "7bea2c356aacb255e70987327f46a699b13d75ae569820d53e17acdcfb1963b2": (
        (0.0, 0.7539),
        (0.02, 0.83),
        (0.03, 0.8577),
        (0.04, 0.8831),
        (0.05, 0.9005),
        (0.06, 0.9266),
        (0.08, 0.9522),
        (0.1, 0.9703),
        (0.12, 0.9798),
        (0.15, 0.9896),
        (0.2, 0.991),
        (0.3, 0.9937),
    ),
    # The verified GPU giant descriptor (``clip_appearance_giant.FINGERPRINT``):
    # it loads only after reproducing the reference outputs of the code that
    # measured these bins (cosine >= 0.999; 0.99991 minimum on 3,637 real crops).
    "24086afb46662ca79f1e33b45630b601cb63cc20dc02373564cdd69dc70e3a7c": GIANT_BINS,
    ADAPTER_SHA256: ADAPTER_BINS,
}
# Descriptors whose calibration was measured on broadcast footage and does not
# hold on a fixed (club) camera: the development club clip stayed near 85%.
BROADCAST_ONLY = frozenset({ADAPTER_SHA256})


def calibrated(
    appearance_sha256: str, closed_input: str, *, fixed_camera: bool = False
) -> Confidence | None:
    """Return the measured calibration for this descriptor and footage, or None."""
    bins = BINS.get(appearance_sha256)
    if bins is None or closed_input != "tracklets":
        return None
    if fixed_camera and appearance_sha256 in BROADCAST_ONLY:
        return None
    provenance = (
        ADAPTER_PROVENANCE if appearance_sha256 == ADAPTER_SHA256 else PROVENANCE
    )
    return Confidence(provenance, f"{appearance_sha256}:{RECIPE}:{closed_input}", bins)
