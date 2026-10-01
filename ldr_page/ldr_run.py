#!/usr/bin/env python3
"""ldr_run.py - run the LDO LDR checks on a real DUT netlist and export the figures from ViVA.

    python3 ldr_run.py [--checks 1,2,...|all] [--jobs 8] [--no-sim] [-o work]

For each check it writes one Spectre deck per corner around the DUT subcircuits taken from
an ADE-generated netlist (config: ade_netlist), runs Spectre in parallel, then starts
`virtuoso -nograph` once per check to evaluate the metrics with the ADE calculator and to
export the figures from Virtuoso Visualization. Results land in <work>/real.json, which
ldr_page.py --real uses to replace the page's placeholders.

Needs spectre and virtuoso on PATH. Corners and specs come from ldr_config.json beside
this file (written with defaults on first use; edit it, then rerun).
"""
import argparse
import concurrent.futures as cf
import csv
import itertools
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ldr_page import run_virtuoso, stitch  # noqa: E402

# ------------------------------------------------------------------ configuration
DEFAULT_CONFIG = {
    "ade_netlist": "/home/usr1/bw4375/simulation/myLib/tb_QbtLdo1p2_LoadReg/adexl/results/data/"
                   "Interactive.1/1/LDO_LoadReg/netlist/input.scs",
    "dut_cell": "QbtLdo1p2",
    "dut_inst": "I0 (0 LDOFBR VIN VOUT VREF VREF) {cell}",
    "vin_typ": 3.3, "vin_tol": 0.1, "temps": [-40, 25, 125],
    "vout_nom": 1.8, "iload_min": 10e-6, "iload_max": 5e-3, "iload_typ": 1e-3,
    "cout": "100p", "resr": "100m",
    # Section swaps per process corner: {nominal section: corner section}. Fill in your PDK's
    # names (e.g. 3.3 V device, resistor and MIM sections) in ldr_config.local.json.
    "process": {
        "TT": {"tt": "tt"},
        "SS": {"tt": "ss"},
        "FF": {"tt": "ff"},
        "SF": {"tt": "sf"},
        "FS": {"tt": "fs"},
    },
    "mismatch_sections": [],          # the PDK's mismatch (statistical) sections, for #10
    "mismatch_suffix": "",            # model-name suffix of devices that carry a mismatch model
    "mc_runs": 500,
    # Specs: the page's limits scaled from its 1.2 V / 300 mA example to this LDO's load
    # range. They are review placeholders; edit them to the real targets.
    "specs": {
        "dc_gain_db": 60, "pm_deg": 45, "gm_db": 10, "ugf_max_mhz": 5,
        "vdo_max_mv": 200,
        "ilim_min_x": 1.3, "ilim_max_x": 2.3,
        "load_step_pct": 3, "line_step_pct": 1,
        "psrr_mask": [[10, 60], [1e3, 60], [1e3, 40], [1e5, 40], [1e5, 20], [1e6, 20]],
        "tss_max_ms": 1, "tss_min_ms": 0, "startup_os_pct": 2, "inrush_max_ma": 20,
        "vref_tol_pct": 1.5, "vos_3sigma_mv": 4, "cpk_min": 1.33,
        "iq_max_ua": 50, "eta_min_pct": 90,
        "irev_max_ua": 10, "tsd_min_c": 150, "tsd_max_c": 170,
        "vn_max_uvrms": 50,
    },
}

CFG_PATH = os.path.join(HERE, "ldr_config.json")
AHDL = os.path.join(HERE, "ahdl_cache")      # one Verilog-A compile cache shared by every run


def load_config():
    """ldr_config.json, then ldr_config.local.json on top (keep PDK-specific names there; it is
    not committed)."""
    if not os.path.isfile(CFG_PATH):
        with open(CFG_PATH, "w") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
        print("ldr_run: wrote default %s (edit specs/corners there)" % CFG_PATH)
    cfg = json.load(open(CFG_PATH))
    local = os.path.join(HERE, "ldr_config.local.json")
    if os.path.isfile(local):
        loc = json.load(open(local))
        specs = dict(cfg.get("specs", {}), **loc.get("specs", {}))
        cfg.update(loc)
        cfg["specs"] = specs
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    for k, v in DEFAULT_CONFIG["specs"].items():
        cfg["specs"].setdefault(k, v)
    return cfg


# ------------------------------------------------------------------ DUT netlist
def extract_dut(cfg):
    """Model includes and every subckt up to `ends <dut_cell>` from the ADE netlist."""
    lines = open(cfg["ade_netlist"]).read().split("\n")
    models = []
    for l in lines:
        m = re.match(r'include\s+"([^"]+)"\s+section=(\S+)', l)
        if m:
            models.append((m.group(1), m.group(2)))
    first = next(i for i, l in enumerate(lines) if l.startswith("subckt ") or
                 (l.startswith("// Library name:") and i + 3 < len(lines) and
                  any(x.startswith("subckt ") for x in lines[i:i + 4])))
    last = next(i for i, l in enumerate(lines) if l.strip() == "ends " + cfg["dut_cell"])
    dut = "\n".join(lines[first:last + 1]) + "\n"
    # stb copy: split the EA input (VFB) from the divider (VFBR) inside the DUT and put an
    # iprobe between them; LDOFBR stays on the divider side
    body = dut.split("subckt %s " % cfg["dut_cell"], 1)[1]
    body2, n = re.subn(r"(I7 \(GND IREF1 VDBR_IN )LDOFBR LDOFBR", r"\1VFB_EA LDOFBR", body)
    if n != 1:
        sys.exit("ldr_run: could not find the feedback connection to break for stb")
    stb = ("subckt %s_stb " % cfg["dut_cell"] + body2).replace(
        "ends " + cfg["dut_cell"], "    IPRB0 (LDOFBR VFB_EA) iprobe\nends %s_stb" % cfg["dut_cell"])
    return models, dut, stb


def corners(cfg, dims="PVT", vin=None, temp=None):
    procs = list(cfg["process"])
    vt = cfg["vin_typ"]
    vins = [round(vt * (1 - cfg["vin_tol"]), 4), vt, round(vt * (1 + cfg["vin_tol"]), 4)] if "V" in dims else [vin or vt]
    temps = cfg["temps"] if "T" in dims else [25 if temp is None else temp]
    return [{"P": p, "V": v, "T": t} for p, v, t in itertools.product(procs, vins, temps)]


def cname(c, extra=""):
    return "%s_%g_%g%s" % (c["P"], c["V"], c["T"], extra)


# ------------------------------------------------------------------ deck writer
class Deck:
    def __init__(self, cfg, models, workdir):
        self.cfg, self.models, self.work = cfg, models, workdir
        os.makedirs(workdir, exist_ok=True)

    def includes(self, proc, mismatch=False):
        sw = self.cfg["process"][proc]
        out = ['include "%s" section=%s' % (f, sw.get(s, s)) for f, s in self.models]
        if mismatch:
            f = self.models[0][0]
            out += ['include "%s" section=%s' % (f, s) for s in self.cfg["mismatch_sections"]]
        return "\n".join(out)

    def write(self, check, point, c, body, params="", stb=False, mismatch=False):
        cfg = self.cfg
        d = os.path.join(self.work, check, point)
        os.makedirs(d, exist_ok=True)
        dut = cfg["dut_cell"] + ("_stb" if stb else "")
        txt = "\n".join([
            "// %s %s (generated by ldr_run.py)" % (check, point),
            "simulator lang=spectre", "global 0",
            "parameters vin=%g iload=%g vnom=%g %s" % (c["V"], c.get("I", cfg["iload_max"]), cfg["vout_nom"], params),
            self.includes(c["P"], mismatch),
            'include "%s"' % os.path.join(self.work, "dut.scs"),
            'include "%s"' % os.path.join(self.work, "dut_stb.scs"),
            cfg["dut_inst"].format(cell=dut),
            body.strip(),
            "simOpts options temp=%g tnom=27 reltol=1e-3 vabstol=1e-6 iabstol=1e-12 gmin=1e-12" % c["T"],
            ""])
        open(os.path.join(d, "input.scs"), "w").write(txt)
        return {"check": check, "point": point, "dir": d, "psf": os.path.join(d, "psf"), **c}


