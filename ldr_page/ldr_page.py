#!/usr/bin/env python3
"""ldr_page.py - the LDO LDR checklist page with your real ADE XL results in it.

    python3 ldr_page.py <adexl>/results/data/Interactive.N.rdb [--viva] [-o out_dir]

Takes the published page (https://borenw.github.io/ldo-loadreg-setup/, or --page FILE),
keeps its layout, and:

  * replaces the load-regulation figures (#8 and section 4) with images exported from
    Virtuoso Visualization for this run, and fills the #8 worst-case table and the
    section 4 tables from the results database;
  * stamps PLACEHOLDER over every other chart and worst-case table, which are still the
    page's illustrative model.

--viva runs `virtuoso -nograph` with ldr_viva_figs.il to export the figures (Cadence must
be on PATH). Without it, the PNGs already in <out_dir>/figs are used.
The output is <out_dir>/ldr_<cell>_<history>.html, with the full-size PNGs beside it.
"""
import argparse
import base64
import html
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_URL = "https://borenw.github.io/ldo-loadreg-setup/"
# swatches match ViVA's y1..y5 curve colours in the exported figures
PROC_COLOR = {"TT": "#ff0000", "SS": "#00cc66", "FF": "#ff00ff", "SF": "#00dfdf", "FS": "#9900e5"}


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*([-+0-9.eE]+)\s*([fpnumkKMG]?)\s*", str(v))
    if not m:
        return None
    return float(m.group(1)) * {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1,
                                "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]


def eng(v, unit):
    """10u -> '10 µA'"""
    for s, p in ((1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(v) >= s * 0.999:
            return "%g %s%s" % (round(v / s, 3), p, unit)
    return "%g %s" % (v, unit)


# ---------------------------------------------------------------- results database
def read_run(rdb):
    db = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(rdb), uri=True)
    q = lambda s, *a: db.execute(s, a).fetchall()
    test = q("select name from test")[0][0]
    info = dict(q("select * from maestroViewInfo"))
    raw = next((r for r, in q("select rawDir from psfTable") if r and os.path.isdir(r)), None)
    if not raw:
        sys.exit("ldr_page: the run's PSF directory (psfTable.rawDir) is not on disk")
    res = {n: (rid, e) for rid, n, e in q("select resultID, name, expression from result")}
    spec = {}
    for sid, rid, ty, e1 in q("select specID, resultID, type, expression1 from spec"):
        name = next(n for n, (r, _) in res.items() if r == rid)
        spec[name] = {"id": sid, "type": ty, "lim": num(e1), "text": e1}
    par = dict(q("select parameterID, name from parameter"))
    pts = []
    for pid, cn in q("select p.pointID, c.name from point p join corner c "
                     "on c.cornerID = p.cornerID order by p.pointID"):
        pv = {par.get(k): v for k, v in q("select parameterID, value from parameterValue "
                                          "where pointID=?", pid)}
        vals = {}
        for n, (rid, _) in res.items():
            r = q("select value from resultValue where pointID=? and resultID=?", pid, rid)
            vals[n] = num(r[0][0]) if r and r[0][0] != "wave" else None
        ok = {}
        for n, s in spec.items():
            r = q("select specStatus from specValue where pointID=? and specID=?", pid, s["id"])
            ok[n] = (r[0][0] == 0) if r else None
        pts.append({"pid": pid, "corner": cn or "Nominal", "proc": cn.split("_")[0] if cn else "Nominal",
                    "vin": num(pv.get("vin")), "T": num(pv.get("temperature")),
                    "vnom": num(pv.get("VOUT_NOM")), "iload": num(pv.get("iload")),
                    "vals": vals, "ok": ok})
    expr = {n: e for n, (_, e) in res.items()}
    when = q("select min(startTime) from testStatus")[0][0]
    return {"test": test, "info": info, "raw": raw, "spec": spec, "pts": pts, "expr": expr,
            "when": when, "rdb": os.path.abspath(rdb)}


def sweep_range(run):
    """ILOAD start/stop from one point's netlist (the analysis line ADE wrote)."""
    p = next(p for p in run["pts"] if p["corner"] != "Nominal")
    f = os.path.join(run["raw"], str(p["pid"]), run["test"], "netlist", "input.scs")
    m = re.search(r"^dc\s+dc\s+param=(\S+)\s+start=(\S+)\s+stop=(\S+)", open(f).read(), re.M)
    return (m.group(1), num(m.group(2)), num(m.group(3))) if m else ("iload", None, None)


# ---------------------------------------------------------------- ViVA export
def run_virtuoso(script, cwd, log, timeout=900):
    """virtuoso -nograph < script. Batch Virtuoso here sometimes stays at its prompt after
    hiQuit(), so the script prints LDR DONE and the whole process group is stopped then."""
    import signal
    import time
    with open(script) as fin, open(log, "w") as fout:
        pr = subprocess.Popen(["virtuoso", "-nograph"], stdin=fin, stdout=fout,
                              stderr=subprocess.STDOUT, cwd=cwd, start_new_session=True)
        t0 = time.time()
        while pr.poll() is None and time.time() - t0 < timeout:
            time.sleep(2)
            if "LDR DONE" in open(log, errors="replace").read():
                time.sleep(5)
                break
        if pr.poll() is None:
            os.killpg(pr.pid, signal.SIGTERM)
            try:
                pr.wait(20)
            except subprocess.TimeoutExpired:
                os.killpg(pr.pid, signal.SIGKILL)
    if "LDR DONE" not in open(log, errors="replace").read():
        sys.exit("ldr_page: Virtuoso did not finish the export; see %s" % log)


def export_figs(run, figdir):
    os.makedirs(figdir, exist_ok=True)
    for f in os.listdir(figdir):
        if f.endswith(".png"):
            os.remove(os.path.join(figdir, f))
    cor = [p for p in run["pts"] if p["corner"] != "Nominal"]
    nom = next((p for p in run["pts"] if p["corner"] == "Nominal"), cor[0])
    vnom, err = nom["vnom"], run["spec"].get("VOUT_err_pct", {}).get("lim", 1.5)
    vins = sorted({p["vin"] for p in cor})
    clip = sorted({p["vin"] for p in cor if (p["vals"].get("VOUT_heavy") or vnom) < 0.72 * vnom})
    rows = "\n".join('(%d "%s" "%s" %g %d)' % (p["pid"], p["corner"], p["proc"], p["vin"], round(p["T"]))
                     for p in cor)
    with open(os.path.join(figdir, "ldr_points.il"), "w") as fh:
        fh.write('ldoRaw = "%s"\nldoTest = "%s"\nldoOut = "%s"\nldoVinTyp = %g\nldoVnom = %g\n'
                 'ldoErr = %g\nldoVins = \'(%s)\nldoYlim = \'(%s)\nldoPts = \'(\n%s)\n'
                 % (run["raw"], run["test"], os.path.abspath(figdir), nom["vin"], vnom, err,
                    " ".join("%g" % v for v in vins), " ".join("%g" % v for v in clip), rows))
    if not shutil.which("virtuoso"):
        sys.exit("ldr_page: --viva needs virtuoso on PATH (load your Cadence environment first)")
    # Virtuoso's helper processes inherit stdout, so a pipe never closes: log to a file
    log = os.path.join(figdir, "viva_export.log")
    run_virtuoso(os.path.join(HERE, "ldr_viva_figs.il"), figdir, log)
    for line in open(log, errors="replace").read().splitlines():
        if line.lstrip("> ").startswith(("LDR ", "*Error*")):
            print("  viva:", line.lstrip("> "))
    labels = ["VOUT ≤ %.3f V  (spec +%g%%)" % (vnom * (1 + err / 100), err),
              "VOUT ≥ %.3f V  (spec −%g%%)" % (vnom * (1 - err / 100), err)]
    for prefix in ("fig_loadreg_typ", "fig_loadreg_pvt"):
        stitch(figdir, prefix, labels)


FONT = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")
             if os.path.isfile(f)), None)


