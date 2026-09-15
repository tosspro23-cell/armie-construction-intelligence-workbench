# Dataset Pack — go/no-go spike (real files opened, not just dataset cards read)

Owner-requested follow-on from the SPEC-M11 planning session's Dataset Pack assessment, which
flagged that the consultant's proposed model's license citation pointed at a third-party
Hugging Face re-packaging (`sylvainHellin/ifc-bench`), not the original rights-holder's own
statement, and that this project's deterministic IFC tooling was untested against a real
multi-discipline model. This spike actually downloads and opens the real candidate files with
`ifcopenshell` and checks their license at the primary source, rather than trusting the
re-host's own dataset card -- which turned out to matter: one of its own `license.txt` files
was independently found to be wrong.

Three candidates from the original proposal were checked: **WBDG Office**, **RWTH DigitalHub**,
and **Sixty5**.

## WBDG Office — the originally-proposed candidate

**Primary source.** The file's own header (`Autodesk Revit Architecture 2011`, dated
`2011-08-11`) matches the well-known USACE ERDC-CERL (US Army Corps of Engineers Engineer
Research and Development Center) reference building trio (Duplex/Office/Clinic), long used
across the open BIM tooling ecosystem. ERDC's own published technical reports describe these
models as intended "for future experimentation by ERDC and testing by software developers and
end users" -- a strong signal, but not a crisp, citable license statement found directly from
USACE/ERDC's own site in this pass.

**The re-host's own metadata is unreliable, confirmed live.** `sylvainHellin/ifc-bench`'s own
`license.txt` for the `wbdg_office` project returned boilerplate attribution for an unrelated
BuildingSMART "Medical-Dental Test Files" project -- a real, independently-confirmed error in
the re-host's own data, not a hypothetical concern. This is exactly the risk the original
assessment flagged about trusting a dataset card over the primary source.

**Technical fit -- one real gap, found by opening the file.** 171/171 doors/windows tagged
(useful). But `IfcElementQuantity` (Qto) sets are empty for every door/window checked --
`_reconciliation_ifc_items`'s `qtos_only=True` lookup returns nothing. Real dimensions live in
the entities' own `OverallWidth`/`OverallHeight` attributes instead (Revit's export puts them
there, not in a Qto set). Left unfixed, `_compare_reconciliation_item`'s `(ifc_item["width_m"]
or 0.0)` null-coalescing would report every item as a fabricated `dimension_mismatch` against a
zero. Also confirmed: `IfcDoor.Tag` and Revit's own "Mark" Pset field are unrelated identifiers
in this file (0/30 sampled matched) -- not a blocker for this proposal specifically, since the
plan is to author our own companion schedule from the real `Tag` values, but a real trap for
anyone assuming a real IFC's `Tag` always equals its schedule Mark.

**Verdict: usable, but the weakest of the three on licensing, and needs a real code fix
(Qto-empty fallback) before it could be trusted for reconciliation.**

## RWTH DigitalHub — recommended

**Primary source: clean.** A public GitHub repository owned directly by
[RWTH-E3D](https://github.com/RWTH-E3D/DigitalHub), the RWTH Aachen University institute that
created it -- not a third-party re-host. A plain **MIT LICENSE** file at the repository root,
copyright RWTH Aachen University E3D Institute, 2020. No re-host to second-guess.

**Technical fit: the best of the three, verified by opening every discipline file.**

```
DigitalHub_FM-ARC_v2.ifc   (Architecture, ~9.0 MB)
  schema: IFC4, loads in 0.3s
  64 doors, 47 windows -- 111/111 tagged
  Height/Width in a real, STANDARD Qto_DoorBaseQuantities set, correctly in
  meters (e.g. Height=2.25, Width=1.01) -- exactly the shape
  AgentService._reconciliation_ifc_items already expects. Zero code changes
  needed, unlike WBDG.
  Storeys: B01_OKRD, E00_OKRD, E01_OKRD (basement + 2 floors)
  1026 total IfcProduct instances (vs. this project's synthetic fixture's ~16)

