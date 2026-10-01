# ldr_page.py

The LDO LDR checklist page (https://borenw.github.io/ldo-loadreg-setup/) with real ADE XL results
dropped in. Layout, sections and figure numbers stay the page's own.

```sh
# Cadence environment loaded (virtuoso on PATH) for --viva
python3 ldr_page.py ~/myLib/tb_QbtLdo1p2_LoadReg/adexl/results/data/Interactive.1.rdb --viva -o out
```

- `--viva` writes `out/figs/ldr_points.il` from the rdb and runs `virtuoso -nograph < ldr_viva_figs.il`,
  which exports two PNGs from Virtuoso Visualization (white background, ViVA's own legend):
  `fig_loadreg_typ.png` (typical VIN) and `fig_loadreg_pvt.png` (one strip per VIN).
  Without `--viva` the PNGs already in `out/figs` are reused.
- Real: Figure 20 (#8) and Figure 39 (section 4) are the exports; the #8 worst-case table and the
  section 4 donut, ranked table, statistics and ADE outputs come from the results database.
- Everything else is stamped PLACEHOLDER (the page's illustrative 1.2 V / 300 mA model).
- `--page FILE` starts from a local copy instead of the published page. If the page layout changes,
  the script stops and names the block it could not find.
- Output: `out/ldr_<cell>_<history>.html` (PNGs embedded, full-size copies in `out/figs`).