RED = (222, 53, 11)


def _font(size=15):
    from PIL import ImageFont
    return ImageFont.truetype(FONT, size) if FONT else ImageFont.load_default()


def _is_red(c):
    return c[0] > 200 and c[1] < 60 and c[2] < 60


def _groups(idx, gap=2):
    out = []
    for y in idx:
        if out and y - out[-1][-1] <= gap:
            out[-1].append(y)
        else:
            out.append([y])
    return out


def _tag(d, xy, text, font, anchor_right=True):
    tw, th = font.getsize(text)
    x, y = xy
    if anchor_right:
        x -= tw
    d.rectangle([x - 4, y - 1, x + tw + 4, y + th + 2], fill="white")
    d.text((x, y), text, fill=RED, font=font)


def label_specs(im, labels, mode="lines", vlabels=None):
    """Write spec values and criteria in red beside the spec lines. Spec lines are the only
    pure-red (ViVA y1) pixels. mode 'lines': label each horizontal red line, top to bottom, in
    the order given; 'note': stack the labels in the top right; 'vlines': label vertical red
    lines left to right with vlabels."""
    from PIL import ImageDraw
    px = im.load()
    w, h = im.size
    x0 = int(w * 0.16)                                  # skip ViVA's legend column
    d = ImageDraw.Draw(im)
    font = _font()
    if mode == "lines" and labels:
        rows = [y for y in range(h) if sum(_is_red(px[x, y]) for x in range(x0, w, 3)) > (w - x0) / 3 * 0.35]
        gs = _groups(rows)
        if len(gs) < len(labels):
            mode = "note"
        else:
            for g, text in zip(gs, labels):
                y = g[0] - 21 if g[0] > 45 else g[-1] + 4
                _tag(d, (w - 14, y), text, font)
            return
    if mode == "note" and labels:
        for k, text in enumerate(labels):
            _tag(d, (w - 14, 40 + 22 * k), text, font)
    if mode == "vlines" and vlabels:
        cols = [x for x in range(x0, w) if sum(_is_red(px[x, y]) for y in range(0, h, 3)) > h / 3 * 0.35]
        for k, (g, text) in enumerate(zip(_groups(cols), vlabels)):
            tw = font.getsize(text)[0]
            if g[-1] + 12 + tw < w:
                _tag(d, (g[-1] + 6, 30 + 20 * (k % 2)), text, font, anchor_right=False)
            else:                               # no room on the right: put it left of the line
                _tag(d, (g[0] - 6, 30 + 20 * (k % 2)), text, font)