DigitalHub_FM-HZG_v2.ifc  (Heating,     ~20.9 MB)
DigitalHub_FM-LFT_v2.ifc  (Ventilation, ~12.7 MB)
DigitalHub_FM-SAN_v2.ifc  (Sanitary,    ~25.2 MB)
```

Four disciplines, ~68 MB total (no structural discipline is published). No PDF drawings or
door/window schedule exist anywhere in the repository (confirmed via the full repo file tree,
`gh api repos/RWTH-E3D/DigitalHub/git/trees/master?recursive=true`) -- only the four `.ifc`
files, one `.stl`, an Excel export, and PNG renders. A companion schedule must be
ARMIE-generated; there is nothing real to reconcile against yet.

**Verdict: adopt.** Clean primary-source license, and the only one of the three candidates that
works with this project's existing deterministic reconciliation code completely unmodified.

## Sixty5 — clean license, real practicality problems

**Primary source: also clean, verified two levels deep** (not just the Hugging Face card, given
WBDG's re-host was already caught being wrong once). `sylvainHellin/ifc-bench`'s own
`model_card.md` for this project cites
`github.com/buildingsmart-community/Community-Sample-Test-Files` -- buildingSMART's own
community organization, CC BY 4.0 stated at the repo root ("contributors are informed they
publish under this license"). Independently confirmed inside that repo's own
`IFC 2.3.0.1 (IFC 2x3)/SDK - S1/README.md`, which restates CC BY 4.0 and credits a real BIM
coordinator (Ir. S. Dülger, Stamen de Koning, Eindhoven) on a real redevelopment site
(Strijp-S, a former Philips factory site) -- richer, better-corroborated provenance than WBDG.

**Two real problems, found by opening the file.**
1. **Scale.** The architecture file alone is **342 MB** (loads in ~13s) -- across all seven
   published disciplines (arc/electrical/facade/kitchen/plumbing/structural/ventilation) this is
   plausibly 1+ GB, an order of magnitude heavier than DigitalHub's ~68 MB across four
   disciplines. A real storage/repo-size/viewer-performance cost, independent of licensing.
2. **Quantities are not usable as-is.** The standard `BaseQuantities` Qto set exists but its
   `Height`/`Width`/`Area`/`Perimeter` fields are all `0.0`. The real numbers live in a
   vendor-specific `ArchiCADQuantities` pset, **in millimeters, not meters** (e.g. Height=2360,
   Width=775). Used naively, this project's code would silently report every door as `0.0 m` --
   a fabricated-looking wrong answer, worse than WBDG's honest `None`. 1899/1899 doors/windows
   are tagged, the richest of the three, but a real unit-conversion and non-standard-pset fix
   would be needed before this could be trusted.

**Verdict: not recommended for adoption now.** Clean license and the richest discipline
coverage, but real integration cost (unit conversion, non-standard quantity extraction) and a
storage cost an order of magnitude larger than DigitalHub, for a capability (reconciliation)
that only needs one discipline's doors/windows to demonstrate.

## Decision

Adopt **RWTH DigitalHub**. Effort estimate for what remains, now that a real candidate with a
clean license and clean technical fit exists:

- Add `DigitalHub_FM-ARC_v2.ifc` as a new committed project fixture (own architecture-only
  discipline for now; the other three disciplines are not needed for door/window reconciliation
  and can be added later if a non-reconciliation multi-discipline query becomes a real
  requirement) -- small, mechanical.
- Author a synthetic companion PDF schedule from this file's real `Tag` values (there is no real
  schedule to reconcile against) -- comparable scope to the original `armie_demo_schedule.pdf`
  generator; not started this pass.
- No reconciliation code changes are required -- DigitalHub's Qto sets already match this
  project's existing expectations exactly.
- Frontend 3D viewer load/render performance against a real ~9 MB, ~1000-element IFC file --
  verified live in this same pass (see the commit this report accompanies).
