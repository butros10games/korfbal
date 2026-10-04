# Appearance adapter `appearance-korfbal-v1.npz`

`clip_appearance.py` merges this file into the pinned DINOv2 ViT-S/14 checkpoint
before exporting the descriptor model. It holds low-rank (LoRA, rank 16) updates
for the attention (`qkv`, `proj`) and MLP (`fc1`, `fc2`) weights of the last four
transformer blocks, plus the retrained final norm: 393,984 values, float16 for the
updates, no pickled objects. The update of a weight `W` is `W + B @ A`
(`<weight>.B` already includes the LoRA scale).

SHA-256 `40c4d238b5728e2cd1536909e8af04e50eb5aceb577ba66706d48d7c470a1f9c`.

## Provenance and licences

| Part            | Source                                                                                                                                                                                                             | Licence                                                                          |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| Backbone        | DINOv2 ViT-S/14 `dinov2_vits14_pretrain.pth` (pinned digest in `clip_appearance.py`), repository commit `7764ea0f`                                                                                                 | Apache-2.0                                                                       |
| Adapter weights | Trained by us from the backbone; no other pretrained weights involved                                                                                                                                              | Ours                                                                             |
| Training images | Player crops from 43 public, free-to-view Eyecons korfbal broadcasts (January 2025 – April 2026: Korfbal League, Korfbal League 2, reserve leagues, U19 Hoofdklasse), six 60-second sections each                  | Broadcast footage, publicly viewable; rights stay with the broadcaster and clubs |
| Identity labels | None by hand: strict tracklets from our own detector (each tracklet is one person), plus links between tracklets that a DINOv2 ViT-g/14 (Apache-2.0) per-clip appearance space ranked as mutual nearest neighbours | Derived                                                                          |

No person re-identification dataset (Market-1501, MSMT17, DukeMTMC and the like)
and no ImageNet classification weights were used. All matches of the clubs in the
labelled benchmark and held-out clips (DSC, TOP, Fortuna, DVO, KZ, LDODK, DTS,
Apollo) were excluded from training, including their reserve and youth teams.

## Training

Each crop is the engine's own input (the observed box resized to 224 x 112).
A contrastive loss pulls two crops of one identity together and pushes apart only
pairs that are certainly different people: tracklets visible in the same frame,
or crops from two matches without a common club. Every other pair is left out, so
a player who leaves and returns is never treated as a stranger. Augmentation:
box jitter, mirroring, brightness and contrast, blur, low resolution (as far or
club-camera players) and random occluders. Six epochs over 124,000 crops of
27,500 tracklets on one RTX 4090; the checkpoint after four epochs was chosen on
the development clips only.

`docs/korfbal/clip-analysis.md` ("Appearance model") has the evaluation.