def stitch(figdir, prefix, labels=(), mode="lines", vlabels=None, titles=None):
    """ViVA saves each subwindow as <prefix>window:<w>.<n>.png; stack them into <prefix>.png."""
    from PIL import Image
    parts = sorted((f for f in os.listdir(figdir) if f.startswith(prefix + "window:")),
                   key=lambda f: int(re.search(r"\.(\d+)\.png$", f).group(1)))
    if not parts and os.path.isfile(os.path.join(figdir, prefix + ".png")):
        parts = [prefix + ".png"]  # a one-panel window is saved under the plain name
    if not parts:
        sys.exit("ldr_page: ViVA wrote no %s images; see %s/viva_export.log" % (prefix, figdir))
    ims = [Image.open(os.path.join(figdir, f)).convert("RGB") for f in parts]
    for im in ims:
        label_specs(im, labels, mode, vlabels)
    if titles:                                 # ViVA's own title is the raw expression; replace it
        from PIL import ImageDraw
        f = _font(16)
        titled = []
        for k, im in enumerate(ims):
            band = Image.new("RGB", (im.width, 30), "white")
            ImageDraw.Draw(band).text((12, 6), titles[k] if k < len(titles) else "", fill=(23, 43, 77), font=f)
            t = Image.new("RGB", (im.width, im.height + 30), "white")
            t.paste(band, (0, 0))
            t.paste(im, (0, 30))
            titled.append(t)
        ims = titled
    out = Image.new("RGB", (max(i.width for i in ims), sum(i.height for i in ims)), "white")
    y = 0
    for im in ims:
        out.paste(im, (0, y))
        y += im.height
    out.save(os.path.join(figdir, prefix + ".png"))
    for f in parts:
        if f != prefix + ".png":
            os.remove(os.path.join(figdir, f))


# ---------------------------------------------------------------- page patching
def sub1(pat, rep, s, flags=re.S):
    out, n = re.subn(pat, lambda m: rep(m) if callable(rep) else rep, s, count=1, flags=flags)
    if n != 1:
        sys.exit("ldr_page: the page layout changed; could not find:\n  %s" % pat[:90])
    return out


