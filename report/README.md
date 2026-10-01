# ldo_report.py

HTML report for the `LDO_LoadReg` test straight from an ADE XL run. No Virtuoso, licence or PDK.

```sh
python3 ldo_report.py ~/myLib/tb_QbtLdo1p2_LoadReg/adexl/results/data/Interactive.1.rdb -o report.html
```

- `Interactive.N.rdb` (SQLite): corner per point, vin/temperature, outputs, spec limits, ADE pass/fail
- `<rawDir>/<point>/<test>/psf/dc.dc` (binary PSF): the VOUT vs ILOAD curve per point
- `psf_read.py` is copied from borenw/gm-estimate-from-spectre (needs numpy)
- `ldo_report_template.html` holds the page layout (CSS from the LDR page); the data is embedded as JSON

Options: `--test NAME` (default first test), `--signal NET` (default: the first `VS("/NET")` output).
The output is one self-contained file that works offline.