def run_spectre(pts, jobs):
    def one(p):
        log = os.path.join(p["dir"], "spectre.out")
        if os.path.isfile(log) and "completes with 0 errors" in open(log, errors="replace").read():
            return p, True
        subprocess.run(["spectre", "input.scs", "-format", "psfbin", "-raw", "psf", "+log", "spectre.out",
                        "-ahdllibdir", AHDL], cwd=p["dir"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
        ok = os.path.isfile(log) and "completes with 0 errors" in open(log, errors="replace").read()
        return p, ok
    bad = []
    if pts:                                    # first run alone: it fills the shared AHDL cache
        p0, ok0 = one(pts[0])
        if not ok0:
            bad.append(p0["point"])
    with cf.ThreadPoolExecutor(jobs) as ex:
        for p, ok in ex.map(one, pts[1:]):
            if not ok:
                bad.append(p["point"])
    if bad:
        print("  spectre FAILED for %d points: %s" % (len(bad), ", ".join(bad[:8])))
    return [p for p in pts if p["point"] not in bad]


# ------------------------------------------------------------------ SKILL generation
COLOR = {"TT": "y6", "SS": "y2", "FF": "y3", "SF": "y4", "FS": "y5"}
TSTYLE = {-40: "dash", 25: "solid", 125: "dot"}


def mono(xs):
    """ViVA puts a waveform whose x repeats (steps, vertical lines) into a subwindow of its own;
    nudge repeats up by 1e-9 relative so x is strictly increasing and the plot looks the same."""
    out = []
    for x in xs:
        if out and x <= out[-1]:
            x = out[-1] + max(abs(out[-1]) * 1e-9, 1e-15)
        out.append(x)
    return out


def sk(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def style_of(p, by="T"):
    if by == "T":
        return TSTYLE.get(p["T"], "solid")
    vs = sorted({p["V"]})
    return "solid"


def curve_label(p, dims):
    parts = [p["P"]]
    if "V" in dims:
        parts.append("%gV" % p["V"])
    if "T" in dims:
        parts.append("%gC" % p["T"])
    return " ".join(parts) + p.get("lab", "")


SKILL_LIB = r'''
;; helpers used by the generated ldr_*.il jobs
procedure(ldrEval(expr) car(errset(evalstring(expr) nil)))
procedure(ldrNum(x) if(numberp(x) sprintf(nil "%.9g" x) "nan"))
procedure(ldrWave(xs ys)
  let((xv yv)
    xv = drCreateVec('double xs)  yv = drCreateVec('double ys)
    drCreateWaveform(xv yv)))
;; ViVA puts a drCreateWaveform wave and a PSF wave in different subwindows; strip a PSF wave
;; to a bare x/y waveform when a panel also has masks or computed curves
procedure(ldrWaveLike(xs ys ref)
  let((xv yv rx)
    xv = drCreateVec('double xs)  yv = drCreateVec('double ys)
    ;; ViVA groups traces by the x vector's name/units; copy them from a simulator trace
    when(ref && (rx = drGetWaveformXVec(ref))
      errset(xv~>units = rx~>units) errset(xv~>name = rx~>name)
      errset(yv~>units = drGetWaveformYVec(ref)~>units))
    drCreateWaveform(xv yv)))
procedure(ldrPlot(win sub w lab col sty thick)
  when(w awvPlotWaveform(win list(w) ?subwindow sub ?expr list(lab) ?color list(col)
                         ?lineStyle list(sty) ?lineThickness list(thick))))
;; a flat red spec line spanning the x range of a reference wave
procedure(ldrSpecH(win sub ref y lab)
  when(ref ldrPlot(win sub ref*0 + y lab "y1" "solid" "thick")))
procedure(ldrSave(win name h)
  saveGraphImage(?window win ?fileName strcat(ldrOut "/" name ".png") ?width 1400 ?height h
                 ?units "pixels" ?backgroundColor "white" ?enableTitle nil ?saveEachSubwindowSeparately t))
'''


class Job:
    """Build one SKILL file: metrics per point (CSV), then figures."""

    def __init__(self, outdir):
        self.out = outdir
        self.titles = {}
        self.lines = ['ldrOut = %s' % sk(outdir), 'load(%s)' % sk(os.path.join(outdir, "ldr_lib.il"))]
        open(os.path.join(outdir, "ldr_lib.il"), "w").write(SKILL_LIB)

    def metrics(self, pts, exprs):
        """exprs: [(name, skill_expr)], evaluated after openResults(point psf)."""
        f = os.path.join(self.out, "metrics.csv")
        self.lines.append('ldrCsv = outfile(%s)' % sk(f))
        self.lines.append('fprintf(ldrCsv "point,%s\\n")' % ",".join(n for n, _ in exprs))
        for p in pts:
            self.lines.append('openResults(%s)' % sk(p["psf"]))
            vals = " ".join('ldrNum(ldrEval(%s))' % sk(e) for _, e in exprs)
            self.lines.append('fprintf(ldrCsv "%%s%s\\n" %s %s)' % (",%s" * len(exprs), sk(p["point"]), vals))
        self.lines.append('close(ldrCsv)')

    title = ""

    def figure(self, name, panels, height=700, title=None):
        """panels: [{"curves":[(psf, expr, label, color, style)], "xy":[(xs, ys, label, color, style)],
        "specs":[(y, legend)], "masks":[(xs, ys, legend)], "xlab","ylab","xlog","ylog","ylim","xlim"}]"""
        self.title = title or name
        self.titles[name] = [pn.get("title") or "%s vs %s" % (pn.get("ylab", ""), pn.get("xlab", "")) for pn in panels]
        self.lines.append('let((win ref)')
        self.lines.append('win = awvCreatePlotWindow()')
        for i, pn in enumerate(panels, 1):
            pn.setdefault("title", "%s vs %s" % (pn.get("ylab", ""), pn.get("xlab", "")))
            if i > 1:
                self.lines.append('awvAddSubwindow(win)')
            self.lines.append('ref = nil')
            for psf, expr, lab, col, sty in pn.get("curves", []):
                self.lines.append('openResults(%s)' % sk(psf))
                self.lines.append('ldrW = ldrEval(%s) unless(ref ref = ldrW)' % sk(expr))
                self.lines.append('ldrPlot(win %d ldrW %s %s %s "medium")' % (i, sk(lab), sk(col), sk(sty)))
            for xs, ys, lab, col, sty in pn.get("xy", []):
                self.lines.append("ldrW = ldrWave('(%s) '(%s)) unless(ref ref = ldrW)" %
                                  (" ".join("%.15g" % x for x in mono(xs)), " ".join("%.9g" % y for y in ys)))
                self.lines.append('ldrPlot(win %d ldrW %s %s %s "medium")' % (i, sk(lab), sk(col), sk(sty)))
            for y, leg in pn.get("specs", []):
                self.lines.append('ldrSpecH(win %d ref %.9g %s)' % (i, y, sk(leg)))
            for xs, ys, leg in pn.get("masks", []):
                self.lines.append("ldrPlot(win %d ldrWaveLike('(%s) '(%s) ref) %s \"y1\" \"solid\" \"thick\")" %
                                  (i, " ".join("%.15g" % x for x in mono(xs)), " ".join("%.9g" % y for y in ys), sk(leg)))
            if pn.get("xlog"):
                self.lines.append('awvLogXAxis(win t ?subwindow %d)' % i)
            if pn.get("ylog"):
                self.lines.append('awvLogYAxis(win 1 t ?subwindow %d)' % i)
            if pn.get("ylim"):
                self.lines.append("awvSetYLimit(win 1 '(%.9g %.9g) ?subwindow %d)" % (pn["ylim"][0], pn["ylim"][1], i))
            if pn.get("xlim"):
                self.lines.append("awvSetXLimit(win '(%.9g %.9g) ?subwindow %d)" % (pn["xlim"][0], pn["xlim"][1], i))
            self.lines.append('awvSetXAxisLabel(win %s ?subwindow %d)' % (sk(pn.get("xlab", "")), i))
            self.lines.append('awvSetYAxisLabel(win 1 %s ?subwindow %d)' % (sk(pn.get("ylab", "")), i))
        self.lines.append('printf("LDR fig %s -> %%L\\n" ldrSave(win %s %d)))' % (name, sk(name), height))

    def run(self):
        self.lines += ['printf("LDR DONE\\n")', 'hiQuit()']
        f = os.path.join(self.out, "job.il")
        open(f, "w").write("\n".join(self.lines) + "\n")
        run_virtuoso(f, self.out, os.path.join(self.out, "viva.log"), timeout=1800)
        for l in open(os.path.join(self.out, "viva.log"), errors="replace"):
            if "*Error*" in l and "errset" not in l:
                pass
        rows = list(csv.DictReader(open(os.path.join(self.out, "metrics.csv")))) \
            if os.path.isfile(os.path.join(self.out, "metrics.csv")) else []
        return {r["point"]: {k: (float(v) if v not in ("nan", "") else None) for k, v in r.items() if k != "point"}
                for r in rows}


# ------------------------------------------------------------------ result helpers
def worst(pts, M, key, how):
    vals = [(M[p["point"]][key], p) for p in pts if M.get(p["point"], {}).get(key) is not None]
    if not vals:
        return None, None
    return (max if how == "max" else min)(vals, key=lambda t: t[0])


def kp_row(label, v, p, spec_txt, ok, margin_txt, unit, dp):
    if v is None:
        return {"l": label, "v": "no data", "c": "–", "s": spec_txt, "m": "–", "r": "fail"}
    return {"l": label, "v": ("{:,.%df}" % dp).format(v) + unit, "c": corner_text(p), "s": spec_txt,
            "m": margin_txt, "r": ok}


def corner_text(p):
    if p is None:
        return "–"
    t = "%s %.2f V" % (p["P"], p["V"]) if p.get("tsweep") else "%s %.2f V %g °C" % (p["P"], p["V"], p["T"])
    return t + (" · " + p["lab"].strip() if p.get("lab") else "")


def judge(v, lim, kind, unit, dp):
    """kind 'lo' = value must be <= lim, 'hi' = value must be >= lim."""
    if v is None:
        return "–", "fail"
    mg = (lim - v) if kind == "lo" else (v - lim)
    fr = mg / abs(lim) if lim else 0
    r = "fail" if mg < 0 else "warn" if fr < 0.2 else "pass"
    return ("{:,.%df}" % dp).format(mg) + unit + " (%.0f%%)" % (fr * 100), r


def row(label, pts, M, key, how, lim, kind, unit, dp, scale=1.0, spec_txt=None):
    v, p = worst(pts, M, key, how)
    v = None if v is None else v * scale
    mg, r = judge(v, lim, kind, unit, dp)
    return kp_row(label, v, p, spec_txt or ("%s %g%s" % ("≤" if kind == "lo" else "≥", lim, unit)), r, mg, unit, dp)


def fmt_panel_specs(specs):
    return [(y, leg) for y, leg in specs]


# ------------------------------------------------------------------ the checks
# Each check_N(ctx) writes decks, simulates, evaluates metrics with the ADE calculator in
# ViVA, exports figures and returns
#   {"charts": {chart_id: (png_stem, title)}, "kp": [rows], "ade": [(name, expr)], "setup": str}
# Chart ids are the page's own (101 = #1 loop gain magnitude, ...).
TB_SRC = "V0 (VIN 0) vsource dc=vin"
TB_LOAD = "I1 (VOUT 0) isource dc=iload"
W_DC = 'v("VOUT" ?result \'dc)'
W_TR = 'v("VOUT" ?result \'tran)'


def tb_cout(cfg):
    return "C0 (VOUT nesr) capacitor c=%s\nR0 (nesr 0) resistor r=%s" % (cfg["cout"], cfg["resr"])


def vmin_max(cfg):
    return round(cfg["vin_typ"] * (1 - cfg["vin_tol"]), 4), round(cfg["vin_typ"] * (1 + cfg["vin_tol"]), 4)


def curves(pts, expr, dims):
    return [(p["psf"], expr, curve_label(p, dims), COLOR[p["P"]], TSTYLE.get(p["T"], "solid")) for p in pts]


def per_vin(pts):
    return [(v, [p for p in pts if p["V"] == v]) for v in sorted({p["V"] for p in pts})]


def check_1(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    pts = []
    for il, tag in ((cfg["iload_min"], "light"), (cfg["iload_max"], "heavy")):
        for c in corners(cfg, "PVT"):
            c = dict(c, I=il, lab=" " + tag)
            pts.append(ctx.deck.write("c1", cname(c, "_" + tag), c, "\n".join(
                [TB_SRC, TB_LOAD, tb_cout(cfg), "stb1 stb start=1 stop=1G dec=20 probe=I0.IPRB0"]), stb=True))
    pts = ctx.sim(pts)
    LG = 'getData("loopGain" ?result \'stb)'
    UGF = 'cross(dB20(%s) 0 1 "falling")' % LG
    ex = [("DC_gain", "value(dB20(%s) 1)" % LG), ("UGF", UGF),
          ("PM", "value(phaseDeg(%s) %s)" % (LG, UGF)),
          ("GM", '-value(dB20(%s) cross(phaseDeg(%s) 0 1 "falling"))' % (LG, LG))]
    job = ctx.job("c1")
    job.metrics(pts, ex)
    pm, pp = [], []
    for tag, il in (("light", cfg["iload_min"]), ("heavy", cfg["iload_max"])):
        sub = [p for p in pts if p["lab"].strip() == tag]
        xl = "freq, ILOAD %s" % eng_s(il, "A")
        pm.append({"curves": curves(sub, "dB20(%s)" % LG, "PVT"), "xlog": True, "xlab": xl, "ylab": "loop gain (dB)",
                   "specs": [(S["dc_gain_db"], "SPEC >= %g dB" % S["dc_gain_db"])], "ylim": (-80, 110)})
        pp.append({"curves": curves(sub, "phaseDeg(%s)" % LG, "PVT"), "xlog": True, "xlab": xl, "ylab": "loop phase (deg)",
                   "specs": [(S["pm_deg"], "SPEC PM >= %g deg" % S["pm_deg"])], "ylim": (-200, 200)})
    job.figure("fig_101", pm, 640)
    job.figure("fig_102", pp, 640)
    M = job.run()
    stitch(job.out, "fig_101", titles=job.titles.get("fig_101"), labels=["DC loop gain ≥ %g dB" % S["dc_gain_db"]])
    stitch(job.out, "fig_102", titles=job.titles.get("fig_102"), labels=["PM ≥ %g°  (phase at the UGF)" % S["pm_deg"]])
    gmp = [p for p in pts if M.get(p["point"], {}).get("GM") is not None]
    kp = [row("Min DC loop gain", pts, M, "DC_gain", "min", S["dc_gain_db"], "hi", " dB", 1),
          row("Min phase margin", pts, M, "PM", "min", S["pm_deg"], "hi", "°", 1),
          row("Min gain margin", gmp, M, "GM", "min", S["gm_db"], "hi", " dB", 1) if gmp else
          {"l": "Min gain margin", "v": "phase never reaches 0°", "c": "all corners", "s": "≥ %g dB" % S["gm_db"],
           "m": "–", "r": "pass"},
          row("Max UGF", pts, M, "UGF", "max", S["ugf_max_mhz"], "lo", " MHz", 3, scale=1e-6)]
    return ctx.done(job, {101: ("fig_101", "Loop gain magnitude, %d PVT points × ILOAD {%s, %s}" % (
        len(pts) // 2, eng_s(cfg["iload_min"], "A"), eng_s(cfg["iload_max"], "A"))),
        102: ("fig_102", "Loop gain phase (PM = phase at the UGF)")}, kp, ex,
        "stb 1 Hz to 1 GHz, probe = iprobe I0.IPRB0 inserted between the divider (LDOFBR) and the EA input (VFB) "
        "in a copy of the DUT. Spectre's loopGain phase starts at +180° for this loop, so PM is the phase at the UGF "
        "and GM is −|T| where the phase reaches 0°. Output %s + %s ESR. PVT × ILOAD {min, max}." % (cfg["cout"], cfg["resr"]))


def check_2(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    _, vmax = vmin_max(cfg)
    loads = [cfg["iload_max"] * k for k in (0.1, 0.25, 0.5, 0.75, 1.0)]
    pts = []
    for c in corners(cfg, "PT"):
        for il in loads:
            cc = dict(c, I=il)
            # resistive load: an ideal current sink at low VIN drives VOUT far negative and the
            # DC continuation stays on that branch for the whole sweep
            pts.append(ctx.deck.write("c2", cname(cc, "_%gmA" % (il * 1e3)), cc, "\n".join(
                [TB_SRC, "RL (VOUT 0) resistor r=%g" % (cfg["vout_nom"] / il), tb_cout(cfg),
                 "sw dc param=vin start=0.5 stop=%g step=2m" % vmax])))
    pts = ctx.sim(pts)
    vdo = 'let((w r) w = %s r = value(w %g) cross(w 0.98*r 1 "rising") - 0.98*r)' % (W_DC, vmax)
    ex = [("VDO", vdo), ("VREG", "value(%s %g)" % (W_DC, vmax))]
    job = ctx.job("c2")
    job.metrics(pts, ex)
    M = job.run()
    xy = []
    for c in corners(cfg, "PT"):
        sub = sorted([p for p in pts if (p["P"], p["V"], p["T"]) == (c["P"], c["V"], c["T"])], key=lambda p: p["I"])
        sub = [p for p in sub if M.get(p["point"], {}).get("VDO") is not None]
        if sub:
            xy.append(([p["I"] * 1e3 for p in sub], [M[p["point"]]["VDO"] for p in sub],
                       curve_label(sub[0], "PT"), COLOR[c["P"]], TSTYLE.get(c["T"], "solid")))
    fj = ctx.job("c2_fig")
    fj.figure("fig_103", [{"xy": xy, "xlab": "ILOAD (mA)", "ylab": "VDO (V)",
                           "specs": [(S["vdo_max_mv"] / 1e3, "SPEC <= %g mV" % S["vdo_max_mv"])]}], 720)
    fj.run()
    stitch(fj.out, "fig_103", titles=fj.titles.get("fig_103"), labels=["VDO ≤ %g mV" % S["vdo_max_mv"]])
    heavy = [p for p in pts if abs(p["I"] - cfg["iload_max"]) < 1e-12]
    kp = [row("Max VDO at %s" % eng_s(cfg["iload_max"], "A"), heavy, M, "VDO", "max", S["vdo_max_mv"], "lo", " mV", 1, scale=1e3)]
    return ctx.done(fj, {103: ("fig_103", "Dropout voltage vs load current, %d process × temperature corners" % len(xy))},
                    kp, ex, "dc sweep vin 0.5 V to %g V with a resistive load RL = VOUT_NOM / ILOAD, ILOAD = %s; VDO = VIN − VOUT where VOUT has fallen to 98%% of "
                    "its value at %g V. VIN is swept, so the corners are P × T." % (
                        vmax, ", ".join(eng_s(x, "A") for x in loads), vmax))


def check_3(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    pts = [ctx.deck.write("c3", cname(c), c, "\n".join(
        [TB_SRC, "VF (VOUT 0) vsource dc=vf", tb_cout(cfg),
         "sw dc param=vf start=0 stop=%g step=5m" % (cfg["vout_nom"] * 1.1)]), params="vf=0")
        for c in corners(cfg, "PVT")]
    pts = ctx.sim(pts)
    I = 'i("VF:p" ?result \'dc)'
    lo, hi = S["ilim_min_x"] * cfg["iload_max"], S["ilim_max_x"] * cfg["iload_max"]
    ex = [("ILIM_peak", "ymax(%s)" % I), ("ISC", "value(%s 0)" % I),
          ("I_90", "value(%s %g)" % (I, 0.9 * cfg["vout_nom"]))]
    job = ctx.job("c3")
    job.metrics(pts, ex)
    panels = [{"curves": curves(sub, "%s*1e3" % I, "PT"), "xlab": "forced VOUT (V), VIN %g V" % v,
               "ylab": "IOUT (mA)", "specs": [(hi * 1e3, "SPEC <= %g mA" % (hi * 1e3)), (lo * 1e3, "SPEC >= %g mA" % (lo * 1e3))]}
              for v, sub in per_vin(pts)]
    job.figure("fig_104", panels, 560)
    M = job.run()
    stitch(job.out, "fig_104", titles=job.titles.get("fig_104"), labels=["ILIM ≤ %g mA" % (hi * 1e3), "ILIM ≥ %g mA" % (lo * 1e3)])
    kp = [row("Min ILIM (peak IOUT)", pts, M, "ILIM_peak", "min", lo * 1e3, "hi", " mA", 2, scale=1e3),
          row("Max ILIM (peak IOUT)", pts, M, "ILIM_peak", "max", hi * 1e3, "lo", " mA", 2, scale=1e3),
          info_row("Max short-circuit current", pts, M, "ISC", "max", " mA", 2, 1e3)]
    return ctx.done(job, {104: ("fig_104", "IOUT vs forced VOUT, %d PVT points (one panel per VIN)" % len(pts))}, kp, ex,
                    "dc sweep of a vsource VF forcing $DUT_OUTPUT from 0 to %g V; IOUT = current the LDO drives into VF. "
                    "ILIM window = %g× to %g× ILOAD max (%s)." % (cfg["vout_nom"] * 1.1, S["ilim_min_x"], S["ilim_max_x"],
                                                                  eng_s(cfg["iload_max"], "A")))


def check_4(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    a, b = cfg["iload_min"], cfg["iload_max"]
    pwl = "[0 %g 20u %g 21u %g 60u %g 61u %g]" % (a, a, b, b, a)
    pts = [ctx.deck.write("c4", cname(c), c, "\n".join(
        [TB_SRC, "VAM (VOUT VL) vsource dc=0", "I1 (VL 0) isource type=pwl wave=%s" % pwl, tb_cout(cfg),
         "tr1 tran stop=100u maxstep=10n"])) for c in corners(cfg, "PVT")]
    pts = ctx.sim(pts)
    lim = S["load_step_pct"] / 100 * cfg["vout_nom"]
    ex = [("undershoot", "value(%s 19u) - ymin(clip(%s 20u 60u))" % (W_TR, W_TR)),
          ("overshoot", "ymax(clip(%s 60u 100u)) - value(%s 59u)" % (W_TR, W_TR)),
          ("t_settle", 'cross(abs(clip(%s 61u 100u) - value(%s 99u)) %g 1 "falling") - 61u' % (W_TR, W_TR, 0.01 * cfg["vout_nom"]))]
    job = ctx.job("c4")
    job.metrics(pts, ex)
    dv = "(%s - value(%s 19u))" % (W_TR, W_TR)
    panels = [{"curves": curves(sub, dv, "PT"), "xlab": "time, VIN %g V" % v, "ylab": "VOUT - VOUT(19us) (V)",
               "specs": [(lim, "SPEC <= +%g mV" % (lim * 1e3)), (-lim, "SPEC >= -%g mV" % (lim * 1e3))]}
              for v, sub in per_vin(pts)]
    job.figure("fig_105", panels, 560)
    tt = next(p for p in pts if p["P"] == "TT" and p["V"] == cfg["vin_typ"] and p["T"] == 25)
    job.figure("fig_140", [{"curves": [(tt["psf"], 'i("VAM:p" ?result \'tran)*1e3', "ILOAD", "y6", "solid")],
                            "xlab": "time", "ylab": "ILOAD (mA)"}], 360)
    M = job.run()
    labs = ["ΔVOUT ≤ +%g mV  (%g%%)" % (lim * 1e3, S["load_step_pct"]), "ΔVOUT ≥ −%g mV" % (lim * 1e3)]
    stitch(job.out, "fig_105", titles=job.titles.get("fig_105"), labels=labs)
    stitch(job.out, "fig_140", titles=job.titles.get("fig_140"), labels=[])
    kp = [row("Max undershoot (load rise)", pts, M, "undershoot", "max", lim * 1e3, "lo", " mV", 1, scale=1e3),
          row("Max overshoot (load release)", pts, M, "overshoot", "max", lim * 1e3, "lo", " mV", 1, scale=1e3),
          info_row("Max settling to 1% after release", pts, M, "t_settle", "max", " µs", 2, 1e6)]
    return ctx.done(job, {140: ("fig_140", "Stimulus, ILOAD %s → %s → %s, 1 µs edges (TT %g V 25 °C)" % (
        eng_s(a, "A"), eng_s(b, "A"), eng_s(a, "A"), cfg["vin_typ"])),
        105: ("fig_105", "VOUT deviation from its pre-step value, %d PVT points (one panel per VIN)" % len(pts))}, kp, ex,
        "tran 0 to 100 µs, ILOAD pwl %s → %s at 20 µs and back at 60 µs, 1 µs edges; ΔVOUT is referred to each corner's "
        "own VOUT at 19 µs, because the DC error differs per corner." % (eng_s(a, "A"), eng_s(b, "A")))


def check_5(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    pts = [ctx.deck.write("c5", cname(c), c, "\n".join(
        ["V0 (VIN 0) vsource dc=vin mag=1", TB_LOAD, tb_cout(cfg), "ac1 ac start=10 stop=10M dec=20"]))
        for c in corners(cfg, "PVT")]
    pts = ctx.sim(pts)
    P = '(-dB20(v("VOUT" ?result \'ac)))'
    ex = [("PSRR_1k", "value(%s 1k)" % P), ("PSRR_100k", "value(%s 100k)" % P), ("PSRR_1M", "value(%s 1M)" % P)]
    job = ctx.job("c5")
    job.metrics(pts, ex)
    mx = [m[0] for m in S["psrr_mask"]]
    my = [m[1] for m in S["psrr_mask"]]
    panels = [{"curves": curves(sub, P, "PT"), "masks": [(mx, my, "SPEC mask")], "xlog": True,
               "xlab": "freq, VIN %g V, ILOAD %s" % (v, eng_s(cfg["iload_max"], "A")), "ylab": "PSRR (dB)"}
              for v, sub in per_vin(pts)]
    job.figure("fig_106", panels, 560)
    M = job.run()
    mask_lab = "PSRR mask: ≥ %g dB to 1 kHz, ≥ %g dB to 100 kHz, ≥ %g dB to 1 MHz" % (my[0], my[2], my[4])
    stitch(job.out, "fig_106", titles=job.titles.get("fig_106"), labels=[mask_lab], mode="note")
    kp = [row("Min PSRR at 1 kHz", pts, M, "PSRR_1k", "min", my[1], "hi", " dB", 1),
          row("Min PSRR at 100 kHz", pts, M, "PSRR_100k", "min", my[3], "hi", " dB", 1),
          row("Min PSRR at 1 MHz", pts, M, "PSRR_1M", "min", my[5], "hi", " dB", 1)]
    return ctx.done(job, {106: ("fig_106", "PSRR vs frequency, ILOAD %s, %d PVT points (one panel per VIN)" % (
        eng_s(cfg["iload_max"], "A"), len(pts)))}, kp, ex,
        "ac 10 Hz to 10 MHz, acmag 1 on the $DUT_POWER source, ILOAD = %s; PSRR = −dB20(VF(\"/$DUT_OUTPUT\"))." % eng_s(cfg["iload_max"], "A"))


def check_6(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    lo, hi = vmin_max(cfg)
    edge = (hi - lo) / 1e6                     # 1 V/us
    pwl = "[0 %g 20u %g %.6gu %g 60u %g %.6gu %g]" % (lo, lo, 20 + edge * 1e6, hi, hi, 60 + edge * 1e6, lo)
    pts = [ctx.deck.write("c6", cname(c), c, "\n".join(
        ["V0 (VIN 0) vsource type=pwl wave=%s" % pwl, TB_LOAD, tb_cout(cfg), "tr1 tran stop=100u maxstep=10n"]))
        for c in corners(cfg, "PT")]
    pts = ctx.sim(pts)
    lim = S["line_step_pct"] / 100 * cfg["vout_nom"]
    ex = [("dV_up", "ymax(clip(%s 20u 60u)) - value(%s 19u)" % (W_TR, W_TR)),
          ("dV_dn", "value(%s 59u) - ymin(clip(%s 60u 100u))" % (W_TR, W_TR))]
    job = ctx.job("c6")
    job.metrics(pts, ex)
    dv = "(%s - value(%s 19u))" % (W_TR, W_TR)
    job.figure("fig_107", [{"curves": curves(pts, dv, "PT"), "xlab": "time", "ylab": "VOUT - VOUT(19us) (V)",
                            "specs": [(lim, "SPEC <= +%g mV" % (lim * 1e3)), (-lim, "SPEC >= -%g mV" % (lim * 1e3))]}], 700)
    tt = next(p for p in pts if p["P"] == "TT" and p["T"] == 25)
    job.figure("fig_160", [{"curves": [(tt["psf"], 'v("VIN" ?result \'tran)', "VIN", "y6", "solid")],
                            "xlab": "time", "ylab": "VIN (V)"}], 360)
    M = job.run()
    stitch(job.out, "fig_107", titles=job.titles.get("fig_107"), labels=["ΔVOUT ≤ +%g mV  (%g%%)" % (lim * 1e3, S["line_step_pct"]), "ΔVOUT ≥ −%g mV" % (lim * 1e3)])
    stitch(job.out, "fig_160", titles=job.titles.get("fig_160"), labels=[])
    kp = [row("Max ΔVOUT on VIN rise", pts, M, "dV_up", "max", lim * 1e3, "lo", " mV", 2, scale=1e3),
          row("Max ΔVOUT on VIN fall", pts, M, "dV_dn", "max", lim * 1e3, "lo", " mV", 2, scale=1e3)]
    return ctx.done(job, {160: ("fig_160", "Stimulus, VIN %g → %g → %g V at 1 V/µs" % (lo, hi, lo)),
                          107: ("fig_107", "VOUT deviation, VIN step at 1 V/µs, ILOAD %s, %d process × temperature corners" % (
                              eng_s(cfg["iload_max"], "A"), len(pts)))}, kp, ex,
                    "tran 0 to 100 µs; $DUT_POWER source pwl %g → %g V at 20 µs and back at 60 µs, 1 V/µs; ILOAD = %s. "
                    "VIN is the stimulus, so the corners are P × T." % (lo, hi, eng_s(cfg["iload_max"], "A")))


def check_7(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    rl = cfg["vout_nom"] / cfg["iload_max"]
    pts = [ctx.deck.write("c7", cname(c), c, "\n".join(
        ["V0 (VIN 0) vsource type=pwl wave=[0 0 10u 0 20u vin]", "RL (VOUT 0) resistor r=%g" % rl, tb_cout(cfg),
         "tr1 tran stop=1m maxstep=50n"])) for c in corners(cfg, "PVT")]
    pts = ctx.sim(pts)
    fin = "value(%s 1m)" % W_TR
    IIN = '(-i("V0:p" ?result \'tran))'
    ex = [("t_ss", 'cross(%s 0.9*%s 1 "rising") - 20u' % (W_TR, fin)),
          ("overshoot_pct", "(ymax(%s) - %s)/%s*100" % (W_TR, fin, fin)),
          ("I_inrush", "ymax(%s)" % IIN), ("VOUT_final", fin)]
    job = ctx.job("c7")
    job.metrics(pts, ex)
    os_ = 1 + S["startup_os_pct"] / 100
    p1 = [{"curves": curves(sub, "%s/%s" % (W_TR, fin), "PT"), "xlab": "time, VIN ramp 0 to %g V over 10us" % v,
           "ylab": "VOUT / VOUT(1ms)", "specs": [(os_, "SPEC <= %g" % os_)], "xlim": (0, 200e-6)} for v, sub in per_vin(pts)]
    p2 = [{"curves": curves(sub, "%s*1e3" % IIN, "PT"), "xlab": "time, VIN %g V" % v, "ylab": "IIN (mA)",
           "specs": [(S["inrush_max_ma"], "SPEC <= %g mA" % S["inrush_max_ma"])], "xlim": (0, 200e-6)} for v, sub in per_vin(pts)]
    job.figure("fig_108", p1, 520)
    job.figure("fig_109", p2, 520)
    M = job.run()
    stitch(job.out, "fig_108", titles=job.titles.get("fig_108"), labels=["overshoot ≤ %g%%" % S["startup_os_pct"]])
    stitch(job.out, "fig_109", titles=job.titles.get("fig_109"), labels=["IIN ≤ %g mA" % S["inrush_max_ma"]])
    # power-up temperature scan: the reference can latch at VIN for some temperatures, which the
    # three corner temperatures miss
    scan = []
    for c in corners(cfg, "PV"):
        temps = range(-40, 126, 1) if (c["P"], c["V"]) == ("TT", cfg["vin_typ"]) else range(-40, 126, 5)
        for t in temps:
            cc = dict(c, T=t)
            scan.append(ctx.deck.write("c7scan", cname(cc), cc, "\n".join(
                ["V0 (VIN 0) vsource type=pwl wave=[0 0 10u 0 20u vin]", "RL (VOUT 0) resistor r=%g" % rl, tb_cout(cfg),
                 "tr1 tran stop=1m maxstep=200n"])))
    scan = ctx.sim(scan)
    sj = ctx.job("c7scan")
    sj.metrics(scan, [("VOUT_1ms", "value(%s 1m)" % W_TR), ("VREF_1ms", 'value(v("VREF" ?result \'tran) 1m)')])
    MS = sj.run()
    fails = [p for p in scan if MS.get(p["point"], {}).get("VOUT_1ms") is not None and MS[p["point"]]["VOUT_1ms"] < 0.5 * cfg["vout_nom"]]
    xy = []
    for c in corners(cfg, "PV"):
        sub = sorted([p for p in scan if (p["P"], p["V"]) == (c["P"], c["V"]) and MS.get(p["point"], {}).get("VOUT_1ms") is not None],
                     key=lambda p: p["T"])
        if sub:
            vs = sorted({q["V"] for q in scan})
            xy.append(([p["T"] for p in sub], [MS[p["point"]]["VOUT_1ms"] for p in sub], "%s %gV" % (c["P"], c["V"]),
                       COLOR[c["P"]], {0: "dash", 1: "solid", 2: "dot"}[vs.index(c["V"])]))
    fj = ctx.job("c7scan_fig")
    fj.figure("fig_108b", [{"xy": xy, "xlab": "temperature (C)", "ylab": "VOUT 1 ms after power-up (V)",
                            "specs": [(0.9 * cfg["vout_nom"], "SPEC >= 90% VOUT_NOM")]}], 640)
    fj.run()
    stitch(fj.out, "fig_108b", titles=fj.titles.get("fig_108b"), labels=["started: VOUT ≥ 90%% of %g V" % cfg["vout_nom"]])
    shutil.copy(os.path.join(fj.out, "fig_108b.png"), os.path.join(ctx.figs, "fig_108b.png"))
    ft = sorted({(p["P"], p["V"]) for p in fails})
    fail_txt = "; ".join("%s %g V: %s °C" % (pp, vv, ",".join("%g" % p["T"] for p in sorted(fails, key=lambda q: q["T"])
                                                          if (p["P"], p["V"]) == (pp, vv))) for pp, vv in ft)
    kp = [{"l": "Power-up temperature scan: failed starts", "v": "%d of %d" % (len(fails), len(scan)),
           "c": fail_txt or "none", "s": "0", "m": "–", "r": "fail" if fails else "pass"},
          row("Max start-up time (VIN at final → VOUT 90%)", pts, M, "t_ss", "max", S["tss_max_ms"] * 1e3, "lo", " µs", 2, scale=1e6),
          row("Max overshoot", pts, M, "overshoot_pct", "max", S["startup_os_pct"], "lo", "%", 2),
          row("Max inrush current", pts, M, "I_inrush", "max", S["inrush_max_ma"], "lo", " mA", 2, scale=1e3)]
    out = ctx.done(job, {108: ("fig_108", "VOUT ramp at power-up, normalised to its final value, %d PVT points" % len(pts)),
                         109: ("fig_109", "Input current at power-up")}, kp, ex,
                   "This LDO has no EN pin, so start-up is a VIN ramp 0 → VIN over 10 µs (from 10 µs) into RL = %g Ω "
                   "(ILOAD max) + %s; tran 0 to 1 ms. t_ss counts from VIN reaching its final value. A second scan "
                   "powers up every process × VIN corner every 5 °C (TT %g V every 1 °C) and records VOUT at 1 ms; a start "
                   "fails when VOUT stays below half of VOUT_NOM (the reference latches at VIN)." % (rl, cfg["cout"], cfg["vin_typ"]))
    out["extra"] = [{"file": "fig_108b.png", "title": "Power-up temperature scan: VOUT 1 ms after VIN ramps, %d runs, "
                     "%d failed starts" % (len(scan), len(fails))}]
    return out


def check_8(ctx):
    """Load regulation comes from the ADE XL run (ldr_page.py); this adds line regulation."""
    cfg = ctx.cfg
    lo, hi = vmin_max(cfg)
    pts = [ctx.deck.write("c8", cname(c), dict(c, I=cfg["iload_typ"]), "\n".join(
        [TB_SRC, TB_LOAD, tb_cout(cfg), "sw dc param=vin start=%g stop=%g step=10m" % (lo, hi)]))
        for c in corners(cfg, "PT")]
    pts = ctx.sim(pts)
    ex = [("line_reg_mV_per_V", "(value(%s %g) - value(%s %g))/%g*1e3" % (W_DC, hi, W_DC, lo, hi - lo))]
    job = ctx.job("c8")
    job.metrics(pts, ex)
    M = job.run()
    kp = [info_row("Max line regulation (VIN %g → %g V, ILOAD %s)" % (lo, hi, eng_s(cfg["iload_typ"], "A")),
                   pts, M, "line_reg_mV_per_V", "absmax", " mV/V", 2, 1)]
    return ctx.done(job, {}, kp, ex, "line_reg: dc sweep vin %g to %g V at ILOAD %s, P × T corners." % (lo, hi, eng_s(cfg["iload_typ"], "A")))


def check_9(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    pts = [ctx.deck.write("c9", cname(c), dict(c, T=25, I=cfg["iload_typ"], tsweep=True), "\n".join(
        [TB_SRC, "RL (VOUT 0) resistor r=%g" % (cfg["vout_nom"] / cfg["iload_typ"]), tb_cout(cfg),
         "sw dc param=temp start=-40 stop=125 step=5"]))
        for c in corners(cfg, "PV")]
    for p in pts:
        p["T"] = 25
    pts = ctx.sim(pts)
    vref = 0.9
    V = 'v("VREF" ?result \'dc)'
    ex = [("VREF_m40", "value(%s -40)" % V), ("VREF_25", "value(%s 25)" % V), ("VREF_125", "value(%s 125)" % V),
          ("VREF_err_pct", "ymax(abs(%s - %g))/%g*100" % (V, vref, vref))]
    job = ctx.job("c9")
    job.metrics(pts, ex)
    tol = S["vref_tol_pct"] / 100
    cv = [(p["psf"], V, "%s %gV" % (p["P"], p["V"]), COLOR[p["P"]], {0: "dash", 1: "solid", 2: "dot"}[sorted({q["V"] for q in pts}).index(p["V"])]) for p in pts]
    job.figure("fig_111", [{"curves": cv, "xlab": "temperature (C)", "ylab": "VREF0V9_LDO_OUT (V)",
                            "specs": [(vref * (1 + tol), "SPEC <= %.4f V" % (vref * (1 + tol))),
                                      (vref * (1 - tol), "SPEC >= %.4f V" % (vref * (1 - tol)))]}], 720)
    M = job.run()
    stitch(job.out, "fig_111", titles=job.titles.get("fig_111"), labels=["VREF ≤ %.4f V (+%g%%)" % (vref * (1 + tol), S["vref_tol_pct"]),
                                "VREF ≥ %.4f V (−%g%%)" % (vref * (1 - tol), S["vref_tol_pct"])])
    for p in pts:
        m = M.get(p["point"], {})
        if m.get("VREF_m40") is not None and m.get("VREF_125") is not None:
            m["tc_ppm"] = (m["VREF_125"] - m["VREF_m40"]) / 165 / vref * 1e6
    kp = [row("Max VREF error over −40 to 125 °C", pts, M, "VREF_err_pct", "max", S["vref_tol_pct"], "lo", "%", 2),
          info_row("Worst VREF tempco (−40 to 125 °C)", pts, M, "tc_ppm", "absmax", " ppm/°C", 0, 1)]
    return ctx.done(job, {111: ("fig_111", "VREF vs temperature, %d process × VIN corners (no trim bus on this design)" % len(pts))},
                    kp, ex, "This design has no VREF trim input, so #9 shows the reference itself: dc sweep temp −40 to 125 °C, "
                    "V(VREF0V9_LDO_OUT) at ILOAD %s, process × VIN corners. Target %g V ±%g%%." % (
                        eng_s(cfg["iload_typ"], "A"), vref, S["vref_tol_pct"]))


def check_10(ctx):
    """Monte Carlo mismatch at one corner; fills #10 (offset) and #9's second chart (VREF spread)."""
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    c = ctx.mc_corner
    n = cfg["mc_runs"]
    cc = dict(c, I=cfg["iload_typ"])
    p = ctx.deck.write("c10", cname(cc, "_mc"), cc, "\n".join(
        [TB_SRC, TB_LOAD, tb_cout(cfg),
         "mc1 montecarlo variations=mismatch numruns=%d seed=12345 savefamilyplots=yes {\n  op1 dc\n}" % n]), mismatch=True)
    ctx.sim([p], fmt="psfascii")
    vals = []
    for k in range(1, n + 1):
        f = os.path.join(p["psf"], "mc1-%03d_op1.dc" % k)
        if not os.path.isfile(f):
            continue
        t = open(f).read()
        g = lambda nm: float(re.search(r'^"%s"\s+"V"\s+([-0-9.eE+]+)' % nm, t, re.M).group(1))
        vals.append((g("LDOFBR") - g("VREF"), g("VREF"), g("VOUT")))
    if not vals:
        sys.exit("ldr_run: Monte Carlo produced no iterations; see %s/spectre.out" % p["dir"])
    vos = [v[0] * 1e3 for v in vals]
    vref = [v[1] for v in vals]
    mu, sd = statistics.mean(vos), statistics.pstdev(vos)
    L = S["vos_3sigma_mv"]
    cpk = min(L - mu, mu + L) / (3 * sd) if sd > 0 else float("inf")
    job = ctx.job("c10")
    job.figure("fig_113", [hist_panel(vos, 40, "Vos = V(LDOFBR) - V(VREF) (mV)", "count", [(-L, ">="), (L, "<=")])], 620)
    mu_r, sd_r = statistics.mean(vref), statistics.pstdev(vref)
    job.figure("fig_112", [hist_panel([x * 1e3 for x in vref], 40, "VREF (mV)", "count",
                                      [(900 * (1 - S["vref_tol_pct"] / 100), ">="), (900 * (1 + S["vref_tol_pct"] / 100), "<=")])], 620)
    job.run()
    stitch(job.out, "fig_113", titles=job.titles.get("fig_113"), labels=[], mode="vlines", vlabels=["Vos ≥ −%g mV" % L, "Vos ≤ +%g mV" % L])
    stitch(job.out, "fig_112", titles=job.titles.get("fig_112"), labels=[], mode="vlines", vlabels=["VREF ≥ −%g%%" % S["vref_tol_pct"], "VREF ≤ +%g%%" % S["vref_tol_pct"]])
    ct = corner_text(c)
    r1 = abs(mu) + 3 * sd
    mg, res = judge(r1, L, "lo", " mV", 2)
    mg2, res2 = judge(cpk, S["cpk_min"], "hi", "", 2)
    ea = ea_models(ctx)
    suf = cfg.get("mismatch_suffix", "")
    no_mis = bool(ea and suf and all(not m.endswith(suf) for m in ea.values()))
    kp = [{"l": "|μ| + 3σ", "v": "%.2f mV" % r1, "c": "MC %d pts, %s" % (len(vals), ct), "s": "≤ %g mV" % L, "m": mg,
           "r": "na" if no_mis else res},
          {"l": "Cpk", "v": "%.2f" % cpk, "c": "MC %d pts" % len(vals), "s": "≥ %g" % S["cpk_min"], "m": mg2,
           "r": "na" if no_mis else res2}]
    if no_mis:
        kp.append({"l": "EA input pair mismatch model", "v": "none (%s)" % ", ".join("%s = %s" % kv for kv in sorted(ea.items())),
                   "c": "netlist", "s": "devices with a mismatch model", "m": "–", "r": "fail"})
    kp9 = {"l": "VREF spread, μ ± 3σ (mismatch)", "v": "%.1f ± %.1f mV" % (mu_r * 1e3, 3 * sd_r * 1e3),
           "c": "MC %d pts, %s" % (len(vals), ct), "s": "info", "m": "–", "r": "info"}
    out = ctx.done(job, {113: ("fig_113", "EA input offset, μ = %.2f mV, σ = %.3f mV (%d Monte Carlo points, %s)" % (mu, sd, len(vals), ct)),
                         112: ("fig_112", "VREF spread from mismatch, μ = %.1f mV, σ = %.2f mV (%d points)" % (mu_r * 1e3, sd_r * 1e3, len(vals)))},
                   kp, [("Vos", 'VDC("/LDOFBR") - VDC("/VREF")'), ("VREF", 'VDC("/VREF")')],
                   "Monte Carlo, mismatch only (%s), %d points, seed 12345, at %s, ILOAD %s.%s" % (
                       "%d PDK mismatch sections" % len(cfg["mismatch_sections"]), len(vals), ct, eng_s(cfg["iload_typ"], "A"),
                       " The EA input pair uses device models that carry no mismatch model, so this run cannot see "
                       "the EA offset: the μ is systematic offset and the σ is understated. Switch M1/M2 to the "
                       "mismatch-enabled version of the device to get a real offset distribution." if no_mis else ""))
    out["kp9_extra"] = kp9
    return out


def ea_models(ctx):
    """Model names of the EA input pair (the transistors gated by VFB and VREF in the LDO core)."""
    txt = open(os.path.join(ctx.work, "dut.scs")).read()
    core = re.search(r"^subckt (\S*ldo\S*) (.*?)^ends \1", txt, re.M | re.S | re.I)
    txt = core.group(0) if core else txt          # only the LDO core, not the reference block
    out = {}
    for m in re.finditer(r"^\s*(M\w+)\s*\(\s*\S+\s+(VFB|VREF)\s+\S+\s+\S+\s*\)\s+(\w+)", txt, re.M):
        out[m.group(1)] = m.group(3)
    return out


def hist_panel(vals, nb, xlab, ylab, vspecs):
    lo, hi = min(vals), max(vals)
    for x, _ in vspecs:
        lo, hi = min(lo, x), max(hi, x)
    pad = (hi - lo) * 0.05 or 1
    lo, hi = lo - pad, hi + pad
    w = (hi - lo) / nb
    cnt = [0] * nb
    for v in vals:
        cnt[min(nb - 1, int((v - lo) / w))] += 1
    xs, ys = [lo], [0]
    for k, c in enumerate(cnt):                  # stair outline of the histogram
        xs += [lo + k * w, lo + (k + 1) * w]
        ys += [c, c]
    xs.append(hi)
    ys.append(0)
    top = max(cnt) * 1.1
    masks = [([x, x], [0, top], "SPEC %s %g" % (s, x)) for x, s in vspecs]
    return {"xy": [(xs, ys, "MC histogram", "y6", "solid")], "masks": masks, "xlab": xlab, "ylab": ylab}


def check_11(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    pts = [ctx.deck.write("c11", cname(c), c, "\n".join(
        [TB_SRC, TB_LOAD, tb_cout(cfg), "sw dc param=iload start=1u stop=%g dec=10" % cfg["iload_max"]]))
        for c in corners(cfg, "PVT")]
    pts = ctx.sim(pts)
    IIN = '(-i("V0:p" ?result \'dc))'
    IQ = "(%s - xval(%s))" % (IIN, IIN)
    ETA = "(xval(%s)/%s*100)" % (IIN, IIN)
    ex = [("IQ_noload", "value(%s 1u)" % IQ), ("IQ_1m", "value(%s 1m)" % IQ), ("eta_1m", "value(%s 1m)" % ETA)]
    job = ctx.job("c11")
    job.metrics(pts, ex)
    p1 = [{"curves": curves(sub, "%s*1e6" % IQ, "PT"), "xlog": True, "xlab": "ILOAD (A), VIN %g V" % v, "ylab": "IQ (uA)",
           "specs": [(S["iq_max_ua"], "SPEC <= %g uA" % S["iq_max_ua"])]} for v, sub in per_vin(pts)]
    p2 = [{"curves": curves(sub, ETA, "PT"), "xlog": True, "xlab": "ILOAD (A), VIN %g V" % v, "ylab": "IOUT/IIN (%)",
           "specs": [(S["eta_min_pct"], "SPEC >= %g %%" % S["eta_min_pct"])]} for v, sub in per_vin(pts)]
    job.figure("fig_114", p1, 520)
    job.figure("fig_115", p2, 520)
    M = job.run()
    stitch(job.out, "fig_114", titles=job.titles.get("fig_114"), labels=["IQ ≤ %g µA" % S["iq_max_ua"]])
    stitch(job.out, "fig_115", titles=job.titles.get("fig_115"), labels=["η ≥ %g%% at 1 mA" % S["eta_min_pct"]])
    kp = [row("Max IQ at no load (1 µA)", pts, M, "IQ_noload", "max", S["iq_max_ua"], "lo", " µA", 2, scale=1e6),
          row("Min current efficiency at 1 mA", pts, M, "eta_1m", "min", S["eta_min_pct"], "hi", "%", 2)]
    return ctx.done(job, {114: ("fig_114", "Quiescent current vs load, %d PVT points (one panel per VIN)" % len(pts)),
                          115: ("fig_115", "Current efficiency vs load")}, kp, ex,
                    "dc sweep iload, log, 1 µA to %s; IQ = IIN − ILOAD, η = ILOAD / IIN." % eng_s(cfg["iload_max"], "A"))


def check_12(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    pts = [ctx.deck.write("c12", cname(c), dict(c, V=0.0), "\n".join(
        ["V0 (VIN 0) vsource dc=0", "VR (VOUT 0) vsource dc=vr", tb_cout(cfg),
         "sw dc param=vr start=0 stop=%g step=10m" % cfg["vout_nom"]]), params="vr=0") for c in corners(cfg, "PT")]
    for p, c in zip(pts, corners(cfg, "PT")):
        p["V"] = c["V"]
    pts = ctx.sim(pts)
    IR = '(-i("VR:p" ?result \'dc))'
    ex = [("IREV", "value(%s %g)" % (IR, cfg["vout_nom"])), ("IREV_max", "ymax(abs(%s))" % IR)]
    job = ctx.job("c12")
    job.metrics(pts, ex)
    job.figure("fig_116", [{"curves": curves(pts, "abs(%s)" % IR, "PT"), "ylog": True, "xlab": "VOUT - VIN (V), VIN = 0 V",
                            "ylab": "|IREV| (A)", "specs": [(S["irev_max_ua"] * 1e-6, "SPEC <= %g uA" % S["irev_max_ua"])]}], 700)
    M = job.run()
    stitch(job.out, "fig_116", titles=job.titles.get("fig_116"), labels=["IREV ≤ %g µA" % S["irev_max_ua"]])
    kp = [row("Max IREV at VOUT = %g V" % cfg["vout_nom"], pts, M, "IREV_max", "max", S["irev_max_ua"], "lo", " µA", 3, scale=1e6)]
    return ctx.done(job, {116: ("fig_116", "Reverse current vs VOUT − VIN, VIN = 0 V, %d process × temperature corners" % len(pts))},
                    kp, ex, "VIN source at 0 V, vsource VR forces $DUT_OUTPUT from 0 to %g V; IREV = current VR pushes into "
                    "the LDO output. This LDO has no EN pin, so the EN on/off cases do not apply." % cfg["vout_nom"])


TMAX = 165      # the models stop being valid above ~166 °C (CMI-2434, Vsat < 0)


def check_13(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    # resistive load: with an ideal sink the DC continuation lands on a collapsed branch at -40 C
    pts = [ctx.deck.write("c13", cname(c), dict(c, T=25, tsweep=True), "\n".join(
        [TB_SRC, "RL (VOUT 0) resistor r=%g" % (cfg["vout_nom"] / cfg["iload_max"]), tb_cout(cfg),
         "sw dc param=temp start=-40 stop=%g step=1" % TMAX])) for c in corners(cfg, "PV")]
    pts = ctx.sim(pts)
    ex = [("TSD_trip", 'cross(%s 0.5*value(%s 25) 1 "falling")' % (W_DC, W_DC)), ("VOUT_Tmax", "value(%s %g)" % (W_DC, TMAX))]
    job = ctx.job("c13")
    job.metrics(pts, ex)
    vs = sorted({p["V"] for p in pts})
    cv = [(p["psf"], W_DC, "%s %gV" % (p["P"], p["V"]), COLOR[p["P"]], {0: "dash", 1: "solid", 2: "dot"}[vs.index(p["V"])]) for p in pts]
    job.figure("fig_117", [{"curves": cv, "xlab": "Tj (C)", "ylab": "VOUT (V)",
                            "masks": [([S["tsd_min_c"]] * 2, [0, 2.4], "SPEC TSD >= %g C" % S["tsd_min_c"])]}], 720)
    M = job.run()
    stitch(job.out, "fig_117", titles=job.titles.get("fig_117"), labels=[], mode="vlines",
           vlabels=["TSD ≥ %g °C" % S["tsd_min_c"]])
    # a trip below 100 C is the reference's latch-up dip, not a thermal shutdown
    for p in pts:
        m = M.get(p["point"], {})
        if m.get("TSD_trip") is not None and m["TSD_trip"] < 100:
            m["TSD_trip"] = None
    trips = [M[p["point"]]["TSD_trip"] for p in pts if M.get(p["point"], {}).get("TSD_trip") is not None]
    if trips:
        kp = [row("Min TSD trip", pts, M, "TSD_trip", "min", S["tsd_min_c"], "hi", " °C", 1),
              row("Max TSD trip", pts, M, "TSD_trip", "max", S["tsd_max_c"], "lo", " °C", 1)]
    else:
        kp = [{"l": "TSD trip", "v": "none up to %g °C" % TMAX, "c": "all %d corners" % len(pts),
               "s": "%g to %g °C" % (S["tsd_min_c"], S["tsd_max_c"]), "m": "–", "r": "fail"}]
    return ctx.done(job, {117: ("fig_117", "VOUT vs junction temperature, ILOAD %s, %d process × VIN corners" % (
        eng_s(cfg["iload_max"], "A"), len(pts)))}, kp, ex,
        "dc sweep temp −40 to %g °C at ILOAD %s (static; no self-heating). The sweep stops at %g °C because the model "
        "breaks above it (CMI-2434 at 168 °C: Vsat < 0 in I0.I8.M12). TSD_trip is where VOUT falls below half its "
        "25 °C value; if no corner trips, this design has no thermal shutdown. The dip near −33 °C is not a shutdown: "
        "there the reference settles to a second operating point with VREF at VIN (see #7's power-up scan)." % (
            TMAX, eng_s(cfg["iload_max"], "A"), TMAX))


def check_14(ctx):
    cfg, S = ctx.cfg, ctx.cfg["specs"]
    pts = [ctx.deck.write("c14", cname(c), dict(c, I=cfg["iload_typ"]), "\n".join(
        [TB_SRC, TB_LOAD, tb_cout(cfg), "noi1 (VOUT 0) noise start=10 stop=1M dec=20"])) for c in corners(cfg, "PVT")]
    pts = ctx.sim(pts)
    VN = 'getData("out" ?result \'noise)'
    ex = [("Vn_rms", "sqrt(integ(%s*%s 10 100k))" % (VN, VN)), ("Vn_1k", "value(%s 1k)" % VN), ("Vn_10k", "value(%s 10k)" % VN)]
    job = ctx.job("c14")
    job.metrics(pts, ex)
    p1 = [{"curves": curves(sub, VN, "PT"), "xlog": True, "ylog": True, "xlab": "freq, VIN %g V" % v,
           "ylab": "Vn (V/sqrt(Hz))"} for v, sub in per_vin(pts)]
    p2 = [{"curves": curves(sub, "sqrt(iinteg(%s*%s))" % (VN, VN), "PT"), "xlog": True, "xlab": "freq, VIN %g V" % v,
           "ylab": "integrated Vn from 10 Hz (Vrms)", "specs": [(S["vn_max_uvrms"] * 1e-6, "SPEC <= %g uV" % S["vn_max_uvrms"])]}
          for v, sub in per_vin(pts)]
    job.figure("fig_118", p1, 520)
    job.figure("fig_119", p2, 520)
    M = job.run()
    stitch(job.out, "fig_118", titles=job.titles.get("fig_118"), labels=[])
    stitch(job.out, "fig_119", titles=job.titles.get("fig_119"), labels=["Vn ≤ %g µVrms (10 Hz to 100 kHz)" % S["vn_max_uvrms"]])
    kp = [row("Max Vn, 10 Hz to 100 kHz", pts, M, "Vn_rms", "max", S["vn_max_uvrms"], "lo", " µVrms", 1, scale=1e6),
          info_row("Max Vn density at 1 kHz", pts, M, "Vn_1k", "max", " nV/√Hz", 0, 1e9)]
    return ctx.done(job, {118: ("fig_118", "Output noise density, ILOAD %s, %d PVT points" % (eng_s(cfg["iload_typ"], "A"), len(pts))),
                          119: ("fig_119", "Integrated output noise from 10 Hz")}, kp, ex,
                    "noise 10 Hz to 1 MHz, output VOUT referred to ground, ILOAD %s." % eng_s(cfg["iload_typ"], "A"))


CHECKS = {1: check_1, 2: check_2, 3: check_3, 4: check_4, 5: check_5, 6: check_6, 7: check_7, 8: check_8,
          9: check_9, 10: check_10, 11: check_11, 12: check_12, 13: check_13, 14: check_14}


def eng_s(v, unit):
    for s, p in ((1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(v) >= s * 0.999:
            return "%g %s%s" % (round(v / s, 3), p, unit)
    return "%g %s" % (v, unit)


def info_row(label, pts, M, key, how, unit, dp, scale):
    if how == "absmax":
        vals = [(M[p["point"]][key], p) for p in pts if M.get(p["point"], {}).get(key) is not None]
        v, p = max(vals, key=lambda t: abs(t[0])) if vals else (None, None)
    else:
        v, p = worst(pts, M, key, how)
    if v is None:
        return {"l": label, "v": "no data", "c": "–", "s": "info", "m": "–", "r": "info"}
    return {"l": label, "v": ("{:,.%df}" % dp).format(v * scale) + unit, "c": corner_text(p), "s": "info", "m": "–", "r": "info"}


class Ctx:
    def __init__(self, cfg, work, jobs, nosim):
        self.cfg, self.work, self.jobs, self.nosim = cfg, work, jobs, nosim
        models, dut, stb = extract_dut(cfg)
        os.makedirs(work, exist_ok=True)
        open(os.path.join(work, "dut.scs"), "w").write(dut)
        open(os.path.join(work, "dut_stb.scs"), "w").write(stb)
        self.deck = Deck(cfg, models, work)
        self.mc_corner = {"P": "TT", "V": cfg["vin_typ"], "T": 25}
        self.figs = os.path.join(work, "figs")
        os.makedirs(self.figs, exist_ok=True)

    def sim(self, pts, fmt="psfbin"):
        if self.nosim:
            return [p for p in pts if os.path.isdir(p["psf"])]
        if fmt != "psfbin":
            for p in pts:
                log = os.path.join(p["dir"], "spectre.out")
                if not (os.path.isfile(log) and "completes with 0 errors" in open(log, errors="replace").read()):
                    subprocess.run(["spectre", "input.scs", "-format", fmt, "-raw", "psf", "+log", "spectre.out",
                                    "-ahdllibdir", AHDL],
                                   cwd=p["dir"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=7200)
            return pts
        return run_spectre(pts, self.jobs)

    def job(self, name):
        d = os.path.join(self.work, "viva", name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        return Job(d)

    def done(self, job, charts, kp, ade, setup):
        out = {}
        for cid, (stem, title) in charts.items():
            src = os.path.join(job.out, stem + ".png")
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(self.figs, stem + ".png"))
                out[cid] = {"file": stem + ".png", "title": title}
        return {"charts": out, "kp": kp, "ade": ade, "setup": setup}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--checks", default="all", help="comma list of check numbers, or all")
    ap.add_argument("--jobs", type=int, default=8, help="parallel Spectre runs")
    ap.add_argument("--no-sim", action="store_true", help="reuse existing Spectre results")
    ap.add_argument("-o", "--work", default=os.path.join(HERE, "work"))
    a = ap.parse_args()
    for tool in ("spectre", "virtuoso"):
        if not shutil.which(tool):
            sys.exit("ldr_run: %s is not on PATH; load the Cadence environment first" % tool)
    cfg = load_config()
    ctx = Ctx(cfg, os.path.abspath(a.work), a.jobs, a.no_sim)
    sel = sorted(CHECKS) if a.checks == "all" else [int(x) for x in a.checks.split(",")]
    rj = os.path.join(ctx.work, "real.json")
    real = json.load(open(rj)) if os.path.isfile(rj) else {}
    import time
    for n in sel:
        t0 = time.time()
        print("ldr_run: #%d ..." % n, flush=True)
        r = CHECKS[n](ctx)
        if n == 10 and "kp9_extra" in r:
            real.setdefault("9", {}).setdefault("extra_kp", [])
            real["9"]["extra_kp"] = [r.pop("kp9_extra")]
            real["9"].setdefault("charts", {})
            if 112 in r["charts"]:
                real["9"]["charts"]["112"] = r["charts"].pop(112)
        prev = real.get(str(n), {})
        r["charts"] = {str(k): v for k, v in r["charts"].items()}
        if n == 9 and "112" in prev.get("charts", {}):
            r["charts"]["112"] = prev["charts"]["112"]
        if n == 9 and prev.get("extra_kp"):
            r["extra_kp"] = prev["extra_kp"]
        real[str(n)] = r
        json.dump(real, open(rj, "w"), indent=1, ensure_ascii=False)
        print("ldr_run: #%d done in %.0f s, %d chart(s), %d table row(s)" % (n, time.time() - t0, len(r["charts"]), len(r["kp"])), flush=True)
    # #15 cannot run here; say why in its setup line (charts stay PLACEHOLDER)
    real["15"] = {"charts": {}, "kp": [], "ade": [], "setup":
                  "Not simulated on this install. Aging: Spectre's reliability analysis stops with SPECTRE-17074 "
                  "(no aging model cards for any device in the PDK model file), so HCI/NBTI cannot be run. "
                  "EM/IR needs a post-layout extraction of %s and an EMIR tool (Voltus-Fi or Calibre PERC), which "
                  "are not set up. The charts below remain the page's illustrative model." % cfg["dut_cell"]}
    json.dump(real, open(rj, "w"), indent=1, ensure_ascii=False)
    print("ldr_run: wrote %s" % rj)


if __name__ == "__main__":
    main()