def build(run, page, figdir, real=None, realdir=None, embed=False):
    pts = run["pts"]
    cor = [p for p in pts if p["corner"] != "Nominal"]
    nom = next((p for p in pts if p["corner"] == "Nominal"), None)
    S = run["spec"]
    ERR, LR = S.get("VOUT_err_pct"), S.get("load_reg_mV_per_A")
    vnom = (nom or cor[0])["vnom"]
    vtyp = (nom or cor[0])["vin"]
    param, i0, i1 = sweep_range(run)
    info = run["info"]
    src = "%s/%s/%s · %s · test %s" % (info.get("lib"), info.get("cell"), info.get("view"),
                                       info.get("history"), run["test"])
    lab = lambda p: "%s %.2f V %s" % (p["proc"], p["vin"], "%g °C" % p["T"]) if p["T"] is not None else p["corner"]
    allok = lambda p: all(v is True for v in p["ok"].values())

    def img(name, alt):
        f = os.path.join(figdir, name)
        if not os.path.isfile(f):
            sys.exit("ldr_page: %s is missing; run with --viva to export it" % f)
        return ('<a href="figs/%s" title="Open the full-size export"><img class="realimg" alt="%s" '
                'src="%s"></a>' % (name, html.escape(alt), src_of(f, name)))

    def src_of(f, name):
        """Linked figs/<name> keeps the page small (GitHub will not display files over ~1 MB);
        --embed inlines the PNG for a single self-contained file."""
        if embed:
            return "data:image/png;base64," + base64.b64encode(open(f, "rb").read()).decode()
        return "figs/" + name

    # -- real data for the page script: #8 chart and kp rows, section 4 table
    def kp_row(label, key, unit, dp):
        s = S[key]
        w = max(cor, key=lambda p: p["vals"][key] if p["vals"][key] is not None else -1e99)
        v = w["vals"][key]
        mg = s["lim"] - v
        fr = mg / abs(s["lim"])
        cl = "hot" if mg < 0 else "mid" if fr < 0.2 else "cool"
        f = lambda x: ("{:,.%df}" % dp).format(x)
        return ('<tr><td>%s</td><td class="n %s">%s%s</td><td>%s</td><td class="n">≤ %s%s</td>'
                '<td class="n %s">%s%s (%.0f%%)</td><td><span class="chip %s">%s</span></td></tr>'
                % (label, cl, f(v), unit, html.escape(lab(w)), s["text"], unit, cl, f(mg), unit,
                   fr * 100, "fail" if mg < 0 else "warn" if fr < 0.2 else "pass",
                   "Fail" if mg < 0 else "Marginal" if fr < 0.2 else "Pass"))

    kp8 = (kp_row("Max VOUT error", "VOUT_err_pct", "%", 2) if ERR else "") + \
          (kp_row("Max load regulation", "load_reg_mV_per_A", " mV/A", 0) if LR else "")
    n_typ = sum(1 for p in cor if abs(p["vin"] - vtyp) < 1e-9)
    fig_typ = ('<div class="card real" style="grid-column:1/-1"><div class="cap">Figure ${FIG++} · Load '
               'regulation, VOUT vs ILOAD, VIN %s V, %d PVT points <span class="chip pass">Cadence result'
               '</span></div>%s<div class="leg"><span>Exported from Virtuoso Visualization · %s</span>'
               '</div></div>' % ("%g" % vtyp, n_typ, img("fig_loadreg_typ.png", "VOUT vs ILOAD at typical VIN"),
                                 html.escape(src)))

    def bar(v, lim, dp):
        if v is None:
            return "–"
        r = min(abs(v) / lim, 1)
        over = v > lim
        cls = "hot" if over else "mid" if v / lim > 0.7 else "cool"
        return ('<div class="barc"><div class="bar" style="width:%dpx%s"></div><span class="%s">%s</span></div>'
                % (max(3, r * 110), ";background:var(--spec)" if over else "", cls,
                   ("{:,.%df}" % dp).format(v)))

    rank = sorted(pts, key=lambda p: -(p["vals"].get("VOUT_err_pct") or -1))
    lrt = "".join(
        '<tr><td class="rank">#%d</td><td><span style="display:inline-block;width:10px;height:10px;'
        'border-radius:2px;background:%s;margin-right:6px"></span>%s</td><td class="n">%.4f V</td>'
        '<td class="n">%.4f V</td><td>%s</td><td>%s</td><td><span class="chip %s">%s</span></td></tr>'
        % (i + 1, PROC_COLOR.get(p["proc"], "var(--fg)"), html.escape(lab(p)), p["vals"]["VOUT_light"],
           p["vals"]["VOUT_heavy"], bar(p["vals"].get("load_reg_mV_per_A"), LR["lim"], 0),
           bar(p["vals"].get("VOUT_err_pct"), ERR["lim"], 2), "pass" if allok(p) else "fail",
           "Pass" if allok(p) else "Fail") for i, p in enumerate(rank))
    # -- the other checks, from ldr_run.py (real.json): chart cards, table rows, ADE outputs, setup
    real = real or {}

    def card(cid, info):
        f = os.path.join(realdir, info["file"])
        src = src_of(f, info["file"])
        return ('<div class="card real" style="grid-column:1/-1"><div class="cap">Figure ${FIG++} · %s '
                '<span class="chip pass">Cadence result</span></div><a href="figs/%s" title="Open the full-size '
                'export"><img class="realimg" alt="%s" src="%s" loading="lazy"></a><div class="leg"><span>'
                'Spectre on the ADE netlist of %s, exported from Virtuoso Visualization</span></div></div>'
                % (html.escape(info["title"]), info["file"], html.escape(info["title"]), src, html.escape(cell_name)))

    def kp_html(rows):
        lab = {"pass": "Pass", "warn": "Marginal", "fail": "Fail", "info": "Info", "na": "Not valid"}
        cls = {"pass": "cool", "warn": "mid", "fail": "hot", "info": "", "na": "mid"}
        return "".join('<tr><td>%s</td><td class="n %s">%s</td><td>%s</td><td class="n">%s</td><td class="n %s">%s</td>'
                       '<td><span class="chip %s"%s>%s</span></td></tr>'
                       % (html.escape(r["l"]), cls[r["r"]], html.escape(r["v"]), html.escape(r["c"]), html.escape(r["s"]),
                          cls[r["r"]], html.escape(r["m"]), {"info": "", "na": "warn"}.get(r["r"], r["r"]),
                          ' style="border:1px solid var(--line)"' if r["r"] == "info" else "", lab[r["r"]]) for r in rows)

    cell_name = info.get("cell", "the DUT").replace("tb_", "").replace("_LoadReg", "")
    charts, kps, ades, setups, extras = {"110": fig_typ}, {"7": kp8}, {}, {}, {}
    for n, r in real.items():
        i = int(n) - 1
        for cid, inf in r.get("charts", {}).items():
            if realdir and os.path.isfile(os.path.join(realdir, inf["file"])):
                charts[cid] = card(cid, inf)
        ex = [card(None, inf) for inf in r.get("extra", []) if realdir and os.path.isfile(os.path.join(realdir, inf["file"]))]
        if ex:
            extras[str(i)] = ex
        rows = kp_html(r.get("kp", []) + r.get("extra_kp", []))
        if n == "8":
            kps["7"] = kp8 + rows
        elif rows:
            kps[str(i)] = rows
        if r.get("ade") and n != "8":
            ades[str(i)] = r["ade"]
        if r.get("setup"):
            setups[str(i)] = r["setup"] if n != "8" else None
    setups = {k: v for k, v in setups.items() if v}
    REAL = {"charts": charts, "kp": kps, "ade": ades, "setup": setups, "extra": extras, "lrt": lrt}

    # -- CSS, title, banner note
    css = (".ph{position:relative}.ph::after{content:'PLACEHOLDER';position:absolute;inset:0;display:grid;"
           "place-items:center;font:700 34px var(--sans);letter-spacing:.12em;color:var(--spec);opacity:.2;"
           "transform:rotate(-16deg);pointer-events:none;z-index:2}"
           ".tw.ph::after{font-size:26px}"
           ".realimg{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:2px;background:#fff}"
           ".real .cap .chip{margin-left:6px;vertical-align:1px}"
           ".realnote{border-left:3px solid var(--ok);background:var(--ok-bg);color:var(--fg)}")
    page = sub1(r"</style>", css + "\n</style>", page)
    cell = info.get("cell", "")
    page = sub1(r"<title>[^<]*</title>", "<title>LDO LDR · %s</title>" % html.escape(cell), page)
    page = sub1(r'(<h1[^>]*>)([^<]*)(</h1>)', lambda m: m.group(1) + m.group(2) + " · " + html.escape(cell) + m.group(3), page)
    when = ""
    if run["when"]:
        import datetime
        when = datetime.datetime.fromtimestamp(float(run["when"])).strftime("%Y-%m-%d %H:%M")
    nreal = len(charts)
    note = ('<div class="note realnote"><b>Real results</b> for %s. Load regulation (#8, Figure 20, section 4) is the '
            'ADE XL run %s, %s: %d points (%d PVT corners + nominal), %s swept %s to %s. %s'
            'Every figure tagged <span class="chip pass">Cadence result</span> (%d of them) is a Spectre run on the '
            'ADE netlist of the DUT, exported from Virtuoso Visualization, with its worst-case table and ADE '
            'expressions filled from the same run. Specs are the page\'s limits scaled to this LDO; edit them in '
            'ldr_config.json. Anything stamped <b>PLACEHOLDER</b> is still the page\'s illustrative model of a '
            '1.2 V, 300 mA LDO.</div>' % (html.escape(cell_name), html.escape(src), when, len(pts), len(cor), param,
                                         eng(i0, "A"), eng(i1, "A"),
                                         "The other checks come from ldr_run.py. " if real else "", nreal))
    page = sub1(r'(<div class="meta">.*?</div>)', lambda m: m.group(1) + "\n" + note, page)

    # -- page script: real chart for chart 110, real #8 table, watermark the rest
    page = sub1(r"<script>\nconst T=\{", "<script>\nconst REAL=%s;\nconst T={" %
                json.dumps(REAL, ensure_ascii=False).replace("</", "<\\/"), page)
    page = sub1(re.escape("d.ch.forEach(o=>{h+=`<div class=\"card\"><div class=\"cap\">Figure ${FIG++} · ${o.ti}</div>${plot(o)}${d.cleg?'':leg(o)}</div>`});"),
                "(REAL.extra[i]||[]).forEach(x=>{d.ch.push({id:'x'+d.ch.length,extra:x})});\n"
                "d.ch.forEach(o=>{h+=o.extra?o.extra.replace('${FIG++}',FIG++):REAL.charts[o.id]?REAL.charts[o.id].replace('${FIG++}',FIG++):`<div class=\"card ph\"><div class=\"cap\">Figure ${FIG++} · ${o.ti}</div>${plot(o)}${d.cleg?'':leg(o)}</div>`});", page)
    page = sub1(re.escape("h+='<div class=\"tw\"><table><thead><tr><th>Metric</th>"),
                "h+=(REAL.kp[i]?'<div class=\"tw\">':'<div class=\"tw ph\">')+'<table><thead><tr><th>Metric</th>", page)
    page = sub1(re.escape("h+='</tbody></table></div>';\nh+=`<div class=\"ade\">"),
                "if(REAL.kp[i])h=h.replace(/<tbody>(?:(?!<tbody>)[\\s\\S])*$/,'<tbody>'+REAL.kp[i]);\nh+='</tbody></table></div>';\nh+=`<div class=\"ade\">", page)
    # ADE outputs box and setup line: the expressions this run actually evaluated
    page = sub1(re.escape("${k[6].map(e=>`<b>${e[0]}</b><br>${esc(e[1])}`).join('<br>')}</div><div class=\"su\">Setup: ${esc(k[7])}</div>"),
                "${(REAL.ade[i]||k[6]).map(e=>`<b>${e[0]}</b><br>${esc(e[1])}`).join('<br>')}</div><div class=\"su\">Setup: ${esc(REAL.setup[i]||k[7])}</div>", page)
    page = sub1(re.escape("const t=K[i][6].map(x=>x[0]+'\\t'+x[1]).join('\\n');"),
                "const t=(REAL.ade[i]||K[i][6]).map(x=>x[0]+'\\t'+x[1]).join('\\n');", page)
    page = sub1(r"const L=\[\['SS 125°C'.*?\n.*?\n.*?\ndocument\.getElementById\('lrt'\)\.innerHTML=rows\.map.*?\.join\(''\);",
                "document.getElementById('lrt').innerHTML=REAL.lrt;", page)

    # -- section 4: intro, figure, donut, tables
    page = sub1(r'(<div class="banner" id="lr">[^<]*</div>\s*)<p>.*?</p>',
                lambda m: m.group(1) + '<p>DC sweep of the load current from %s to %s at VIN = %g V '
                '(%s corners), across the <a href="#pvt">shared PVT set</a>: %d points. Two limits apply: '
                'the static slope (load regulation, mV/A) and the total VOUT error over the whole load '
                'range. The figure and tables below are the real run.</p>'
                % (eng(i0, "A"), eng(i1, "A"), vtyp,
                   " / ".join("%g V" % v for v in sorted({p["vin"] for p in cor})), len(cor)), page)
    page = sub1(r'(<h3 id="lrchart"><span id="lrfig">Figure</span> · VOUT vs ILOAD across PVT</h3>\s*)<div class="fig">.*?</svg>.*?</div>\s*</div>\s*<p class="cap">.*?</p>',
                lambda m: m.group(1) + '<div class="fig real">%s</div><p class="cap">One strip per VIN; '
                'colour is the process corner (legend on the left of each panel) and dash is the temperature: '
                'dashed −40 °C, solid 25 °C, dotted 125 °C. The red lines are the ±%g%% VOUT error spec, labelled with their limits. '
                'Exported from Virtuoso Visualization · %s.</p>'
                % (img("fig_loadreg_pvt.png", "VOUT vs ILOAD, all PVT points"), ERR["lim"], html.escape(src)), page)

    rep5 = [p for p in cor if abs(p["vin"] - vtyp) < 1e-9 and (p["proc"], p["T"]) in
            {("SS", 125), ("FF", 125), ("TT", 25), ("SS", -40), ("FF", -40)}]
    if len(rep5) == 5:
        # same process colour as the figure; the hot corner of a pair is drawn lighter
        cmap = {k: PROC_COLOR[k[0]] + ("99" if k[1] == 125 else "") for k in
                [("SS", 125), ("FF", 125), ("TT", 25), ("SS", -40), ("FF", -40)]}
        rep5.sort(key=lambda p: -p["vals"]["load_reg_mV_per_A"])
        tot = sum(p["vals"]["load_reg_mV_per_A"] for p in rep5)
        acc, grad, leg = 0, [], []
        for p in rep5:
            sh = p["vals"]["load_reg_mV_per_A"] / tot * 100
            c = cmap[(p["proc"], p["T"])]
            grad.append("%s %.1f%% %.1f%%" % (c, acc, acc + sh))
            acc += sh
            leg.append('    <div><i style="background:%s"></i>%s %g°C · %s mV/A (%.0f%%)</div>'
                       % (c, p["proc"], p["T"], "{:,.0f}".format(p["vals"]["load_reg_mV_per_A"]), sh))
        pie = ('<div class="pie">\n  <div class="donut" style="background:conic-gradient(%s)" role="img" '
               'aria-label="Share of total load-regulation slope by corner"><span>%s mV/A<br>total</span></div>\n'
               '  <div class="lg">\n%s\n    <div style="color:var(--muted);font-size:12.5px">Five representative '
               'corners at VIN %g V, as in the original figure</div>\n  </div>\n</div>'
               % (",".join(grad), "{:,.0f}".format(tot), "\n".join(leg), vtyp))
        page = sub1(r'(<h3 id="lrtable">Worst-case summary</h3>\s*)<div class="pie">.*?</div>\s*</div>\s*</div>',
                    lambda m: m.group(1) + pie, page)
    page = sub1(r"<th>VOUT at 1 mA</th><th>VOUT at 300 mA</th>",
                "<th>VOUT at %s</th><th>VOUT at %s</th>" % (eng(i0, "A"), eng(i1, "A")), page)
    page = sub1(r"<th>Load reg \(spec ≤ 20 mV/A\)</th><th>VOUT error \(spec ≤ 1.5%\)</th>",
                "<th>Load reg (spec ≤ %s mV/A)</th><th>VOUT error (spec ≤ %s%%)</th>" % (LR["text"], ERR["text"]), page)

    def st(key, dp):
        v = [p["vals"][key] for p in cor if p["vals"][key] is not None]
        t = next((p["vals"][key] for p in cor if abs(p["vin"] - vtyp) < 1e-9 and p["proc"] == "TT" and p["T"] == 25), None)
        lim = S[key]["lim"]
        f = lambda x: ("{:,.%df}" % dp).format(x)
        cls = lambda x: "hot" if x > lim else "mid" if x > 0.7 * lim else "cool"
        mg = lim - max(v)
        return {"typ": (f(t), cls(t)), "mean": (f(statistics.mean(v)), cls(statistics.mean(v))),
                "max": (f(max(v)), cls(max(v))), "sd": (f(statistics.pstdev(v)), ""),
                "mg": ("%s (%.0f%%)" % (f(mg), mg / lim * 100), "hot" if mg < 0 else "cool")}
    a, b = st("load_reg_mV_per_A", 0), st("VOUT_err_pct", 2)
    srow = lambda l, k: '<tr><td>%s</td><td class="n %s">%s</td><td class="n %s">%s</td></tr>' % (l, a[k][1], a[k][0], b[k][1], b[k][0])
    stats = "\n".join([srow("Typical (TT 25°C, VIN %g V)" % vtyp, "typ"), srow("Mean of %d corners" % len(cor), "mean"),
                       srow("Max", "max"), srow("Std dev of corners", "sd"), srow("Worst margin to spec", "mg")])
    page = sub1(r"(<thead><tr><th>Statistic</th><th>Load reg \(mV/A\)</th><th>VOUT error \(%\)</th></tr></thead>\s*<tbody>).*?(</tbody>)",
                lambda m: m.group(1) + "\n" + stats + "\n" + m.group(2), page)
    ade = "\n".join('<tr><td><code>%s</code></td><td><code>%s</code></td><td>%s</td></tr>'
                    % (html.escape(n), html.escape(e or ""), ("&lt; %s" % html.escape(S[n]["text"])) if n in S else
                       ("plot only" if n == "VOUT" else "info")) for n, e in run["expr"].items())
    page = sub1(r'(<h3 id="lrade">ADE XL outputs</h3>.*?<tbody>).*?(</tbody>)',
                lambda m: m.group(1) + "\n" + ade + "\n" + m.group(2), page)
    return page


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("rdb", help="…/adexl/results/data/Interactive.N.rdb")
    ap.add_argument("-o", "--out", default=".", help="output directory (default: here)")
    ap.add_argument("--page", default=PAGE_URL, help="page to start from: file or URL (default: the published page)")
    ap.add_argument("--viva", action="store_true", help="export the figures from ViVA first (needs virtuoso)")
    ap.add_argument("--real", help="real.json from ldr_run.py; its figures replace the other placeholders")
    ap.add_argument("--embed", action="store_true", help="inline the PNGs (one self-contained file, ~6 MB)")
    a = ap.parse_args()

    run = read_run(a.rdb)
    figdir = os.path.join(a.out, "figs")
    if a.viva:
        export_figs(run, figdir)
    page = (urllib.request.urlopen(a.page).read().decode("utf-8") if re.match(r"https?://", a.page)
            else open(a.page, encoding="utf-8").read())
    outf = os.path.join(a.out, "ldr_%s_%s.html" % (run["info"].get("cell", "ldo"), run["info"].get("history", "run")))
    with open(outf, "w", encoding="utf-8") as fh:
        real, realdir = None, None
        if a.real:
            real = json.load(open(a.real))
            realdir = os.path.join(os.path.dirname(os.path.abspath(a.real)), "figs")
            os.makedirs(figdir, exist_ok=True)
            for f in os.listdir(realdir):
                shutil.copy(os.path.join(realdir, f), os.path.join(figdir, f))
        fh.write(build(run, page, figdir, real, realdir, a.embed))
    print("ldr_page: wrote file://%s" % os.path.abspath(outf))


if __name__ == "__main__":
    main()
